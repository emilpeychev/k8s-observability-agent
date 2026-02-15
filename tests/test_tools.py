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
        result = execute_tool(
            platform, "get_resource_detail", {"qualified_name": "default/Deployment/api"}
        )
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
        """Exporter-dependent alerts should be marked CONDITIONAL when no exporter is present."""
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
            telemetry=[],  # no exporter
            raw={},
        )
        from k8s_observability_agent.analyzer import build_platform

        platform = build_platform([deploy], ["db.yaml"], [])
        result = execute_tool(platform, "get_workload_insights", {})
        # All pg_* alerts require an exporter, so should be conditional
        assert "CONDITIONAL" in result
        assert "exporter" in result.lower()

    def test_workload_insights_replication_alerts_multi_replica(self) -> None:
        """With exporter present and replicas>1, replication alerts should be unconditional."""
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
            telemetry=["exporter:postgres_exporter"],
            raw={},
        )
        from k8s_observability_agent.analyzer import build_platform

        platform = build_platform([deploy], ["db.yaml"], [])
        result = execute_tool(platform, "get_workload_insights", {})
        assert "CONDITIONAL" not in result
        assert "PostgresReplicationLagHigh" in result

    def test_workload_insights_exporter_present(self) -> None:
        """When exporter sidecar is present, exporter-dependent alerts should be included normally."""
        from k8s_observability_agent.models import ContainerSpec

        pg_container = ContainerSpec(
            name="postgres",
            image="postgres:15",
            ports=[5432],
            archetype="database",
            archetype_display="PostgreSQL",
            archetype_confidence="high",
            archetype_score=0.95,
            archetype_match_source="image",
        )
        deploy = K8sResource(
            api_version="apps/v1",
            kind="Deployment",
            name="db",
            namespace="default",
            replicas=1,
            source_file="db.yaml",
            containers=[pg_container],
            telemetry=["exporter:postgres_exporter"],
            raw={},
        )
        from k8s_observability_agent.analyzer import build_platform

        platform = build_platform([deploy], ["db.yaml"], [])
        result = execute_tool(platform, "get_workload_insights", {})
        assert "PostgresTooManyConnections" in result
        # Replication alerts should still be conditional (replicas=1)
        assert "CONDITIONAL" in result  # for replication alerts
        # But the exporter-only alerts should NOT have the CONDITIONAL marker
        lines = result.split("\n")
        for i, line in enumerate(lines):
            if "PostgresTooManyConnections" in line:
                # Check the next few lines for CONDITIONAL
                nearby = "\n".join(lines[i : i + 3])
                assert "CONDITIONAL" not in nearby
                break

    def test_conditional_includes_specific_exporter_name(self) -> None:
        """CONDITIONAL messages should name the specific exporter to deploy."""
        from k8s_observability_agent.models import ContainerSpec

        pg_container = ContainerSpec(
            name="postgres",
            image="postgres:15",
            ports=[5432],
            archetype="database",
            archetype_display="PostgreSQL",
            archetype_confidence="high",
            archetype_score=0.95,
            archetype_match_source="image",
        )
        deploy = K8sResource(
            api_version="apps/v1",
            kind="Deployment",
            name="db",
            namespace="default",
            replicas=1,
            source_file="db.yaml",
            containers=[pg_container],
            telemetry=[],
            raw={},
        )
        from k8s_observability_agent.analyzer import build_platform

        platform = build_platform([deploy], ["db.yaml"], [])
        result = execute_tool(platform, "get_workload_insights", {})
        # Should mention the specific exporter name in remediation
        assert "deploy postgres_exporter sidecar" in result

    def test_observability_readiness_verdict_ready(self) -> None:
        """Workload with exporter + scrape annotations should be READY."""
        from k8s_observability_agent.models import ContainerSpec

        container = ContainerSpec(
            name="app",
            image="nginx:latest",
            ports=[80],
            archetype="web-server",
            archetype_display="Nginx",
            archetype_confidence="high",
            archetype_score=0.85,
            archetype_match_source="image",
        )
        deploy = K8sResource(
            api_version="apps/v1",
            kind="Deployment",
            name="web",
            namespace="default",
            replicas=1,
            source_file="web.yaml",
            containers=[container],
            telemetry=["exporter:nginx_exporter", "scrape_annotations"],
            raw={},
        )
        from k8s_observability_agent.analyzer import build_platform

        platform = build_platform([deploy], ["web.yaml"], [])
        result = execute_tool(platform, "get_workload_insights", {})
        assert "READY" in result
        assert "exporter present + scrape path configured" in result

    def test_observability_readiness_verdict_not_ready(self) -> None:
        """Workload with no telemetry should be NOT READY."""
        from k8s_observability_agent.models import ContainerSpec

        container = ContainerSpec(
            name="app",
            image="myapp:latest",
            ports=[8080],
            archetype="custom-app",
            archetype_display="Custom Application",
            archetype_confidence="low",
            archetype_score=0.0,
            archetype_match_source="none",
        )
        deploy = K8sResource(
            api_version="apps/v1",
            kind="Deployment",
            name="app",
            namespace="default",
            replicas=1,
            source_file="app.yaml",
            containers=[container],
            telemetry=[],
            raw={},
        )
        from k8s_observability_agent.analyzer import build_platform

        platform = build_platform([deploy], ["app.yaml"], [])
        result = execute_tool(platform, "get_workload_insights", {})
        assert "NOT READY" in result
        assert "no metrics exposure detected" in result

    def test_platform_summary_includes_readiness(self) -> None:
        """Platform summary should include observability readiness counts."""
        from k8s_observability_agent.models import ContainerSpec

        container = ContainerSpec(
            name="app",
            image="redis:7",
            ports=[6379],
            archetype="cache",
            archetype_display="Redis",
            archetype_confidence="high",
            archetype_score=0.9,
            archetype_match_source="image",
        )
        deploy = K8sResource(
            api_version="apps/v1",
            kind="Deployment",
            name="cache",
            namespace="default",
            replicas=1,
            source_file="cache.yaml",
            containers=[container],
            telemetry=[],
            raw={},
        )
        from k8s_observability_agent.analyzer import build_platform

        platform = build_platform([deploy], ["cache.yaml"], [])
        result = execute_tool(platform, "get_platform_summary", {})
        assert "Observability Readiness:" in result
        assert "NOT READY:" in result
