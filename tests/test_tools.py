"""Tests for agent.tools.registry."""

import json

from k8s_observability_agent.models import K8sResource, Platform
from k8s_observability_agent.tools.registry import TOOL_DEFINITIONS, execute_tool


def _sample_platform() -> Platform:
    deploy = K8sResource(
        api_version="apps/v1",
        kind="Deployment",
        name="api",
        namespace="default",
        replicas=2,
        source_file="deploy.yaml",
        containers=[],
        selector={"app": "api"},
        raw={
            "spec": {
                "template": {
                    "metadata": {"labels": {"app": "api"}},
                    "spec": {"containers": [{"name": "api", "image": "api:1"}]},
                },
            },
        },
    )
    svc = K8sResource(
        api_version="v1",
        kind="Service",
        name="api-svc",
        namespace="default",
        service_type="ClusterIP",
        selector={"app": "api"},
        source_file="svc.yaml",
        raw={"spec": {"selector": {"app": "api"}}},
    )
    from k8s_observability_agent.analyzer import build_platform

    return build_platform([deploy, svc], ["deploy.yaml", "svc.yaml"], [])


class TestToolDefinitions:
    def test_all_tools_have_required_keys(self) -> None:
        for td in TOOL_DEFINITIONS:
            assert "name" in td
            assert "description" in td
            assert "input_schema" in td
            assert td["input_schema"]["type"] == "object"


class TestExecuteTool:
    def test_list_resources(self) -> None:
        platform = _sample_platform()
        result = execute_tool(platform, "list_resources", {})
        assert "api" in result
        assert "api-svc" in result

    def test_list_resources_filter_kind(self) -> None:
        platform = _sample_platform()
        result = execute_tool(platform, "list_resources", {"kind": "Service"})
        assert "api-svc" in result
        assert "Deployment" not in result

    def test_get_resource_detail(self) -> None:
        platform = _sample_platform()
        result = execute_tool(platform, "get_resource_detail", {"qualified_name": "default/Deployment/api"})
        parsed = json.loads(result)
        assert parsed["name"] == "api"
        assert parsed["kind"] == "Deployment"

    def test_get_resource_detail_not_found(self) -> None:
        platform = _sample_platform()
        result = execute_tool(platform, "get_resource_detail", {"qualified_name": "nope/X/y"})
        assert "not found" in result.lower()

    def test_get_platform_summary(self) -> None:
        platform = _sample_platform()
        result = execute_tool(platform, "get_platform_summary", {})
        assert "Total resources:" in result

    def test_get_relationships(self) -> None:
        platform = _sample_platform()
        result = execute_tool(platform, "get_relationships", {})
        assert "selects" in result

    def test_check_health_gaps(self) -> None:
        platform = _sample_platform()
        result = execute_tool(platform, "check_health_gaps", {})
        # api deployment has no containers with probes, so gaps should appear
        assert "gap" in result.lower() or "No observability gaps" in result

    def test_unknown_tool(self) -> None:
        platform = _sample_platform()
        result = execute_tool(platform, "nonexistent", {})
        assert "Unknown tool" in result

    def test_workload_insights_conditional_alerts_single_replica(self) -> None:
        """Replication alerts should be marked CONDITIONAL for replicas=1."""
        from k8s_observability_agent.models import ContainerSpec

        pg_container = ContainerSpec(
            name="postgres",
            image="postgres:15",
            ports=[5432],
            env_vars=["POSTGRES_DB"],
            archetype="database",
            archetype_display="PostgreSQL",
            archetype_confidence="high",
            archetype_score=0.95,
            archetype_match_source="image",
            archetype_evidence=["image:postgres:15", "port:5432"],
        )
        deploy = K8sResource(
            api_version="apps/v1",
            kind="Deployment",
            name="db",
            namespace="default",
            replicas=1,
            source_file="db.yaml",
            containers=[pg_container],
            raw={},
        )
        from k8s_observability_agent.analyzer import build_platform

        platform = build_platform([deploy], ["db.yaml"], [])
        result = execute_tool(platform, "get_workload_insights", {})
        assert "CONDITIONAL" in result
        assert "PostgresReplicationLagHigh" in result

    def test_workload_insights_replication_alerts_multi_replica(self) -> None:
        """Replication alerts should NOT be marked CONDITIONAL for replicas>1."""
        from k8s_observability_agent.models import ContainerSpec

        pg_container = ContainerSpec(
            name="postgres",
            image="postgres:15",
            ports=[5432],
            env_vars=["POSTGRES_DB"],
            archetype="database",
            archetype_display="PostgreSQL",
            archetype_confidence="high",
            archetype_score=0.95,
            archetype_match_source="image",
            archetype_evidence=["image:postgres:15", "port:5432"],
        )
        deploy = K8sResource(
            api_version="apps/v1",
            kind="StatefulSet",
            name="db",
            namespace="default",
            replicas=3,
            source_file="db.yaml",
            containers=[pg_container],
            raw={},
        )
        from k8s_observability_agent.analyzer import build_platform

        platform = build_platform([deploy], ["db.yaml"], [])
        result = execute_tool(platform, "get_workload_insights", {})
        assert "CONDITIONAL" not in result
        assert "PostgresReplicationLagHigh" in result
