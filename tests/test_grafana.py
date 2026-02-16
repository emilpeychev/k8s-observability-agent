"""Tests for k8s_observability_agent.grafana module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from k8s_observability_agent.grafana import GrafanaClient


@pytest.fixture()
def mock_grafana():
    """Create a GrafanaClient with a mocked httpx.Client."""
    with patch.object(httpx, "Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value = mock_http
        client = GrafanaClient("http://localhost:3000", api_key="test-key")
        client._client = mock_http
        yield client, mock_http


class TestGrafanaInit:
    def test_url_trailing_slash(self):
        with patch.object(httpx, "Client"):
            g = GrafanaClient("http://grafana:3000/")
        assert g.base_url == "http://grafana:3000"

    def test_api_key_auth(self):
        with patch.object(httpx, "Client") as mock_cls:
            GrafanaClient("http://grafana:3000", api_key="test-key")
            call_kwargs = mock_cls.call_args[1]
            assert "Bearer test-key" in str(call_kwargs.get("headers", {}))

    def test_basic_auth_fallback(self):
        with patch.object(httpx, "Client") as mock_cls:
            GrafanaClient("http://grafana:3000", api_key="", username="admin", password="secret")
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs.get("auth") == ("admin", "secret")


class TestGrafanaHealth:
    def test_is_reachable_true(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_http.get.return_value = mock_resp
        assert client.is_reachable() is True

    def test_is_reachable_false(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_http.get.side_effect = Exception("connection refused")
        assert client.is_reachable() is False

    def test_get_health(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"database": "ok", "version": "10.0.0"}
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        health = client.get_health()
        assert health["database"] == "ok"


class TestGrafanaDatasources:
    def test_list_datasources(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"name": "Prometheus", "type": "prometheus", "uid": "prom-uid"},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        ds = client.list_datasources()
        assert len(ds) == 1
        assert ds[0]["type"] == "prometheus"

    def test_get_prometheus_datasource(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"name": "Loki", "type": "loki", "uid": "loki-uid"},
            {"name": "Prometheus", "type": "prometheus", "uid": "prom-uid"},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        ds = client.get_prometheus_datasource()
        assert ds is not None
        assert ds["uid"] == "prom-uid"

    def test_get_prometheus_datasource_missing(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"name": "Loki", "type": "loki", "uid": "loki-uid"},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        assert client.get_prometheus_datasource() is None


class TestGrafanaDashboards:
    def test_list_dashboards(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"uid": "abc123", "title": "Node Exporter", "folderTitle": "General"},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        dashboards = client.list_dashboards()
        assert len(dashboards) == 1
        assert dashboards[0]["title"] == "Node Exporter"

    def test_import_dashboard(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "uid": "imported-uid",
            "importedUrl": "/d/imported-uid/my-dashboard",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_http.post.return_value = mock_resp

        result = client.import_dashboard(
            dashboard_json={"title": "Test Dashboard", "panels": []},
            datasource_name="Prometheus",
        )
        assert result["uid"] == "imported-uid"

    def test_import_dashboard_with_uid(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"uid": "uid", "importedUrl": "/d/uid/dash"}
        mock_resp.raise_for_status = MagicMock()
        mock_http.post.return_value = mock_resp

        client.import_dashboard(
            dashboard_json={"title": "Test"},
            datasource_uid="prom-uid-123",
        )

        # Verify the payload sent to Grafana
        call_kwargs = mock_http.post.call_args[1]
        payload = call_kwargs["json"]
        assert payload["inputs"][0]["value"] == "prom-uid-123"

    @patch("httpx.get")
    def test_import_dashboard_by_id_success(self, mock_httpx_get, mock_grafana):
        client, mock_http = mock_grafana

        # Mock grafana.com download
        dl_resp = MagicMock()
        dl_resp.json.return_value = {"title": "PostgreSQL Dashboard", "panels": []}
        dl_resp.raise_for_status = MagicMock()
        mock_httpx_get.return_value = dl_resp

        # Mock Grafana import
        import_resp = MagicMock()
        import_resp.json.return_value = {
            "uid": "imported-uid",
            "importedUrl": "/d/imported-uid/postgresql",
        }
        import_resp.raise_for_status = MagicMock()
        mock_http.post.return_value = import_resp

        # Mock datasource lookup
        ds_resp = MagicMock()
        ds_resp.json.return_value = [
            {"name": "Prometheus", "type": "prometheus", "uid": "prom-uid"},
        ]
        ds_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = ds_resp

        result = client.import_dashboard_by_id(9628)
        assert result["success"] is True
        assert result["dashboard_id"] == 9628
        assert result["title"] == "PostgreSQL Dashboard"

    @patch("httpx.get")
    def test_import_dashboard_by_id_download_failure(self, mock_httpx_get, mock_grafana):
        client, mock_http = mock_grafana
        mock_httpx_get.side_effect = Exception("Network error")

        result = client.import_dashboard_by_id(99999)
        assert result["success"] is False
        assert "Failed to download" in result["error"]


class TestGrafanaFolders:
    def test_list_folders(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"id": 1, "uid": "abc", "title": "Databases"},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        folders = client.list_folders()
        assert len(folders) == 1

    def test_create_folder(self, mock_grafana):
        client, mock_http = mock_grafana
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 1, "uid": "hash", "title": "My Folder"}
        mock_resp.raise_for_status = MagicMock()
        mock_http.post.return_value = mock_resp

        folder = client.create_folder("My Folder")
        assert folder["title"] == "My Folder"


class TestContextManager:
    def test_context_manager(self):
        with patch.object(httpx, "Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value = mock_http
            with GrafanaClient("http://localhost:3000") as g:
                assert g is not None
            mock_http.close.assert_called_once()
