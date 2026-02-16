"""Tests for k8s_observability_agent.cluster module."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from k8s_observability_agent.cluster import ClusterClient, CommandResult


# ═══════════════════════════════════════════════════════════════════════════
# CommandResult
# ═══════════════════════════════════════════════════════════════════════════


class TestCommandResult:
    def test_ok_property(self):
        r = CommandResult(command="kubectl version", returncode=0, stdout="ok", stderr="")
        assert r.ok is True

    def test_not_ok(self):
        r = CommandResult(command="kubectl fail", returncode=1, stdout="", stderr="err")
        assert r.ok is False

    def test_summary_success(self):
        r = CommandResult(command="cmd", returncode=0, stdout="hello world", stderr="")
        assert r.summary == "hello world"

    def test_summary_truncation(self):
        r = CommandResult(command="cmd", returncode=0, stdout="x" * 3000, stderr="")
        assert len(r.summary) == 2000

    def test_summary_error(self):
        r = CommandResult(command="cmd", returncode=1, stdout="", stderr="bad thing")
        assert "ERROR" in r.summary
        assert "bad thing" in r.summary


# ═══════════════════════════════════════════════════════════════════════════
# ClusterClient.__init__
# ═══════════════════════════════════════════════════════════════════════════


class TestClusterClientInit:
    def test_default_base_cmd(self):
        c = ClusterClient()
        assert c._base_cmd == ["kubectl"]

    def test_kubeconfig(self):
        c = ClusterClient(kubeconfig="/tmp/kube.conf")
        assert "--kubeconfig" in c._base_cmd
        assert "/tmp/kube.conf" in c._base_cmd

    def test_context(self):
        c = ClusterClient(context="minikube")
        assert "--context" in c._base_cmd
        assert "minikube" in c._base_cmd

    def test_writes_disabled_by_default(self):
        c = ClusterClient()
        assert c.allow_writes is False


# ═══════════════════════════════════════════════════════════════════════════
# ClusterClient._run
# ═══════════════════════════════════════════════════════════════════════════


class TestClusterClientRun:
    @patch("subprocess.run")
    def test_successful_command(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"items": []}', stderr=""
        )
        c = ClusterClient()
        result = c._run(["get", "pods"])
        assert result.ok
        assert '{"items": []}' in result.stdout

    @patch("subprocess.run")
    def test_failing_command(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="not found"
        )
        c = ClusterClient()
        result = c._run(["get", "pods"])
        assert not result.ok
        assert "not found" in result.stderr

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired("kubectl", 30))
    def test_timeout(self, mock_run):
        c = ClusterClient()
        result = c._run(["get", "pods"])
        assert not result.ok
        assert "timed out" in result.stderr

    @patch("subprocess.run", side_effect=FileNotFoundError())
    def test_kubectl_not_found(self, mock_run):
        c = ClusterClient()
        result = c._run(["get", "pods"])
        assert not result.ok
        assert "kubectl not found" in result.stderr


# ═══════════════════════════════════════════════════════════════════════════
# Read operations
# ═══════════════════════════════════════════════════════════════════════════


class TestClusterReadOps:
    @patch("subprocess.run")
    def test_get_namespaces(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"items": [{"metadata": {"name": "default"}}]}),
            stderr="",
        )
        c = ClusterClient()
        result = c.get_namespaces()
        assert result.ok

    @patch("subprocess.run")
    def test_get_resources_with_namespace(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        c = ClusterClient()
        c.get_resources("pods", namespace="kube-system")
        args = mock_run.call_args[0][0]
        assert "-n" in args
        assert "kube-system" in args

    @patch("subprocess.run")
    def test_get_resources_all_namespaces(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        c = ClusterClient()
        c.get_resources("pods")
        args = mock_run.call_args[0][0]
        assert "--all-namespaces" in args

    @patch("subprocess.run")
    def test_get_resources_with_label_selector(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        c = ClusterClient()
        c.get_resources("service", label_selector="app=prometheus")
        args = mock_run.call_args[0][0]
        assert "-l" in args
        assert "app=prometheus" in args

    @patch("subprocess.run")
    def test_get_pod_logs(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="log line 1\nlog line 2", stderr=""
        )
        c = ClusterClient()
        result = c.get_pod_logs("my-pod", namespace="default", tail_lines=50)
        assert result.ok
        args = mock_run.call_args[0][0]
        assert "--tail=50" in args

    @patch("subprocess.run")
    def test_describe_resource(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Name: my-pod\nStatus: Running", stderr=""
        )
        c = ClusterClient()
        result = c.describe_resource("pod", "my-pod", "default")
        assert result.ok

    @patch("subprocess.run")
    def test_check_connectivity(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Client Version: v1.29.0\nServer Version: v1.28.4",
            stderr="",
        )
        c = ClusterClient()
        result = c.check_connectivity()
        assert result.ok


# ═══════════════════════════════════════════════════════════════════════════
# Write operations
# ═══════════════════════════════════════════════════════════════════════════


class TestClusterWriteOps:
    def test_apply_rejected_without_allow_writes(self):
        c = ClusterClient(allow_writes=False)
        with pytest.raises(PermissionError):
            c.apply_manifest("apiVersion: v1\nkind: ConfigMap")

    def test_delete_rejected_without_allow_writes(self):
        c = ClusterClient(allow_writes=False)
        with pytest.raises(PermissionError):
            c.delete_resource("pod", "my-pod")

    @patch("subprocess.run")
    def test_apply_with_writes_enabled(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="configmap/test created", stderr=""
        )
        c = ClusterClient(allow_writes=True)
        result = c.apply_manifest("apiVersion: v1\nkind: ConfigMap", namespace="test-ns")
        assert result.ok

    @patch("subprocess.run")
    def test_delete_with_writes_enabled(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='pod "my-pod" deleted', stderr=""
        )
        c = ClusterClient(allow_writes=True)
        result = c.delete_resource("pod", "my-pod")
        assert result.ok


# ═══════════════════════════════════════════════════════════════════════════
# Discovery helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestClusterDiscovery:
    @patch("subprocess.run")
    def test_find_prometheus_found(self, mock_run):
        svc_data = {
            "items": [
                {
                    "metadata": {"name": "prometheus-server", "namespace": "monitoring"},
                    "spec": {
                        "ports": [{"name": "http-web", "port": 9090}]
                    },
                }
            ]
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(svc_data), stderr=""
        )
        c = ClusterClient()
        info = c.find_prometheus()
        assert info["found"] is True
        assert info["namespace"] == "monitoring"
        assert info["service"] == "prometheus-server"
        assert info["port"] == 9090
        assert "svc.cluster.local" in info["url"]

    @patch("subprocess.run")
    def test_find_prometheus_not_found(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"items": []}), stderr=""
        )
        c = ClusterClient()
        info = c.find_prometheus()
        assert info["found"] is False

    @patch("subprocess.run")
    def test_find_grafana_found(self, mock_run):
        svc_data = {
            "items": [
                {
                    "metadata": {"name": "grafana", "namespace": "monitoring"},
                    "spec": {
                        "ports": [{"name": "http", "port": 3000}]
                    },
                }
            ]
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(svc_data), stderr=""
        )
        c = ClusterClient()
        info = c.find_grafana()
        assert info["found"] is True
        assert info["namespace"] == "monitoring"
        assert info["port"] == 3000
