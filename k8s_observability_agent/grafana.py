"""Grafana HTTP API client for dashboard management and validation.

Connects to a Grafana instance to:
  • Import community dashboards by ID
  • List existing dashboards and datasources
  • Verify that datasources are healthy
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# grafana.com API for fetching dashboard JSON by ID.
_GRAFANA_COM_API = "https://grafana.com/api/dashboards/{dashboard_id}/revisions/latest/download"


class GrafanaClient:
    """Lightweight Grafana HTTP API client.

    Parameters
    ----------
    base_url : str
        Base URL of the Grafana instance (e.g. ``http://localhost:3000``).
    api_key : str
        Grafana API key or service-account token.  If empty, uses basic auth.
    username : str
        Basic auth username (default: ``admin``).
    password : str
        Basic auth password (default: ``admin``).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        username: str = "admin",
        password: str = "admin",
        *,
        ca_cert: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        auth = None

        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            auth = (username, password)

        verify: bool | str = ca_cert if ca_cert else True
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            auth=auth,
            timeout=_TIMEOUT,
            verify=verify,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GrafanaClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ── Health ────────────────────────────────────────────────────────────

    def is_reachable(self) -> bool:
        """Check if Grafana is reachable and credentials are valid."""
        try:
            resp = self._client.get("/api/health")
            return resp.status_code == 200
        except Exception:
            return False

    def get_health(self) -> dict[str, Any]:
        """Return Grafana health status."""
        resp = self._client.get("/api/health")
        resp.raise_for_status()
        return resp.json()

    # ── Datasources ──────────────────────────────────────────────────────

    def list_datasources(self) -> list[dict[str, Any]]:
        """List all configured datasources."""
        resp = self._client.get("/api/datasources")
        resp.raise_for_status()
        return resp.json()

    def check_datasource_health(self, datasource_uid: str) -> dict[str, Any]:
        """Check the health of a specific datasource by UID."""
        try:
            resp = self._client.get(f"/api/datasources/uid/{datasource_uid}/health")
            if resp.status_code == 200:
                return {"healthy": True, **resp.json()}
            return {"healthy": False, "status_code": resp.status_code, "body": resp.text[:500]}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)[:500]}

    def get_prometheus_datasource(self) -> dict[str, Any] | None:
        """Find the first Prometheus-type datasource."""
        for ds in self.list_datasources():
            if ds.get("type") == "prometheus":
                return ds
        return None

    # ── Dashboards ────────────────────────────────────────────────────────

    def list_dashboards(self, query: str = "") -> list[dict[str, Any]]:
        """Search for dashboards."""
        params: dict[str, str] = {"type": "dash-db"}
        if query:
            params["query"] = query
        resp = self._client.get("/api/search", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_dashboard(self, uid: str) -> dict[str, Any]:
        """Get a dashboard by UID."""
        resp = self._client.get(f"/api/dashboards/uid/{uid}")
        resp.raise_for_status()
        return resp.json()

    def import_dashboard(
        self,
        dashboard_json: dict[str, Any],
        datasource_name: str = "Prometheus",
        datasource_uid: str = "",
        folder_id: int = 0,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Import a dashboard JSON model into Grafana.

        Parameters
        ----------
        dashboard_json : dict
            The dashboard JSON model (as from grafana.com download).
        datasource_name : str
            Name of the Prometheus datasource to use.
        datasource_uid : str
            UID of the Prometheus datasource.  If empty, uses datasource_name.
        folder_id : int
            Target folder ID (0 = General).
        overwrite : bool
            Whether to overwrite an existing dashboard with the same title.
        """
        # Build input mappings for datasource template variables
        inputs = []
        if datasource_uid:
            inputs.append({
                "name": "DS_PROMETHEUS",
                "type": "datasource",
                "pluginId": "prometheus",
                "value": datasource_uid,
            })
        else:
            inputs.append({
                "name": "DS_PROMETHEUS",
                "type": "datasource",
                "pluginId": "prometheus",
                "value": datasource_name,
            })

        payload = {
            "dashboard": dashboard_json,
            "inputs": inputs,
            "folderId": folder_id,
            "overwrite": overwrite,
        }

        resp = self._client.post("/api/dashboards/import", json=payload)
        resp.raise_for_status()
        return resp.json()

    def import_dashboard_by_id(
        self,
        grafana_com_id: int,
        datasource_name: str = "Prometheus",
        datasource_uid: str = "",
        folder_id: int = 0,
    ) -> dict[str, Any]:
        """Download a dashboard from grafana.com and import it.

        Parameters
        ----------
        grafana_com_id : int
            The grafana.com dashboard ID (e.g. 9628 for PostgreSQL).
        datasource_name : str
            Name of the Prometheus datasource.
        datasource_uid : str
            UID of the Prometheus datasource.
        folder_id : int
            Target folder ID.

        Returns
        -------
        dict with ``status``, ``uid``, ``url`` on success, or ``error`` on failure.
        """
        # Step 1: Download from grafana.com
        url = _GRAFANA_COM_API.format(dashboard_id=grafana_com_id)
        try:
            dl_resp = httpx.get(url, timeout=15.0, follow_redirects=True)
            dl_resp.raise_for_status()
            dashboard_json = dl_resp.json()
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to download dashboard {grafana_com_id} from grafana.com: {exc}",
            }

        # Step 2: Import into Grafana
        try:
            result = self.import_dashboard(
                dashboard_json=dashboard_json,
                datasource_name=datasource_name,
                datasource_uid=datasource_uid,
                folder_id=folder_id,
            )
            return {
                "success": True,
                "dashboard_id": grafana_com_id,
                "imported_uid": result.get("uid", ""),
                "imported_url": result.get("importedUrl", ""),
                "title": dashboard_json.get("title", f"Dashboard {grafana_com_id}"),
            }
        except httpx.HTTPStatusError as exc:
            return {
                "success": False,
                "dashboard_id": grafana_com_id,
                "error": f"Import failed ({exc.response.status_code}): {exc.response.text[:300]}",
            }
        except Exception as exc:
            return {
                "success": False,
                "dashboard_id": grafana_com_id,
                "error": f"Import failed: {exc}",
            }

    # ── Folders ───────────────────────────────────────────────────────────

    def create_folder(self, title: str) -> dict[str, Any]:
        """Create a dashboard folder. Returns the folder info (id, uid, title)."""
        import hashlib

        uid = hashlib.sha256(title.encode()).hexdigest()[:12]
        resp = self._client.post(
            "/api/folders", json={"title": title, "uid": uid}
        )
        if resp.status_code == 412:
            # Already exists — try to fetch it
            existing = self._client.get(f"/api/folders/{uid}")
            if existing.status_code == 200:
                return existing.json()
        resp.raise_for_status()
        return resp.json()

    def list_folders(self) -> list[dict[str, Any]]:
        """List all dashboard folders."""
        resp = self._client.get("/api/folders")
        resp.raise_for_status()
        return resp.json()
