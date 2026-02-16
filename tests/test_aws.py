"""Tests for AWS live resource discovery module."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from k8s_observability_agent.aws import (
    _discover_dynamodb,
    _discover_ecs,
    _discover_eks,
    _discover_elasticache,
    _discover_lambda,
    _discover_msk,
    _discover_opensearch,
    _discover_rds,
    _discover_s3,
    _discover_sns,
    _discover_sqs,
    discover_aws_resources,
)
from k8s_observability_agent.models import AwsDiscovery, IaCSource


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════


def _mock_session(client_factory: dict[str, Any] | None = None) -> MagicMock:
    """Create a mock boto3 session."""
    session = MagicMock()
    session.region_name = "eu-west-1"

    if client_factory:
        def _get_client(service_name: str, **kwargs: Any) -> MagicMock:
            return client_factory.get(service_name, MagicMock())
        session.client.side_effect = _get_client

    return session


def _paginator_mock(method: str, pages: list[dict]) -> MagicMock:
    """Create a mock paginator that yields the given pages."""
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = pages
    client.get_paginator.return_value = paginator
    return client


# ══════════════════════════════════════════════════════════════════════════════
#  RDS Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverRDS:
    def test_discovers_db_instances(self) -> None:
        client = _paginator_mock("describe_db_instances", [
            {"DBInstances": [
                {
                    "DBInstanceIdentifier": "prod-db",
                    "Engine": "postgres",
                    "EngineVersion": "15.4",
                    "DBInstanceClass": "db.r6g.large",
                    "AllocatedStorage": 100,
                    "MultiAZ": True,
                    "DBInstanceStatus": "available",
                    "Endpoint": {"Address": "prod-db.xxx.rds.amazonaws.com", "Port": 5432},
                    "DBSubnetGroup": {"VpcId": "vpc-123"},
                },
            ]},
        ])
        # Also mock the cluster paginator
        cluster_pag = MagicMock()
        cluster_pag.paginate.return_value = [{"DBClusters": []}]
        client.get_paginator.side_effect = lambda m: (
            _paginator_mock("", [{"DBInstances": [{
                "DBInstanceIdentifier": "prod-db",
                "Engine": "postgres",
                "EngineVersion": "15.4",
                "DBInstanceClass": "db.r6g.large",
                "AllocatedStorage": 100,
                "MultiAZ": True,
                "DBInstanceStatus": "available",
                "Endpoint": {"Address": "prod-db.xxx.rds.amazonaws.com", "Port": 5432},
                "DBSubnetGroup": {"VpcId": "vpc-123"},
            }]}]).get_paginator("") if m == "describe_db_instances" else cluster_pag
        )

        session = _mock_session({"rds": client})
        resources = _discover_rds(session, "eu-west-1")

        assert len(resources) >= 1
        db = next(r for r in resources if r.name == "prod-db")
        assert db.archetype == "database"
        assert db.resource_type == "aws_rds_instance"
        assert db.provider == "aws"
        assert db.properties["engine"] == "postgres"
        assert db.properties["multi_az"] is True
        assert any("postgres_exporter" in n for n in db.monitoring_notes)

    def test_discovers_aurora_clusters(self) -> None:
        client = MagicMock()
        inst_pag = MagicMock()
        inst_pag.paginate.return_value = [{"DBInstances": []}]
        cluster_pag = MagicMock()
        cluster_pag.paginate.return_value = [{"DBClusters": [{
            "DBClusterIdentifier": "aurora-prod",
            "Engine": "aurora-postgresql",
            "EngineVersion": "15.4",
            "Status": "available",
            "Endpoint": "aurora-prod.cluster.rds.amazonaws.com",
            "ReaderEndpoint": "aurora-prod.cluster-ro.rds.amazonaws.com",
            "Port": 5432,
            "DBClusterMembers": [{"x": 1}, {"x": 2}],
        }]}]
        client.get_paginator.side_effect = lambda m: (
            inst_pag if m == "describe_db_instances" else cluster_pag
        )

        session = _mock_session({"rds": client})
        resources = _discover_rds(session, "eu-west-1")

        assert len(resources) == 1
        cluster = resources[0]
        assert cluster.name == "aurora-prod"
        assert cluster.resource_type == "aws_rds_cluster"
        assert cluster.archetype == "database"
        assert cluster.properties["members"] == 2

    def test_handles_api_error(self) -> None:
        client = MagicMock()
        pag = MagicMock()
        pag.paginate.side_effect = Exception("AccessDenied")
        client.get_paginator.return_value = pag

        session = _mock_session({"rds": client})
        resources = _discover_rds(session, "eu-west-1")
        # Should not crash, but return empty or partial results
        assert isinstance(resources, list)


# ══════════════════════════════════════════════════════════════════════════════
#  ElastiCache Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverElastiCache:
    def test_discovers_replication_groups(self) -> None:
        client = MagicMock()
        rg_pag = MagicMock()
        rg_pag.paginate.return_value = [{"ReplicationGroups": [{
            "ReplicationGroupId": "prod-redis",
            "Description": "Production Redis cluster",
            "Status": "available",
            "NodeGroups": [{"a": 1}, {"b": 2}],
            "ClusterEnabled": True,
            "AutomaticFailover": "enabled",
            "MultiAZ": "enabled",
        }]}]
        cc_pag = MagicMock()
        cc_pag.paginate.return_value = [{"CacheClusters": []}]
        client.get_paginator.side_effect = lambda m: (
            rg_pag if m == "describe_replication_groups" else cc_pag
        )

        session = _mock_session({"elasticache": client})
        resources = _discover_elasticache(session, "eu-west-1")

        assert len(resources) == 1
        redis = resources[0]
        assert redis.name == "prod-redis"
        assert redis.archetype == "cache"
        assert redis.resource_type == "aws_elasticache_replication_group"
        assert any("redis_exporter" in n for n in redis.monitoring_notes)

    def test_discovers_standalone_memcached(self) -> None:
        client = MagicMock()
        rg_pag = MagicMock()
        rg_pag.paginate.return_value = [{"ReplicationGroups": []}]
        cc_pag = MagicMock()
        cc_pag.paginate.return_value = [{"CacheClusters": [{
            "CacheClusterId": "memcache-1",
            "Engine": "memcached",
            "EngineVersion": "1.6.22",
            "CacheNodeType": "cache.t3.micro",
            "NumCacheNodes": 2,
            "CacheClusterStatus": "available",
        }]}]
        client.get_paginator.side_effect = lambda m: (
            rg_pag if m == "describe_replication_groups" else cc_pag
        )

        session = _mock_session({"elasticache": client})
        resources = _discover_elasticache(session, "eu-west-1")

        assert len(resources) == 1
        mc = resources[0]
        assert mc.archetype == "cache"
        assert mc.properties["engine"] == "memcached"

    def test_skips_replication_group_members(self) -> None:
        """Standalone clusters that belong to a rep group should be skipped."""
        client = MagicMock()
        rg_pag = MagicMock()
        rg_pag.paginate.return_value = [{"ReplicationGroups": []}]
        cc_pag = MagicMock()
        cc_pag.paginate.return_value = [{"CacheClusters": [{
            "CacheClusterId": "prod-redis-001",
            "Engine": "redis",
            "ReplicationGroupId": "prod-redis",  # belongs to a group
            "CacheClusterStatus": "available",
        }]}]
        client.get_paginator.side_effect = lambda m: (
            rg_pag if m == "describe_replication_groups" else cc_pag
        )

        session = _mock_session({"elasticache": client})
        resources = _discover_elasticache(session, "eu-west-1")
        assert len(resources) == 0  # skipped because it's part of a rep group


# ══════════════════════════════════════════════════════════════════════════════
#  MSK Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverMSK:
    def test_discovers_msk_cluster(self) -> None:
        client = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"ClusterInfoList": [{
            "ClusterName": "events-kafka",
            "ClusterType": "PROVISIONED",
            "State": "ACTIVE",
            "Provisioned": {
                "NumberOfBrokerNodes": 3,
                "CurrentBrokerSoftwareInfo": {"KafkaVersion": "3.5.1"},
                "BrokerNodeGroupInfo": {"InstanceType": "kafka.m5.large"},
            },
        }]}]
        client.get_paginator.return_value = pag

        session = _mock_session({"kafka": client})
        resources = _discover_msk(session, "eu-west-1")

        assert len(resources) == 1
        kafka = resources[0]
        assert kafka.archetype == "message-queue"
        assert kafka.properties["broker_nodes"] == 3
        assert any("kafka_exporter" in n.lower() or "consumer lag" in n.lower() for n in kafka.monitoring_notes)


# ══════════════════════════════════════════════════════════════════════════════
#  SQS Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverSQS:
    def test_discovers_queues(self) -> None:
        client = MagicMock()
        client.list_queues.return_value = {
            "QueueUrls": [
                "https://sqs.eu-west-1.amazonaws.com/123456/orders-queue",
                "https://sqs.eu-west-1.amazonaws.com/123456/orders-queue-dlq",
            ],
        }

        session = _mock_session({"sqs": client})
        resources = _discover_sqs(session, "eu-west-1")

        assert len(resources) == 2
        names = {r.name for r in resources}
        assert "orders-queue" in names
        assert "orders-queue-dlq" in names

        dlq = next(r for r in resources if r.name == "orders-queue-dlq")
        assert dlq.properties["is_dead_letter_queue"] is True
        assert any("DLQ" in n for n in dlq.monitoring_notes)


# ══════════════════════════════════════════════════════════════════════════════
#  Lambda Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverLambda:
    def test_discovers_functions(self) -> None:
        client = _paginator_mock("list_functions", [
            {"Functions": [
                {
                    "FunctionName": "process-orders",
                    "Runtime": "python3.12",
                    "MemorySize": 256,
                    "Timeout": 30,
                    "Handler": "handler.main",
                    "LastModified": "2025-01-01T00:00:00Z",
                    "Architectures": ["arm64"],
                },
            ]},
        ])

        session = _mock_session({"lambda": client})
        resources = _discover_lambda(session, "eu-west-1")

        assert len(resources) == 1
        fn = resources[0]
        assert fn.name == "process-orders"
        assert fn.properties["runtime"] == "python3.12"
        assert fn.archetype == "custom-app"
        assert any("Errors" in n or "Duration" in n for n in fn.monitoring_notes)


# ══════════════════════════════════════════════════════════════════════════════
#  ECS Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverECS:
    def test_discovers_cluster_and_services(self) -> None:
        client = MagicMock()
        client.list_clusters.return_value = {"clusterArns": ["arn:aws:ecs:eu-west-1:123:cluster/prod"]}
        client.describe_clusters.return_value = {"clusters": [{
            "clusterName": "prod",
            "status": "ACTIVE",
            "runningTasksCount": 10,
            "pendingTasksCount": 0,
            "activeServicesCount": 3,
            "registeredContainerInstancesCount": 4,
            "capacityProviders": ["FARGATE"],
        }]}

        svc_pag = MagicMock()
        svc_pag.paginate.return_value = [{"serviceArns": [
            "arn:aws:ecs:eu-west-1:123:service/prod/api-svc",
        ]}]
        client.get_paginator.return_value = svc_pag
        client.describe_services.return_value = {"services": [{
            "serviceName": "api-svc",
            "status": "ACTIVE",
            "desiredCount": 3,
            "runningCount": 3,
            "launchType": "FARGATE",
            "taskDefinition": "arn:aws:ecs:eu-west-1:123:task-definition/api:5",
        }]}

        session = _mock_session({"ecs": client})
        resources = _discover_ecs(session, "eu-west-1")

        clusters = [r for r in resources if r.resource_type == "aws_ecs_cluster"]
        services = [r for r in resources if r.resource_type == "aws_ecs_service"]
        assert len(clusters) == 1
        assert len(services) == 1
        assert clusters[0].properties["running_tasks"] == 10
        assert services[0].properties["desired_count"] == 3


# ══════════════════════════════════════════════════════════════════════════════
#  Other Service Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverOpenSearch:
    def test_discovers_domains(self) -> None:
        client = MagicMock()
        client.list_domain_names.return_value = {"DomainNames": [{"DomainName": "logs"}]}
        client.describe_domains.return_value = {"DomainStatusList": [{
            "DomainName": "logs",
            "EngineVersion": "OpenSearch_2.11",
            "ClusterConfig": {"InstanceType": "r6g.large.search", "InstanceCount": 3},
            "Endpoint": "logs.es.amazonaws.com",
            "Processing": False,
        }]}

        session = _mock_session({"opensearch": client})
        resources = _discover_opensearch(session, "eu-west-1")

        assert len(resources) == 1
        assert resources[0].archetype == "search-engine"
        assert resources[0].properties["instance_count"] == 3


class TestDiscoverSNS:
    def test_discovers_topics(self) -> None:
        client = _paginator_mock("list_topics", [
            {"Topics": [
                {"TopicArn": "arn:aws:sns:eu-west-1:123:order-events"},
                {"TopicArn": "arn:aws:sns:eu-west-1:123:alerts"},
            ]},
        ])

        session = _mock_session({"sns": client})
        resources = _discover_sns(session, "eu-west-1")

        assert len(resources) == 2
        names = {r.name for r in resources}
        assert "order-events" in names
        assert "alerts" in names


class TestDiscoverDynamoDB:
    def test_discovers_tables(self) -> None:
        client = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"TableNames": ["users", "sessions"]}]
        client.get_paginator.return_value = pag
        client.describe_table.side_effect = lambda TableName: {"Table": {
            "TableName": TableName,
            "TableStatus": "ACTIVE",
            "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
            "ItemCount": 1000,
            "TableSizeBytes": 50000,
            "GlobalSecondaryIndexes": [],
        }}

        session = _mock_session({"dynamodb": client})
        resources = _discover_dynamodb(session, "eu-west-1")

        assert len(resources) == 2
        assert all(r.archetype == "database" for r in resources)
        assert any("ThrottledRequests" in n for r in resources for n in r.monitoring_notes)


class TestDiscoverEKS:
    def test_discovers_clusters(self) -> None:
        client = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"clusters": ["prod-cluster"]}]
        client.get_paginator.return_value = pag
        client.describe_cluster.return_value = {"cluster": {
            "name": "prod-cluster",
            "version": "1.28",
            "status": "ACTIVE",
            "endpoint": "https://XXXXX.eks.amazonaws.com",
            "platformVersion": "eks.7",
            "logging": {"clusterLogging": [
                {"types": ["api", "audit"], "enabled": True},
            ]},
        }}

        session = _mock_session({"eks": client})
        resources = _discover_eks(session, "eu-west-1")

        assert len(resources) == 1
        assert resources[0].name == "prod-cluster"
        assert resources[0].properties["version"] == "1.28"
        assert any("kube-state-metrics" in n for n in resources[0].monitoring_notes)


class TestDiscoverS3:
    def test_discovers_buckets_in_region(self) -> None:
        client = MagicMock()
        client.list_buckets.return_value = {"Buckets": [
            {"Name": "my-bucket", "CreationDate": "2024-01-01"},
            {"Name": "other-bucket", "CreationDate": "2024-06-01"},
        ]}
        client.get_bucket_location.side_effect = lambda Bucket: (
            {"LocationConstraint": "eu-west-1"}
        )

        session = _mock_session({"s3": client})
        resources = _discover_s3(session, "eu-west-1")

        assert len(resources) == 2
        assert all(r.resource_type == "aws_s3_bucket" for r in resources)

    def test_filters_by_region(self) -> None:
        client = MagicMock()
        client.list_buckets.return_value = {"Buckets": [
            {"Name": "eu-bucket"},
            {"Name": "us-bucket"},
        ]}

        def _mock_location(Bucket: str) -> dict:
            if Bucket == "eu-bucket":
                return {"LocationConstraint": "eu-west-1"}
            return {"LocationConstraint": "us-east-1"}

        client.get_bucket_location.side_effect = _mock_location

        session = _mock_session({"s3": client})
        resources = _discover_s3(session, "eu-west-1")

        assert len(resources) == 1
        assert resources[0].name == "eu-bucket"


# ══════════════════════════════════════════════════════════════════════════════
#  Integration — discover_aws_resources
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverAwsResources:
    @patch("k8s_observability_agent.aws._get_boto3_session")
    def test_runs_all_discoverers(self, mock_session_fn: MagicMock) -> None:
        session = MagicMock()
        session.region_name = "eu-west-1"

        # Make all clients return empty results
        empty_pag = MagicMock()
        empty_pag.paginate.return_value = [{}]
        empty_client = MagicMock()
        empty_client.get_paginator.return_value = empty_pag
        empty_client.list_queues.return_value = {"QueueUrls": []}
        empty_client.list_clusters.return_value = {"clusterArns": []}
        empty_client.list_domain_names.return_value = {"DomainNames": []}
        empty_client.list_buckets.return_value = {"Buckets": []}
        session.client.return_value = empty_client

        mock_session_fn.return_value = session

        resources, errors = discover_aws_resources(region="eu-west-1")
        assert isinstance(resources, list)
        assert isinstance(errors, list)

    @patch("k8s_observability_agent.aws._get_boto3_session")
    def test_filters_by_service(self, mock_session_fn: MagicMock) -> None:
        session = MagicMock()
        session.region_name = "eu-west-1"

        # Create RDS client with a result
        rds_client = MagicMock()
        inst_pag = MagicMock()
        inst_pag.paginate.return_value = [{"DBInstances": [{
            "DBInstanceIdentifier": "test-db",
            "Engine": "postgres",
            "DBInstanceStatus": "available",
            "Endpoint": {"Address": "test.rds", "Port": 5432},
            "DBSubnetGroup": {},
        }]}]
        cluster_pag = MagicMock()
        cluster_pag.paginate.return_value = [{"DBClusters": []}]
        rds_client.get_paginator.side_effect = lambda m: (
            inst_pag if m == "describe_db_instances" else cluster_pag
        )

        session.client.return_value = rds_client
        mock_session_fn.return_value = session

        resources, errors = discover_aws_resources(
            region="eu-west-1", services=["RDS"]
        )
        assert len(resources) == 1
        assert resources[0].name == "test-db"


# ══════════════════════════════════════════════════════════════════════════════
#  AwsDiscovery Model Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestAwsDiscoveryModel:
    def test_empty_discovery(self) -> None:
        d = AwsDiscovery()
        assert d.summary() == {}
        assert d.service_names == []

    def test_summary_counts_by_type(self) -> None:
        from k8s_observability_agent.models import IaCResource
        d = AwsDiscovery(
            resources=[
                IaCResource(source=IaCSource.TERRAFORM, resource_type="aws_rds_instance", name="a"),
                IaCResource(source=IaCSource.TERRAFORM, resource_type="aws_rds_instance", name="b"),
                IaCResource(source=IaCSource.TERRAFORM, resource_type="aws_sqs_queue", name="c"),
            ],
            region="eu-west-1",
        )
        assert d.summary() == {"aws_rds_instance": 2, "aws_sqs_queue": 1}

    def test_service_names(self) -> None:
        from k8s_observability_agent.models import IaCResource
        d = AwsDiscovery(
            resources=[
                IaCResource(source=IaCSource.TERRAFORM, resource_type="aws_rds_instance", name="a"),
                IaCResource(source=IaCSource.TERRAFORM, resource_type="aws_rds_cluster", name="b"),
                IaCResource(source=IaCSource.TERRAFORM, resource_type="aws_sqs_queue", name="c"),
                IaCResource(source=IaCSource.TERRAFORM, resource_type="aws_lambda_function", name="d"),
            ],
        )
        assert d.service_names == ["lambda", "rds", "sqs"]
