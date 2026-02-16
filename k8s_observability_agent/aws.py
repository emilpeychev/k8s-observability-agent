"""Live AWS resource discovery via boto3.

Connects to an AWS account and discovers running infrastructure resources
(RDS, ElastiCache, MSK, ECS, Lambda, SQS, OpenSearch, etc.), maps them to
observability archetypes, and returns structured data for the agent.

Requires ``boto3`` — installed as an optional dependency.
"""

from __future__ import annotations

import logging
from typing import Any

from k8s_observability_agent.models import IaCResource, IaCSource

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  ARCHETYPE MAPPINGS
# ══════════════════════════════════════════════════════════════════════════════

_RDS_ENGINE_ARCHETYPES: dict[str, tuple[str, list[str]]] = {
    "postgres": ("database", [
        "Deploy postgres_exporter sidecar or use CloudWatch metrics",
        "Monitor replication lag, connections, IOPS, disk usage",
        "Import Grafana dashboard 9628",
    ]),
    "mysql": ("database", [
        "Deploy mysqld_exporter sidecar or use CloudWatch metrics",
        "Monitor replication lag, connections, IOPS, slow queries",
        "Import Grafana dashboard 7362",
    ]),
    "mariadb": ("database", [
        "Deploy mysqld_exporter sidecar or use CloudWatch metrics",
        "Monitor replication lag, connections, IOPS",
        "Import Grafana dashboard 7362",
    ]),
    "aurora-postgresql": ("database", [
        "Use CloudWatch or postgres_exporter",
        "Monitor replication lag, connections, Aurora replicas, IOPS",
        "Import Grafana dashboard 9628",
    ]),
    "aurora-mysql": ("database", [
        "Use CloudWatch or mysqld_exporter",
        "Monitor replication lag, connections, Aurora replicas",
        "Import Grafana dashboard 7362",
    ]),
    "oracle-ee": ("database", [
        "Use CloudWatch or oracledb_exporter",
        "Monitor tablespace, sessions, wait events",
    ]),
    "sqlserver-ee": ("database", [
        "Use CloudWatch or mssql_exporter",
        "Monitor deadlocks, batch requests, buffer cache hit ratio",
    ]),
}

_ELASTICACHE_ENGINE_ARCHETYPES: dict[str, tuple[str, list[str]]] = {
    "redis": ("cache", [
        "Deploy redis_exporter or use CloudWatch",
        "Monitor hit rate, evictions, memory usage, connections",
        "Import Grafana dashboard 11835",
    ]),
    "memcached": ("cache", [
        "Deploy memcached_exporter or use CloudWatch",
        "Monitor hit rate, evictions, curr_items, connections",
    ]),
    "valkey": ("cache", [
        "Deploy redis_exporter (Valkey-compatible) or use CloudWatch",
        "Monitor hit rate, evictions, memory usage",
        "Import Grafana dashboard 11835",
    ]),
}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPER — safe boto3 import
# ══════════════════════════════════════════════════════════════════════════════

def _get_boto3_session(
    region: str = "",
    profile: str = "",
) -> Any:
    """Create a boto3 session. Raises ImportError if boto3 is not installed."""
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError:
        raise ImportError(
            "boto3 is required for AWS discovery. "
            "Install it with: pip install boto3"
        )

    kwargs: dict[str, str] = {}
    if region:
        kwargs["region_name"] = region
    if profile:
        kwargs["profile_name"] = profile
    return boto3.Session(**kwargs)


# ══════════════════════════════════════════════════════════════════════════════
#  INDIVIDUAL SERVICE DISCOVERERS
# ══════════════════════════════════════════════════════════════════════════════


def _discover_rds(session: Any, region: str) -> list[IaCResource]:
    """Discover RDS instances and clusters."""
    resources: list[IaCResource] = []
    rds = session.client("rds", region_name=region)

    # ── DB Instances ──────────────────────────────────────────────────
    try:
        paginator = rds.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            for db in page["DBInstances"]:
                engine = db.get("Engine", "unknown")
                engine_key = engine.split("-")[0] if "-" in engine else engine
                archetype, notes = _RDS_ENGINE_ARCHETYPES.get(
                    engine, _RDS_ENGINE_ARCHETYPES.get(engine_key, ("database", [
                        f"Monitor {engine} via CloudWatch",
                        "Monitor connections, IOPS, storage",
                    ]))
                )

                resources.append(IaCResource(
                    source=IaCSource.TERRAFORM,  # re-use enum; identified as "aws" provider
                    source_file=f"aws:{region}",
                    resource_type="aws_rds_instance",
                    name=db.get("DBInstanceIdentifier", ""),
                    provider="aws",
                    properties={
                        "engine": engine,
                        "engine_version": db.get("EngineVersion", ""),
                        "instance_class": db.get("DBInstanceClass", ""),
                        "storage_gb": db.get("AllocatedStorage", 0),
                        "multi_az": db.get("MultiAZ", False),
                        "status": db.get("DBInstanceStatus", ""),
                        "endpoint": db.get("Endpoint", {}).get("Address", ""),
                        "port": db.get("Endpoint", {}).get("Port", 0),
                        "vpc_id": db.get("DBSubnetGroup", {}).get("VpcId", ""),
                    },
                    archetype=archetype,
                    monitoring_notes=list(notes),
                ))
    except Exception as exc:
        logger.warning("Failed to list RDS instances in %s: %s", region, exc)

    # ── DB Clusters (Aurora) ──────────────────────────────────────────
    try:
        paginator = rds.get_paginator("describe_db_clusters")
        for page in paginator.paginate():
            for cluster in page["DBClusters"]:
                engine = cluster.get("Engine", "unknown")
                archetype, notes = _RDS_ENGINE_ARCHETYPES.get(
                    engine, ("database", [f"Monitor {engine} via CloudWatch"])
                )

                resources.append(IaCResource(
                    source=IaCSource.TERRAFORM,
                    source_file=f"aws:{region}",
                    resource_type="aws_rds_cluster",
                    name=cluster.get("DBClusterIdentifier", ""),
                    provider="aws",
                    properties={
                        "engine": engine,
                        "engine_version": cluster.get("EngineVersion", ""),
                        "members": len(cluster.get("DBClusterMembers", [])),
                        "status": cluster.get("Status", ""),
                        "endpoint": cluster.get("Endpoint", ""),
                        "reader_endpoint": cluster.get("ReaderEndpoint", ""),
                        "port": cluster.get("Port", 0),
                    },
                    archetype=archetype,
                    monitoring_notes=list(notes),
                ))
    except Exception as exc:
        logger.warning("Failed to list RDS clusters in %s: %s", region, exc)

    return resources


def _discover_elasticache(session: Any, region: str) -> list[IaCResource]:
    """Discover ElastiCache clusters and replication groups."""
    resources: list[IaCResource] = []
    ec = session.client("elasticache", region_name=region)

    # ── Replication Groups (Redis/Valkey) ─────────────────────────────
    try:
        paginator = ec.get_paginator("describe_replication_groups")
        for page in paginator.paginate():
            for rg in page["ReplicationGroups"]:
                # Determine engine from the members or description
                description = rg.get("Description", "").lower()
                if "valkey" in description:
                    engine = "valkey"
                else:
                    engine = "redis"

                archetype, notes = _ELASTICACHE_ENGINE_ARCHETYPES.get(
                    engine, ("cache", [f"Monitor {engine} via CloudWatch"])
                )

                node_groups = rg.get("NodeGroups", [])
                resources.append(IaCResource(
                    source=IaCSource.TERRAFORM,
                    source_file=f"aws:{region}",
                    resource_type="aws_elasticache_replication_group",
                    name=rg.get("ReplicationGroupId", ""),
                    provider="aws",
                    properties={
                        "engine": engine,
                        "status": rg.get("Status", ""),
                        "num_node_groups": len(node_groups),
                        "cluster_mode": rg.get("ClusterEnabled", False),
                        "automatic_failover": rg.get("AutomaticFailover", ""),
                        "multi_az": rg.get("MultiAZ", ""),
                    },
                    archetype=archetype,
                    monitoring_notes=list(notes),
                ))
    except Exception as exc:
        logger.warning("Failed to list ElastiCache replication groups in %s: %s", region, exc)

    # ── Standalone clusters (Memcached, standalone Redis) ─────────────
    try:
        paginator = ec.get_paginator("describe_cache_clusters")
        for page in paginator.paginate():
            for cluster in page["CacheClusters"]:
                # Skip if part of a replication group (already captured above)
                if cluster.get("ReplicationGroupId"):
                    continue

                engine = cluster.get("Engine", "unknown")
                archetype, notes = _ELASTICACHE_ENGINE_ARCHETYPES.get(
                    engine, ("cache", [f"Monitor {engine} via CloudWatch"])
                )

                resources.append(IaCResource(
                    source=IaCSource.TERRAFORM,
                    source_file=f"aws:{region}",
                    resource_type="aws_elasticache_cluster",
                    name=cluster.get("CacheClusterId", ""),
                    provider="aws",
                    properties={
                        "engine": engine,
                        "engine_version": cluster.get("EngineVersion", ""),
                        "node_type": cluster.get("CacheNodeType", ""),
                        "num_nodes": cluster.get("NumCacheNodes", 0),
                        "status": cluster.get("CacheClusterStatus", ""),
                    },
                    archetype=archetype,
                    monitoring_notes=list(notes),
                ))
    except Exception as exc:
        logger.warning("Failed to list ElastiCache clusters in %s: %s", region, exc)

    return resources


def _discover_msk(session: Any, region: str) -> list[IaCResource]:
    """Discover Amazon MSK (Managed Streaming for Apache Kafka) clusters."""
    resources: list[IaCResource] = []
    try:
        kafka = session.client("kafka", region_name=region)
        paginator = kafka.get_paginator("list_clusters_v2")
        for page in paginator.paginate():
            for cluster in page.get("ClusterInfoList", []):
                provisioned = cluster.get("Provisioned", {})
                serverless = cluster.get("Serverless", {})

                properties: dict[str, Any] = {
                    "cluster_type": cluster.get("ClusterType", ""),
                    "state": cluster.get("State", ""),
                    "kafka_version": provisioned.get("CurrentBrokerSoftwareInfo", {}).get(
                        "KafkaVersion", ""
                    ),
                }
                if provisioned:
                    properties["broker_nodes"] = provisioned.get("NumberOfBrokerNodes", 0)
                    properties["instance_type"] = (
                        provisioned.get("BrokerNodeGroupInfo", {}).get("InstanceType", "")
                    )

                resources.append(IaCResource(
                    source=IaCSource.TERRAFORM,
                    source_file=f"aws:{region}",
                    resource_type="aws_msk_cluster",
                    name=cluster.get("ClusterName", ""),
                    provider="aws",
                    properties=properties,
                    archetype="message-queue",
                    monitoring_notes=[
                        "Deploy kafka_exporter or use CloudWatch",
                        "Monitor consumer lag, partition count, under-replicated partitions",
                        "Import Grafana dashboard 7589",
                    ],
                ))
    except Exception as exc:
        logger.warning("Failed to list MSK clusters in %s: %s", region, exc)

    return resources


def _discover_sqs(session: Any, region: str) -> list[IaCResource]:
    """Discover SQS queues."""
    resources: list[IaCResource] = []
    try:
        sqs = session.client("sqs", region_name=region)
        resp = sqs.list_queues()
        for url in resp.get("QueueUrls", []):
            # Extract queue name from URL
            name = url.rsplit("/", 1)[-1]
            is_dlq = name.endswith("-dlq") or name.endswith("-dead-letter")

            resources.append(IaCResource(
                source=IaCSource.TERRAFORM,
                source_file=f"aws:{region}",
                resource_type="aws_sqs_queue",
                name=name,
                provider="aws",
                properties={
                    "url": url,
                    "is_dead_letter_queue": is_dlq,
                },
                archetype="message-queue",
                monitoring_notes=[
                    "Monitor via CloudWatch",
                    "Alert on ApproximateNumberOfMessagesVisible (queue depth)",
                    "Alert on ApproximateAgeOfOldestMessage",
                    *(["Alert on DLQ receiving messages"] if is_dlq else []),
                ],
            ))
    except Exception as exc:
        logger.warning("Failed to list SQS queues in %s: %s", region, exc)

    return resources


def _discover_lambda(session: Any, region: str) -> list[IaCResource]:
    """Discover Lambda functions."""
    resources: list[IaCResource] = []
    try:
        lam = session.client("lambda", region_name=region)
        paginator = lam.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page["Functions"]:
                resources.append(IaCResource(
                    source=IaCSource.TERRAFORM,
                    source_file=f"aws:{region}",
                    resource_type="aws_lambda_function",
                    name=fn.get("FunctionName", ""),
                    provider="aws",
                    properties={
                        "runtime": fn.get("Runtime", ""),
                        "memory_mb": fn.get("MemorySize", 0),
                        "timeout_seconds": fn.get("Timeout", 0),
                        "handler": fn.get("Handler", ""),
                        "last_modified": fn.get("LastModified", ""),
                        "architectures": fn.get("Architectures", []),
                    },
                    archetype="custom-app",
                    monitoring_notes=[
                        "Monitor via CloudWatch",
                        "Alert on Errors, Throttles, Duration",
                        "Monitor ConcurrentExecutions and IteratorAge (for stream-based)",
                    ],
                ))
    except Exception as exc:
        logger.warning("Failed to list Lambda functions in %s: %s", region, exc)

    return resources


def _discover_ecs(session: Any, region: str) -> list[IaCResource]:
    """Discover ECS clusters and services."""
    resources: list[IaCResource] = []
    try:
        ecs = session.client("ecs", region_name=region)
        cluster_arns = ecs.list_clusters().get("clusterArns", [])
        if not cluster_arns:
            return resources

        clusters_resp = ecs.describe_clusters(clusters=cluster_arns, include=["STATISTICS"])
        for cluster in clusters_resp.get("clusters", []):
            cluster_name = cluster.get("clusterName", "")

            resources.append(IaCResource(
                source=IaCSource.TERRAFORM,
                source_file=f"aws:{region}",
                resource_type="aws_ecs_cluster",
                name=cluster_name,
                provider="aws",
                properties={
                    "status": cluster.get("status", ""),
                    "running_tasks": cluster.get("runningTasksCount", 0),
                    "pending_tasks": cluster.get("pendingTasksCount", 0),
                    "active_services": cluster.get("activeServicesCount", 0),
                    "registered_instances": cluster.get("registeredContainerInstancesCount", 0),
                    "capacity_providers": cluster.get("capacityProviders", []),
                },
                archetype="custom-app",
                monitoring_notes=[
                    "Enable CloudWatch Container Insights",
                    "Monitor task count, CPU, memory utilisation",
                    "Alert on service deployment failures",
                ],
            ))

            # Discover services in this cluster
            try:
                svc_paginator = ecs.get_paginator("list_services")
                service_arns: list[str] = []
                for svc_page in svc_paginator.paginate(cluster=cluster_name):
                    service_arns.extend(svc_page.get("serviceArns", []))

                # describe_services supports max 10 at a time
                for i in range(0, len(service_arns), 10):
                    batch = service_arns[i : i + 10]
                    svcs_resp = ecs.describe_services(cluster=cluster_name, services=batch)
                    for svc in svcs_resp.get("services", []):
                        resources.append(IaCResource(
                            source=IaCSource.TERRAFORM,
                            source_file=f"aws:{region}",
                            resource_type="aws_ecs_service",
                            name=f"{cluster_name}/{svc.get('serviceName', '')}",
                            provider="aws",
                            properties={
                                "status": svc.get("status", ""),
                                "desired_count": svc.get("desiredCount", 0),
                                "running_count": svc.get("runningCount", 0),
                                "launch_type": svc.get("launchType", ""),
                                "task_definition": svc.get("taskDefinition", "").rsplit("/", 1)[-1],
                            },
                            archetype="custom-app",
                            monitoring_notes=[
                                "Monitor desired vs running task count",
                                "Alert on deployment rollbacks and OOM kills",
                            ],
                        ))
            except Exception as exc:
                logger.warning("Failed to list ECS services in cluster %s: %s", cluster_name, exc)

    except Exception as exc:
        logger.warning("Failed to list ECS clusters in %s: %s", region, exc)

    return resources


def _discover_opensearch(session: Any, region: str) -> list[IaCResource]:
    """Discover OpenSearch (ElasticSearch) domains."""
    resources: list[IaCResource] = []
    try:
        client = session.client("opensearch", region_name=region)
        domains = client.list_domain_names().get("DomainNames", [])

        if domains:
            names = [d["DomainName"] for d in domains]
            details = client.describe_domains(DomainNames=names).get("DomainStatusList", [])
            for domain in details:
                resources.append(IaCResource(
                    source=IaCSource.TERRAFORM,
                    source_file=f"aws:{region}",
                    resource_type="aws_opensearch_domain",
                    name=domain.get("DomainName", ""),
                    provider="aws",
                    properties={
                        "engine_version": domain.get("EngineVersion", ""),
                        "instance_type": (
                            domain.get("ClusterConfig", {}).get("InstanceType", "")
                        ),
                        "instance_count": (
                            domain.get("ClusterConfig", {}).get("InstanceCount", 0)
                        ),
                        "endpoint": domain.get("Endpoint", ""),
                        "processing": domain.get("Processing", False),
                    },
                    archetype="search-engine",
                    monitoring_notes=[
                        "Deploy elasticsearch_exporter or use CloudWatch",
                        "Monitor cluster health, indexing rate, search latency",
                        "Alert on JVM memory pressure, disk usage",
                        "Import Grafana dashboard 4358",
                    ],
                ))
    except Exception as exc:
        logger.warning("Failed to list OpenSearch domains in %s: %s", region, exc)

    return resources


def _discover_sns(session: Any, region: str) -> list[IaCResource]:
    """Discover SNS topics."""
    resources: list[IaCResource] = []
    try:
        sns = session.client("sns", region_name=region)
        paginator = sns.get_paginator("list_topics")
        for page in paginator.paginate():
            for topic in page.get("Topics", []):
                arn = topic.get("TopicArn", "")
                name = arn.rsplit(":", 1)[-1] if arn else ""

                resources.append(IaCResource(
                    source=IaCSource.TERRAFORM,
                    source_file=f"aws:{region}",
                    resource_type="aws_sns_topic",
                    name=name,
                    provider="aws",
                    properties={"arn": arn},
                    archetype="message-queue",
                    monitoring_notes=[
                        "Monitor via CloudWatch",
                        "Alert on NumberOfNotificationsFailed",
                        "Monitor NumberOfMessagesPublished",
                    ],
                ))
    except Exception as exc:
        logger.warning("Failed to list SNS topics in %s: %s", region, exc)

    return resources


def _discover_dynamodb(session: Any, region: str) -> list[IaCResource]:
    """Discover DynamoDB tables."""
    resources: list[IaCResource] = []
    try:
        ddb = session.client("dynamodb", region_name=region)
        paginator = ddb.get_paginator("list_tables")
        for page in paginator.paginate():
            for table_name in page.get("TableNames", []):
                try:
                    desc = ddb.describe_table(TableName=table_name)["Table"]
                    billing = desc.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")

                    resources.append(IaCResource(
                        source=IaCSource.TERRAFORM,
                        source_file=f"aws:{region}",
                        resource_type="aws_dynamodb_table",
                        name=table_name,
                        provider="aws",
                        properties={
                            "status": desc.get("TableStatus", ""),
                            "billing_mode": billing,
                            "item_count": desc.get("ItemCount", 0),
                            "size_bytes": desc.get("TableSizeBytes", 0),
                            "gsi_count": len(desc.get("GlobalSecondaryIndexes", [])),
                        },
                        archetype="database",
                        monitoring_notes=[
                            "Monitor via CloudWatch",
                            "Alert on ConsumedReadCapacityUnits / ConsumedWriteCapacityUnits",
                            "Alert on ThrottledRequests and SystemErrors",
                            "Monitor SuccessfulRequestLatency",
                        ],
                    ))
                except Exception as exc:
                    logger.warning("Failed to describe DynamoDB table %s: %s", table_name, exc)
    except Exception as exc:
        logger.warning("Failed to list DynamoDB tables in %s: %s", region, exc)

    return resources


def _discover_eks(session: Any, region: str) -> list[IaCResource]:
    """Discover EKS clusters."""
    resources: list[IaCResource] = []
    try:
        eks = session.client("eks", region_name=region)
        paginator = eks.get_paginator("list_clusters")
        for page in paginator.paginate():
            for cluster_name in page.get("clusters", []):
                try:
                    desc = eks.describe_cluster(name=cluster_name)["cluster"]
                    resources.append(IaCResource(
                        source=IaCSource.TERRAFORM,
                        source_file=f"aws:{region}",
                        resource_type="aws_eks_cluster",
                        name=cluster_name,
                        provider="aws",
                        properties={
                            "version": desc.get("version", ""),
                            "status": desc.get("status", ""),
                            "endpoint": desc.get("endpoint", ""),
                            "platform_version": desc.get("platformVersion", ""),
                            "logging": [
                                lt["types"]
                                for lt in desc.get("logging", {}).get("clusterLogging", [])
                                if lt.get("enabled")
                            ],
                        },
                        archetype="custom-app",
                        monitoring_notes=[
                            "Deploy kube-state-metrics, node-exporter, and metrics-server",
                            "Monitor API server latency, etcd health, node conditions",
                            "Alert on node NotReady, pod scheduling failures",
                            "Import Grafana dashboard 15520 (K8s cluster monitoring)",
                        ],
                    ))
                except Exception as exc:
                    logger.warning("Failed to describe EKS cluster %s: %s", cluster_name, exc)
    except Exception as exc:
        logger.warning("Failed to list EKS clusters in %s: %s", region, exc)

    return resources


def _discover_s3(session: Any, region: str) -> list[IaCResource]:
    """Discover S3 buckets (region-filtered)."""
    resources: list[IaCResource] = []
    try:
        s3 = session.client("s3", region_name=region)
        resp = s3.list_buckets()
        for bucket in resp.get("Buckets", []):
            name = bucket.get("Name", "")
            # Filter buckets by region
            try:
                loc = s3.get_bucket_location(Bucket=name)
                bucket_region = loc.get("LocationConstraint") or "us-east-1"
                if bucket_region != region:
                    continue
            except Exception:
                continue

            resources.append(IaCResource(
                source=IaCSource.TERRAFORM,
                source_file=f"aws:{region}",
                resource_type="aws_s3_bucket",
                name=name,
                provider="aws",
                properties={
                    "creation_date": str(bucket.get("CreationDate", "")),
                },
                archetype="custom-app",
                monitoring_notes=[
                    "Optional CloudWatch metrics (request-level)",
                    "Monitor 4xx/5xx errors, latency if bucket is heavily used",
                ],
            ))
    except Exception as exc:
        logger.warning("Failed to list S3 buckets in %s: %s", region, exc)

    return resources


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

# All service discoverers in execution order
_DISCOVERERS: list[tuple[str, Any]] = [
    ("RDS", _discover_rds),
    ("ElastiCache", _discover_elasticache),
    ("MSK", _discover_msk),
    ("SQS", _discover_sqs),
    ("SNS", _discover_sns),
    ("Lambda", _discover_lambda),
    ("ECS", _discover_ecs),
    ("EKS", _discover_eks),
    ("OpenSearch", _discover_opensearch),
    ("DynamoDB", _discover_dynamodb),
    ("S3", _discover_s3),
]


def discover_aws_resources(
    region: str = "",
    profile: str = "",
    services: list[str] | None = None,
) -> tuple[list[IaCResource], list[str]]:
    """Discover live AWS resources and map them to archetypes.

    Args:
        region: AWS region to scan (e.g. "eu-west-1"). Uses default if empty.
        profile: AWS CLI profile name. Uses default if empty.
        services: Optional list of service names to scan (e.g. ["rds", "elasticache"]).
                  Scans all services if None.

    Returns:
        Tuple of (resources, errors).
    """
    session = _get_boto3_session(region=region, profile=profile)

    # Resolve region from session if not explicitly provided
    effective_region = region or session.region_name or "us-east-1"

    all_resources: list[IaCResource] = []
    errors: list[str] = []

    # Filter discoverers if services list is specified
    discoverers = _DISCOVERERS
    if services:
        allowed = {s.lower() for s in services}
        discoverers = [(name, fn) for name, fn in _DISCOVERERS if name.lower() in allowed]

    for service_name, discover_fn in discoverers:
        try:
            logger.info("AWS discovery: scanning %s in %s …", service_name, effective_region)
            found = discover_fn(session, effective_region)
            all_resources.extend(found)
            if found:
                logger.info("  %s: found %d resources", service_name, len(found))
        except Exception as exc:
            msg = f"AWS {service_name} scan error in {effective_region}: {exc}"
            errors.append(msg)
            logger.warning(msg)

    logger.info(
        "AWS discovery complete: %d resources across %d services in %s",
        len(all_resources),
        len(discoverers),
        effective_region,
    )
    return all_resources, errors


def discover_aws_multi_region(
    regions: list[str],
    profile: str = "",
    services: list[str] | None = None,
) -> tuple[list[IaCResource], list[str]]:
    """Discover AWS resources across multiple regions.

    Args:
        regions: List of AWS regions to scan.
        profile: AWS CLI profile name.
        services: Optional list of service names to scan.

    Returns:
        Tuple of (resources, errors).
    """
    all_resources: list[IaCResource] = []
    all_errors: list[str] = []

    for region in regions:
        resources, errors = discover_aws_resources(
            region=region, profile=profile, services=services
        )
        all_resources.extend(resources)
        all_errors.extend(errors)

    return all_resources, all_errors
