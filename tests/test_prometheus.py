"""Tests for k8s_observability_agent.prometheus module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from k8s_observability_agent.prometheus import PrometheusClient


@pytest.fixture()
def mock_client():
    """Create a PrometheusClient with a mocked httpx.Client."""
    with patch.object(httpx, "Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value = mock_http
        client = PrometheusClient("http://localhost:9090")
        client._client = mock_http
        yield client, mock_http


class TestPrometheusClientInit:
    def test_base_url_trailing_slash_stripped(self):
        with patch.object(httpx, "Client"):
            p = PrometheusClient("http://prom:9090/")
        assert p.base_url == "http://prom:9090"


class TestTargets:
    def test_get_active_targets_summary(self, mock_client):
        client, mock_http = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "activeTargets": [
                    {
                        "labels": {"job": "node-exporter", "instance": "10.0.0.1:9100"},
                        "health": "up",
                        "lastScrape": "2024-01-01T00:00:00Z",
                        "lastError": "",
                        "scrapeUrl": "http://10.0.0.1:9100/metrics",
                    },
                    {
                        "labels": {"job": "node-exporter", "instance": "10.0.0.2:9100"},
                        "health": "down",
                        "lastScrape": "2024-01-01T00:00:00Z",
                        "lastError": "connection refused",
                        "scrapeUrl": "http://10.0.0.2:9100/metrics",
                    },
                ]
            }
        }
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        summaries = client.get_active_targets_summary()
        assert len(summaries) == 2
        assert summaries[0]["health"] == "up"
        assert summaries[1]["health"] == "down"
        assert summaries[1]["lastError"] == "connection refused"


class TestQuery:
    def test_query_value_returns_results(self, mock_client):
        client, mock_http = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"__name__": "up", "job": "prometheus"},
                        "value": [1704067200, "1"],
                    }
                ],
            },
        }
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        results = client.query_value("up")
        assert len(results) == 1
        assert results[0]["metric"]["__name__"] == "up"

    def test_query_value_empty(self, mock_client):
        client, mock_http = mock_client
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        results = client.query_value("nonexistent_metric")
        assert results == []


class TestMetricExists:
    def test_metric_exists_true(self, mock_client):
        client, mock_http = mock_client
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {"result": [{"metric": {}, "value": [0, "5"]}]}
        }
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        assert client.metric_exists("up") is True

    def test_metric_exists_false(self, mock_client):
        client, mock_http = mock_client
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"result": []}}
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        assert client.metric_exists("nonexistent") is False


class TestCheckMetricBatch:
    def test_batch_check(self, mock_client):
        client, mock_http = mock_client

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            # First metric exists, second does not
            if call_count[0] == 1:
                resp.json.return_value = {"data": {"result": [{"value": [0, "1"]}]}}
            else:
                resp.json.return_value = {"data": {"result": []}}
            return resp

        mock_http.get.side_effect = side_effect

        results = client.check_metric_batch(["up", "missing_metric"])
        assert results["up"] is True
        assert results["missing_metric"] is False


class TestValidatePromQL:
    def test_valid_expression(self, mock_client):
        client, mock_http = mock_client
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {"result": [{"metric": {"job": "x"}, "value": [0, "42"]}]}
        }
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        result = client.validate_promql("up == 1")
        assert result["valid"] is True
        assert result["has_data"] is True
        assert result["result_count"] == 1

    def test_invalid_expression(self, mock_client):
        client, mock_http = mock_client
        error_resp = MagicMock()
        error_resp.status_code = 400
        error_resp.text = "parse error"
        mock_http.get.side_effect = httpx.HTTPStatusError(
            "bad request",
            request=MagicMock(),
            response=error_resp,
        )

        result = client.validate_promql("invalid{{{")
        assert result["valid"] is False
        assert "parse error" in result["error"]


class TestScrapeHealthSummary:
    def test_summary(self, mock_client):
        client, mock_http = mock_client
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {
                "activeTargets": [
                    {"labels": {"job": "node", "instance": "a"}, "health": "up",
                     "lastScrape": "", "lastError": "", "scrapeUrl": ""},
                    {"labels": {"job": "node", "instance": "b"}, "health": "up",
                     "lastScrape": "", "lastError": "", "scrapeUrl": ""},
                    {"labels": {"job": "prom", "instance": "c"}, "health": "down",
                     "lastScrape": "", "lastError": "err", "scrapeUrl": ""},
                ]
            }
        }
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp

        summary = client.scrape_health_summary()
        assert summary["total_targets"] == 3
        assert summary["healthy"] == 2
        assert summary["unhealthy"] == 1
        assert summary["jobs"]["node"]["up"] == 2
        assert summary["jobs"]["prom"]["down"] == 1


class TestIsReachable:
    def test_reachable(self, mock_client):
        client, mock_http = mock_client
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "success"}
        mock_resp.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_resp
        assert client.is_reachable() is True

    def test_not_reachable(self, mock_client):
        client, mock_http = mock_client
        mock_http.get.side_effect = Exception("connection refused")
        assert client.is_reachable() is False
