"""Tests for k8s_observability_agent.tools.live module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from k8s_observability_agent.cluster import ClusterClient, CommandResult
from k8s_observability_agent.grafana import GrafanaClient
from k8s_observability_agent.prometheus import PrometheusClient
from k8s_observability_agent.tools.live import LIVE_TOOL_DEFINITIONS, LiveToolExecutor


# ═══════════════════════════════════════════════════════════════════════════
# LIVE_TOOL_DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════


class TestLiveToolDefinitions:
    def test_all_tools_have_names(self):
        for tool in LIVE_TOOL_DEFINITIONS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool

    def test_expected_tools_present(self):
        names = {t["name"] for t in LIVE_TOOL_DEFINITIONS}
        expected = {
            "check_cluster_connectivity",
            "find_monitoring_stack",
            "get_cluster_resources",
            "describe_cluster_resource",
            "get_pod_logs",
            "get_cluster_events",
            "check_scrape_targets",
            "validate_metric_exists",
            "run_promql_query",
            "get_prometheus_alerts",
            "get_prometheus_rules",
            "list_grafana_dashboards",
            "check_grafana_datasources",
            "import_grafana_dashboard",
            "apply_kubernetes_manifest",
            "generate_validation_report",
        }
        assert expected.issubset(names)


# ═══════════════════════════════════════════════════════════════════════════
# LiveToolExecutor
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def mock_executor():
    """Executor with mocked cluster client."""
    cluster = MagicMock(spec=ClusterClient)
    prom = MagicMock(spec=PrometheusClient)
    graf = MagicMock(spec=GrafanaClient)
    executor = LiveToolExecutor(cluster=cluster, prometheus=prom, grafana=graf)
    return executor


class TestCheckClusterConnectivity:
    def test_reachable(self, mock_executor):
        mock_executor.cluster.check_connectivity.return_value = CommandResult(
            command="kubectl version", returncode=0,
            stdout="Client: v1.29\nServer: v1.28", stderr=""
        )
        mock_executor.cluster.get_current_context.return_value = CommandResult(
            command="kubectl config", returncode=0,
            stdout="minikube", stderr=""
        )
        result = mock_executor.execute("check_cluster_connectivity", {})
        assert "reachable" in result.lower()
        assert "minikube" in result

    def test_not_reachable(self, mock_executor):
        mock_executor.cluster.check_connectivity.return_value = CommandResult(
            command="kubectl version", returncode=1,
            stdout="", stderr="connection refused"
        )
        result = mock_executor.execute("check_cluster_connectivity", {})
        assert "FAIL" in result


class TestFindMonitoringStack:
    def test_both_found(self, mock_executor):
        mock_executor.cluster.find_prometheus.return_value = {
            "found": True, "namespace": "monitoring",
            "service": "prometheus", "port": 9090,
            "url": "http://prometheus.monitoring.svc.cluster.local:9090",
        }
        mock_executor.cluster.find_grafana.return_value = {
            "found": True, "namespace": "monitoring",
            "service": "grafana", "port": 3000,
            "url": "http://grafana.monitoring.svc.cluster.local:3000",
        }
        mock_executor.prometheus.is_reachable.return_value = True
        mock_executor.grafana.is_reachable.return_value = True

        result = mock_executor.execute("find_monitoring_stack", {})
        assert "Prometheus FOUND" in result
        assert "Grafana FOUND" in result
        assert "REACHABLE" in result

    def test_not_found(self, mock_executor):
        mock_executor.cluster.find_prometheus.return_value = {
            "found": False, "reason": "No Prometheus service"
        }
        mock_executor.cluster.find_grafana.return_value = {
            "found": False, "reason": "No Grafana service"
        }
        result = mock_executor.execute("find_monitoring_stack", {})
        assert "NOT FOUND" in result


class TestGetClusterResources:
    def test_success(self, mock_executor):
        items = {
            "items": [
                {
                    "metadata": {"name": "web-app", "namespace": "default"},
                    "status": {"phase": "Running"},
                }
            ]
        }
        mock_executor.cluster.get_resources.return_value = CommandResult(
            command="kubectl get", returncode=0,
            stdout=json.dumps(items), stderr=""
        )
        result = mock_executor.execute("get_cluster_resources", {"kind": "pods"})
        assert "Found 1 pods" in result
        assert "web-app" in result

    def test_empty(self, mock_executor):
        mock_executor.cluster.get_resources.return_value = CommandResult(
            command="kubectl get", returncode=0,
            stdout=json.dumps({"items": []}), stderr=""
        )
        result = mock_executor.execute("get_cluster_resources", {"kind": "pods"})
        assert "No pods found" in result


class TestGetPodLogs:
    def test_logs(self, mock_executor):
        mock_executor.cluster.get_pod_logs.return_value = CommandResult(
            command="kubectl logs", returncode=0,
            stdout="Starting server on :8080\nReady.", stderr=""
        )
        result = mock_executor.execute("get_pod_logs", {
            "pod_name": "my-pod", "namespace": "default"
        })
        assert "Starting server" in result


class TestCheckScrapeTargets:
    def test_scrape_summary(self, mock_executor):
        mock_executor.prometheus.scrape_health_summary.return_value = {
            "total_targets": 5, "healthy": 4, "unhealthy": 1,
            "jobs": {
                "node-exporter": {"up": 3, "down": 0, "unknown": 0, "total": 3},
                "postgres": {"up": 1, "down": 1, "unknown": 0, "total": 2},
            },
        }
        result = mock_executor.execute("check_scrape_targets", {})
        assert "Total scrape targets: 5" in result
        assert "Healthy: 4" in result
        assert "DEGRADED" in result


class TestValidateMetricExists:
    def test_batch_check(self, mock_executor):
        mock_executor.prometheus.check_metric_batch.return_value = {
            "up": True, "missing_metric": False,
        }
        result = mock_executor.execute("validate_metric_exists", {
            "metric_names": ["up", "missing_metric"]
        })
        assert "FOUND: up" in result
        assert "MISSING: missing_metric" in result


class TestRunPromqlQuery:
    def test_valid_query_with_data(self, mock_executor):
        mock_executor.prometheus.validate_promql.return_value = {
            "valid": True, "result_count": 1, "has_data": True
        }
        mock_executor.prometheus.query_value.return_value = [
            {"metric": {"job": "test"}, "value": [0, "42"]}
        ]
        result = mock_executor.execute("run_promql_query", {"query": "up"})
        assert "1 result" in result
        assert "42" in result

    def test_invalid_query(self, mock_executor):
        mock_executor.prometheus.validate_promql.return_value = {
            "valid": False, "error": "parse error"
        }
        result = mock_executor.execute("run_promql_query", {"query": "bad{{{}"})
        assert "INVALID" in result
        assert "parse error" in result


class TestImportGrafanaDashboard:
    def test_successful_import(self, mock_executor):
        mock_executor.grafana.get_prometheus_datasource.return_value = {
            "uid": "prom-uid", "name": "Prometheus"
        }
        mock_executor.grafana.import_dashboard_by_id.return_value = {
            "success": True, "dashboard_id": 9628,
            "title": "PostgreSQL", "imported_url": "/d/abc/postgresql",
            "imported_uid": "abc",
        }
        result = mock_executor.execute("import_grafana_dashboard", {
            "dashboard_id": 9628
        })
        assert "successfully" in result.lower()
        assert "PostgreSQL" in result

    def test_failed_import(self, mock_executor):
        mock_executor.grafana.get_prometheus_datasource.return_value = None
        mock_executor.grafana.import_dashboard_by_id.return_value = {
            "success": False, "error": "download failed"
        }
        result = mock_executor.execute("import_grafana_dashboard", {
            "dashboard_id": 99999
        })
        assert "FAILED" in result


class TestApplyManifest:
    def test_write_allowed(self, mock_executor):
        mock_executor.cluster.apply_manifest.return_value = CommandResult(
            command="kubectl apply", returncode=0,
            stdout="service/my-svc created", stderr=""
        )
        result = mock_executor.execute("apply_kubernetes_manifest", {
            "manifest_yaml": "apiVersion: v1\nkind: Service",
            "namespace": "default",
        })
        assert "created" in result

    def test_write_denied(self, mock_executor):
        mock_executor.cluster.apply_manifest.side_effect = PermissionError(
            "Write operations are disabled."
        )
        result = mock_executor.execute("apply_kubernetes_manifest", {
            "manifest_yaml": "apiVersion: v1\nkind: Service",
        })
        assert "disabled" in result.lower()


class TestGenerateValidationReport:
    def test_returns_json(self, mock_executor):
        inp = {
            "cluster_summary": "test cluster",
            "checks": [{"name": "test", "status": "pass", "message": "ok"}],
            "recommendations": ["deploy exporter"],
        }
        result = mock_executor.execute("generate_validation_report", inp)
        parsed = json.loads(result)
        assert parsed["cluster_summary"] == "test cluster"


class TestUnknownTool:
    def test_unknown(self, mock_executor):
        result = mock_executor.execute("nonexistent_tool", {})
        assert "Unknown" in result


class TestRequirePrometheus:
    def test_no_prometheus_raises(self):
        executor = LiveToolExecutor(
            cluster=MagicMock(spec=ClusterClient),
            prometheus=None,
            grafana=None,
        )
        result = executor.execute("check_scrape_targets", {})
        assert "not configured" in result.lower()


class TestRequireGrafana:
    def test_no_grafana_raises(self):
        executor = LiveToolExecutor(
            cluster=MagicMock(spec=ClusterClient),
            prometheus=None,
            grafana=None,
        )
        result = executor.execute("list_grafana_dashboards", {})
        assert "not configured" in result.lower()
