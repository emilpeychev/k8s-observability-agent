"""Prometheus HTTP API client for live metric validation.

Connects to a Prometheus instance (typically via port-forward or in-cluster
URL) to verify that metrics exist, scrape targets are healthy, and alert
rules evaluate correctly.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

# Timeout for Prometheus API calls.
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class PrometheusClient:
    """Lightweight Prometheus HTTP API v1 client.

    Parameters
    ----------
    base_url : str
        Base URL of the Prometheus server (e.g. ``http://localhost:9090``).
    """

    def __init__(self, base_url: str, *, ca_cert: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        verify: bool | str = ca_cert if ca_cert else True
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=_TIMEOUT,
            verify=verify,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PrometheusClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ── Raw helpers ───────────────────────────────────────────────────────

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """Execute a GET request against the Prometheus API.

        Returns the parsed JSON response body.
        Raises ``httpx.HTTPStatusError`` on non-2xx responses.
        """
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Targets ───────────────────────────────────────────────────────────

    def get_targets(self) -> dict[str, Any]:
        """Return all active and dropped scrape targets.

        Returns the full ``/api/v1/targets`` response.
        """
        return self._get("/api/v1/targets")

    def get_active_targets_summary(self) -> list[dict[str, Any]]:
        """Return a simplified view of active scrape targets.

        Each entry contains: job, instance, health, lastScrape, lastError.
        """
        data = self.get_targets()
        active = data.get("data", {}).get("activeTargets", [])
        summaries = []
        for t in active:
            summaries.append({
                "job": t.get("labels", {}).get("job", ""),
                "instance": t.get("labels", {}).get("instance", ""),
                "health": t.get("health", "unknown"),
                "lastScrape": t.get("lastScrape", ""),
                "lastError": t.get("lastError", ""),
                "scrapeUrl": t.get("scrapeUrl", ""),
            })
        return summaries

    # ── Instant query ─────────────────────────────────────────────────────

    def query(self, promql: str) -> dict[str, Any]:
        """Execute an instant PromQL query.

        Returns the full ``/api/v1/query`` response.
        """
        return self._get("/api/v1/query", params={"query": promql})

    def query_value(self, promql: str) -> list[dict[str, Any]]:
        """Execute a PromQL query and return just the result vector.

        Each entry has ``metric`` (label dict) and ``value`` (timestamp, value).
        Returns an empty list if the query produced no data.
        """
        data = self.query(promql)
        return data.get("data", {}).get("result", [])

    def metric_exists(self, metric_name: str) -> bool:
        """Check whether a metric name exists in the TSDB.

        Uses a count query so this is cheap even on large TSDBs.
        """
        results = self.query_value(f"count({metric_name})")
        return len(results) > 0

    # ── Metric metadata ──────────────────────────────────────────────────

    def get_metric_metadata(self, metric_name: str = "") -> dict[str, Any]:
        """Return metric metadata (type, help, unit).

        If *metric_name* is empty, returns metadata for all metrics (may be large).
        """
        params = {}
        if metric_name:
            params["metric"] = metric_name
        return self._get("/api/v1/targets/metadata", params=params)

    # ── Rules ─────────────────────────────────────────────────────────────

    def get_rules(self, rule_type: str = "") -> dict[str, Any]:
        """Return all configured alerting / recording rules.

        *rule_type* can be ``"alert"`` or ``"record"`` to filter.
        """
        params = {}
        if rule_type:
            params["type"] = rule_type
        return self._get("/api/v1/rules", params=params)

    def get_alerts(self) -> dict[str, Any]:
        """Return currently firing / pending alerts."""
        return self._get("/api/v1/alerts")

    # ── High-level validation helpers ─────────────────────────────────────

    def check_metric_batch(self, metric_names: list[str]) -> dict[str, bool]:
        """Check existence of multiple metrics at once.

        Returns a dict mapping metric_name → exists (True/False).
        """
        results: dict[str, bool] = {}
        for name in metric_names:
            try:
                results[name] = self.metric_exists(name)
            except Exception:
                results[name] = False
        return results

    def validate_promql(self, expr: str) -> dict[str, Any]:
        """Try to evaluate a PromQL expression and report success/failure.

        Returns ``{"valid": True, "result_count": N}`` on success,
        or ``{"valid": False, "error": "..."}`` on failure.
        """
        try:
            results = self.query_value(expr)
            return {"valid": True, "result_count": len(results), "has_data": len(results) > 0}
        except httpx.HTTPStatusError as exc:
            return {
                "valid": False,
                "error": exc.response.text[:500] if exc.response else str(exc),
            }
        except Exception as exc:
            return {"valid": False, "error": str(exc)[:500]}

    def scrape_health_summary(self) -> dict[str, Any]:
        """Return a high-level summary of scrape target health.

        Returns counts of healthy, unhealthy, and total targets grouped by job.
        """
        targets = self.get_active_targets_summary()
        jobs: dict[str, dict[str, int]] = {}
        for t in targets:
            job = t["job"]
            if job not in jobs:
                jobs[job] = {"up": 0, "down": 0, "unknown": 0, "total": 0}
            jobs[job]["total"] += 1
            if t["health"] == "up":
                jobs[job]["up"] += 1
            elif t["health"] == "down":
                jobs[job]["down"] += 1
            else:
                jobs[job]["unknown"] += 1

        total_up = sum(j["up"] for j in jobs.values())
        total_down = sum(j["down"] for j in jobs.values())
        total = sum(j["total"] for j in jobs.values())

        return {
            "total_targets": total,
            "healthy": total_up,
            "unhealthy": total_down,
            "jobs": jobs,
        }

    def is_reachable(self) -> bool:
        """Check if Prometheus is reachable."""
        try:
            self._get("/api/v1/status/buildinfo")
            return True
        except Exception:
            return False
