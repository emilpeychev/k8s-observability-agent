"""Live-cluster tool definitions exposed to the Claude validation agent.

These tools allow the agent to interact with a live Kubernetes cluster,
query Prometheus, import Grafana dashboards, and diagnose/fix issues.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from k8s_observability_agent.cluster import ClusterClient
from k8s_observability_agent.grafana import GrafanaClient
from k8s_observability_agent.prometheus import PrometheusClient

logger = logging.getLogger(__name__)


# ──────────────────────────── Tool Schemas ─────────────────────────────────

LIVE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    # ── Cluster connectivity ──────────────────────────────────────────
    {
        "name": "check_cluster_connectivity",
        "description": (
            "Verify that the Kubernetes cluster is reachable. "
            "Returns cluster version and context info."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_monitoring_stack",
        "description": (
            "Auto-discover the monitoring stack in the cluster. "
            "Locates Prometheus and Grafana services, checks reachability, "
            "and returns connection info."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    # ── Cluster inspection ────────────────────────────────────────────
    {
        "name": "get_cluster_resources",
        "description": (
            "List live Kubernetes resources of a given kind in the cluster. "
            "Can filter by namespace and label selector."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Resource kind (e.g. 'pods', 'deployments', 'services', 'servicemonitors').",
                },
                "namespace": {
                    "type": "string",
                    "description": "Namespace to search in. Empty for all namespaces.",
                },
                "label_selector": {
                    "type": "string",
                    "description": "Label selector (e.g. 'app=nginx'). Empty for all.",
                },
            },
            "required": ["kind"],
        },
    },
    {
        "name": "describe_cluster_resource",
        "description": (
            "Describe a specific resource in the cluster. "
            "Shows status, events, conditions — useful for diagnosing issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Resource kind (e.g. 'pod', 'deployment')."},
                "name": {"type": "string", "description": "Resource name."},
                "namespace": {
                    "type": "string",
                    "description": "Namespace (default: 'default').",
                },
            },
            "required": ["kind", "name"],
        },
    },
    {
        "name": "get_pod_logs",
        "description": (
            "Get recent log lines from a pod. "
            "Useful for diagnosing exporter errors or scrape failures."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod_name": {"type": "string"},
                "namespace": {"type": "string", "description": "Default: 'default'."},
                "container": {
                    "type": "string",
                    "description": "Container name (optional, for multi-container pods).",
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "Number of lines to fetch (default: 100).",
                },
            },
            "required": ["pod_name"],
        },
    },
    {
        "name": "get_cluster_events",
        "description": "Get recent events in a namespace. Useful for spotting CrashLoopBackOff, pull errors, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Default: 'default'."},
            },
            "required": [],
        },
    },
    # ── Prometheus validation ─────────────────────────────────────────
    {
        "name": "check_scrape_targets",
        "description": (
            "Get the health status of all Prometheus scrape targets. "
            "Shows which jobs are up/down and any scrape errors."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "validate_metric_exists",
        "description": (
            "Check whether a specific metric exists in Prometheus. "
            "Can check multiple metrics at once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of metric names to check.",
                },
            },
            "required": ["metric_names"],
        },
    },
    {
        "name": "run_promql_query",
        "description": (
            "Execute a PromQL query against Prometheus and return the results. "
            "Use to test alert expressions or verify metric values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PromQL expression to evaluate."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_prometheus_alerts",
        "description": "Get currently firing and pending alerts from Prometheus.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_prometheus_rules",
        "description": "Get all configured alerting and recording rules from Prometheus.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    # ── Grafana operations ────────────────────────────────────────────
    {
        "name": "list_grafana_dashboards",
        "description": "List dashboards currently installed in Grafana.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query to filter dashboards. Empty for all.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "check_grafana_datasources",
        "description": "List all Grafana datasources and check if a Prometheus datasource is configured.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "import_grafana_dashboard",
        "description": (
            "Import a community dashboard from grafana.com into this Grafana instance. "
            "Specify the grafana.com dashboard ID (integer)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dashboard_id": {
                    "type": "integer",
                    "description": "grafana.com dashboard ID (e.g. 9628 for PostgreSQL).",
                },
                "folder_title": {
                    "type": "string",
                    "description": "Grafana folder name to import into. Default: 'General'.",
                },
            },
            "required": ["dashboard_id"],
        },
    },
    # ── Fix / remediation ─────────────────────────────────────────────
    {
        "name": "apply_kubernetes_manifest",
        "description": (
            "Apply a YAML manifest to the cluster (kubectl apply). "
            "Use to deploy exporters, ServiceMonitors, or PrometheusRules. "
            "REQUIRES --allow-writes flag."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "manifest_yaml": {
                    "type": "string",
                    "description": "Raw YAML manifest content to apply.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Target namespace (default: 'default').",
                },
            },
            "required": ["manifest_yaml"],
        },
    },
    # ── Final report ──────────────────────────────────────────────────
    {
        "name": "generate_validation_report",
        "description": (
            "Generate the final validation report, summarising what was checked, "
            "what passed, what failed, and what fixes were applied or recommended."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_summary": {
                    "type": "string",
                    "description": "Summary of the cluster being validated.",
                },
                "checks": {
                    "type": "array",
                    "description": "List of validation checks performed.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Check name."},
                            "status": {
                                "type": "string",
                                "enum": ["pass", "fail", "warn", "skip"],
                                "description": "Result of the check.",
                            },
                            "message": {"type": "string", "description": "Details."},
                            "fix_applied": {
                                "type": "boolean",
                                "description": "Whether a fix was applied.",
                            },
                            "fix_description": {
                                "type": "string",
                                "description": "What fix was applied, if any.",
                            },
                        },
                        "required": ["name", "status", "message"],
                    },
                },
                "recommendations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Remaining action items for the operator.",
                },
                "dashboards_imported": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "dashboard_id": {"type": "integer"},
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "status": {"type": "string"},
                        },
                        "required": ["dashboard_id", "title", "status"],
                    },
                    "description": "Grafana dashboards that were imported.",
                },
            },
            "required": ["cluster_summary", "checks", "recommendations"],
        },
    },
]


# ──────────────────────────── Tool Implementations ─────────────────────────


class LiveToolExecutor:
    """Stateful executor that holds clients for Prometheus, Grafana, and kubectl.

    Parameters
    ----------
    cluster : ClusterClient
        kubectl wrapper for cluster interaction.
    prometheus : PrometheusClient | None
        Prometheus API client (may be None if not yet discovered).
    grafana : GrafanaClient | None
        Grafana API client (may be None if not yet discovered).
    """

    def __init__(
        self,
        cluster: ClusterClient,
        prometheus: PrometheusClient | None = None,
        grafana: GrafanaClient | None = None,
    ) -> None:
        self.cluster = cluster
        self.prometheus = prometheus
        self.grafana = grafana

    # ── Dispatcher ────────────────────────────────────────────────────

    def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Dispatch a tool call and return the string result."""
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return f"Unknown live tool: {tool_name}"
        try:
            return handler(tool_input)
        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            return f"Tool '{tool_name}' error: {exc}"

    # ── Cluster connectivity ──────────────────────────────────────────

    def _tool_check_cluster_connectivity(self, inp: dict[str, Any]) -> str:
        result = self.cluster.check_connectivity()
        if not result.ok:
            return f"FAIL: Cannot reach cluster.\n{result.stderr}"
        ctx = self.cluster.get_current_context()
        return (
            f"Cluster is reachable.\n"
            f"Context: {ctx.stdout.strip()}\n"
            f"Version info:\n{result.stdout.strip()}"
        )

    def _tool_find_monitoring_stack(self, inp: dict[str, Any]) -> str:
        lines: list[str] = []

        # Prometheus
        prom_info = self.cluster.find_prometheus()
        if prom_info.get("found"):
            lines.append(
                f"Prometheus FOUND: {prom_info['service']} in namespace {prom_info['namespace']} "
                f"(port {prom_info['port']})"
            )
            lines.append(f"  In-cluster URL: {prom_info['url']}")
            # If we don't have a Prometheus client yet, create one
            if self.prometheus is None:
                self.prometheus = PrometheusClient(prom_info["url"])
            if self.prometheus.is_reachable():
                lines.append("  Status: REACHABLE")
            else:
                lines.append(
                    "  Status: NOT REACHABLE from agent. "
                    "You may need to set up port-forwarding: "
                    f"kubectl port-forward -n {prom_info['namespace']} svc/{prom_info['service']} 9090:{prom_info['port']}"
                )
        else:
            lines.append(f"Prometheus NOT FOUND: {prom_info.get('reason', 'unknown')}")

        # Grafana
        graf_info = self.cluster.find_grafana()
        if graf_info.get("found"):
            lines.append(
                f"\nGrafana FOUND: {graf_info['service']} in namespace {graf_info['namespace']} "
                f"(port {graf_info['port']})"
            )
            lines.append(f"  In-cluster URL: {graf_info['url']}")
            if self.grafana is None:
                self.grafana = GrafanaClient(graf_info["url"])
            if self.grafana.is_reachable():
                lines.append("  Status: REACHABLE")
            else:
                lines.append(
                    "  Status: NOT REACHABLE from agent. "
                    "You may need to set up port-forwarding: "
                    f"kubectl port-forward -n {graf_info['namespace']} svc/{graf_info['service']} 3000:{graf_info['port']}"
                )
        else:
            lines.append(f"\nGrafana NOT FOUND: {graf_info.get('reason', 'unknown')}")

        return "\n".join(lines)

    # ── Cluster inspection ────────────────────────────────────────────

    def _tool_get_cluster_resources(self, inp: dict[str, Any]) -> str:
        kind = inp["kind"]
        namespace = inp.get("namespace", "")
        label_selector = inp.get("label_selector", "")
        result = self.cluster.get_resources(kind, namespace=namespace, label_selector=label_selector)
        if not result.ok:
            return f"Failed to get {kind}: {result.stderr}"
        try:
            data = json.loads(result.stdout)
            items = data.get("items", [])
            if not items:
                ns_text = f" in namespace '{namespace}'" if namespace else " across all namespaces"
                return f"No {kind} found{ns_text}."
            lines = [f"Found {len(items)} {kind}:"]
            for item in items[:50]:  # Cap at 50 to avoid huge output
                meta = item.get("metadata", {})
                ns = meta.get("namespace", "")
                name = meta.get("name", "")
                status = ""
                if "status" in item:
                    s = item["status"]
                    phase = s.get("phase", "")
                    ready = s.get("readyReplicas", "")
                    replicas = s.get("replicas", "")
                    if phase:
                        status = f"  phase={phase}"
                    if ready is not None and replicas is not None and ready != "" and replicas != "":
                        status += f"  ready={ready}/{replicas}"
                lines.append(f"  {ns}/{name}{status}")
            if len(items) > 50:
                lines.append(f"  ... and {len(items) - 50} more")
            return "\n".join(lines)
        except json.JSONDecodeError:
            return result.stdout[:3000]

    def _tool_describe_cluster_resource(self, inp: dict[str, Any]) -> str:
        kind = inp["kind"]
        name = inp["name"]
        namespace = inp.get("namespace", "default")
        result = self.cluster.describe_resource(kind, name, namespace)
        return result.summary

    def _tool_get_pod_logs(self, inp: dict[str, Any]) -> str:
        pod_name = inp["pod_name"]
        namespace = inp.get("namespace", "default")
        container = inp.get("container", "")
        tail_lines = inp.get("tail_lines", 100)
        result = self.cluster.get_pod_logs(pod_name, namespace, container, tail_lines)
        return result.summary

    def _tool_get_cluster_events(self, inp: dict[str, Any]) -> str:
        namespace = inp.get("namespace", "default")
        result = self.cluster.get_events(namespace)
        if not result.ok:
            return f"Failed to get events: {result.stderr}"
        try:
            data = json.loads(result.stdout)
            items = data.get("items", [])
            if not items:
                return f"No events in namespace '{namespace}'."
            lines = [f"Recent events in {namespace} ({len(items)}):"]
            for ev in items[-30:]:  # Last 30 events
                reason = ev.get("reason", "")
                msg = ev.get("message", "")[:200]
                obj_kind = ev.get("involvedObject", {}).get("kind", "")
                obj_name = ev.get("involvedObject", {}).get("name", "")
                ev_type = ev.get("type", "Normal")
                count = ev.get("count", 1)
                lines.append(f"  [{ev_type}] {obj_kind}/{obj_name}: {reason} — {msg} (x{count})")
            return "\n".join(lines)
        except json.JSONDecodeError:
            return result.stdout[:3000]

    # ── Prometheus validation ─────────────────────────────────────────

    def _require_prometheus(self) -> PrometheusClient:
        if self.prometheus is None:
            raise RuntimeError(
                "Prometheus client is not configured. "
                "Call find_monitoring_stack first, or pass --prometheus-url."
            )
        return self.prometheus

    def _tool_check_scrape_targets(self, inp: dict[str, Any]) -> str:
        prom = self._require_prometheus()
        summary = prom.scrape_health_summary()
        lines = [
            f"Total scrape targets: {summary['total_targets']}",
            f"Healthy: {summary['healthy']}",
            f"Unhealthy: {summary['unhealthy']}",
            "",
            "Per-job breakdown:",
        ]
        for job, stats in summary.get("jobs", {}).items():
            status = "OK" if stats["down"] == 0 else "DEGRADED"
            lines.append(f"  {job}: {stats['up']}/{stats['total']} up [{status}]")
            if stats["down"] > 0:
                # Fetch details for down targets
                try:
                    targets = prom.get_active_targets_summary()
                    for t in targets:
                        if t["job"] == job and t["health"] == "down":
                            lines.append(f"    DOWN: {t['instance']} — {t['lastError'][:150]}")
                except Exception:
                    pass
        return "\n".join(lines)

    def _tool_validate_metric_exists(self, inp: dict[str, Any]) -> str:
        prom = self._require_prometheus()
        metric_names = inp["metric_names"]
        results = prom.check_metric_batch(metric_names)
        lines = [f"Metric existence check ({len(metric_names)} metrics):"]
        found = 0
        missing = 0
        for name, exists in results.items():
            icon = "FOUND" if exists else "MISSING"
            lines.append(f"  {icon}: {name}")
            if exists:
                found += 1
            else:
                missing += 1
        lines.insert(1, f"  Found: {found}, Missing: {missing}")
        return "\n".join(lines)

    def _tool_run_promql_query(self, inp: dict[str, Any]) -> str:
        prom = self._require_prometheus()
        query = inp["query"]
        result = prom.validate_promql(query)
        if not result.get("valid"):
            return f"Query INVALID: {result.get('error', 'unknown error')}"
        # Also get the actual values
        values = prom.query_value(query)
        if not values:
            return f"Query valid but returned no data.\nExpression: {query}"
        lines = [f"Query returned {len(values)} result(s):"]
        for v in values[:20]:  # Cap output
            metric_labels = v.get("metric", {})
            value = v.get("value", ["", ""])[1] if "value" in v else "N/A"
            label_str = ", ".join(f'{k}="{val}"' for k, val in metric_labels.items())
            lines.append(f"  {{{label_str}}} => {value}")
        if len(values) > 20:
            lines.append(f"  ... and {len(values) - 20} more")
        return "\n".join(lines)

    def _tool_get_prometheus_alerts(self, inp: dict[str, Any]) -> str:
        prom = self._require_prometheus()
        data = prom.get_alerts()
        alerts = data.get("data", {}).get("alerts", [])
        if not alerts:
            return "No alerts currently firing or pending."
        lines = [f"Active alerts ({len(alerts)}):"]
        for a in alerts:
            state = a.get("state", "")
            name = a.get("labels", {}).get("alertname", "unknown")
            severity = a.get("labels", {}).get("severity", "")
            summary = a.get("annotations", {}).get("summary", "")[:200]
            lines.append(f"  [{state.upper()}] {name} (severity={severity})")
            if summary:
                lines.append(f"    {summary}")
        return "\n".join(lines)

    def _tool_get_prometheus_rules(self, inp: dict[str, Any]) -> str:
        prom = self._require_prometheus()
        data = prom.get_rules()
        groups = data.get("data", {}).get("groups", [])
        if not groups:
            return "No alerting/recording rules configured."
        lines = [f"Rule groups ({len(groups)}):"]
        for g in groups:
            name = g.get("name", "")
            rules = g.get("rules", [])
            lines.append(f"\n  Group: {name} ({len(rules)} rules)")
            for r in rules[:15]:
                rtype = r.get("type", "")
                rname = r.get("name", "")
                health = r.get("health", "")
                lines.append(f"    [{rtype}] {rname} (health={health})")
            if len(rules) > 15:
                lines.append(f"    ... and {len(rules) - 15} more")
        return "\n".join(lines)

    # ── Grafana operations ────────────────────────────────────────────

    def _require_grafana(self) -> GrafanaClient:
        if self.grafana is None:
            raise RuntimeError(
                "Grafana client is not configured. "
                "Call find_monitoring_stack first, or pass --grafana-url."
            )
        return self.grafana

    def _tool_list_grafana_dashboards(self, inp: dict[str, Any]) -> str:
        graf = self._require_grafana()
        query = inp.get("query", "")
        dashboards = graf.list_dashboards(query)
        if not dashboards:
            return "No dashboards found in Grafana."
        lines = [f"Grafana dashboards ({len(dashboards)}):"]
        for d in dashboards:
            uid = d.get("uid", "")
            title = d.get("title", "")
            folder = d.get("folderTitle", "General")
            lines.append(f"  [{uid}] {title} (folder: {folder})")
        return "\n".join(lines)

    def _tool_check_grafana_datasources(self, inp: dict[str, Any]) -> str:
        graf = self._require_grafana()
        sources = graf.list_datasources()
        if not sources:
            return "No datasources configured in Grafana."
        lines = [f"Grafana datasources ({len(sources)}):"]
        prom_found = False
        for ds in sources:
            name = ds.get("name", "")
            ds_type = ds.get("type", "")
            uid = ds.get("uid", "")
            is_default = ds.get("isDefault", False)
            default_tag = " [DEFAULT]" if is_default else ""
            lines.append(f"  {name} (type={ds_type}, uid={uid}){default_tag}")
            if ds_type == "prometheus":
                prom_found = True
                url = ds.get("url", "")
                lines.append(f"    URL: {url}")
        if not prom_found:
            lines.append("\n  WARNING: No Prometheus datasource found! Dashboards won't work.")
        return "\n".join(lines)

    def _tool_import_grafana_dashboard(self, inp: dict[str, Any]) -> str:
        graf = self._require_grafana()
        dashboard_id = inp["dashboard_id"]
        folder_title = inp.get("folder_title", "")

        # Resolve datasource
        prom_ds = graf.get_prometheus_datasource()
        ds_uid = prom_ds.get("uid", "") if prom_ds else ""
        ds_name = prom_ds.get("name", "Prometheus") if prom_ds else "Prometheus"

        folder_id = 0
        if folder_title:
            try:
                folder = graf.create_folder(folder_title)
                folder_id = folder.get("id", 0)
            except Exception as exc:
                logger.warning("Could not create folder '%s': %s", folder_title, exc)

        result = graf.import_dashboard_by_id(
            grafana_com_id=dashboard_id,
            datasource_name=ds_name,
            datasource_uid=ds_uid,
            folder_id=folder_id,
        )

        if result.get("success"):
            return (
                f"Dashboard imported successfully.\n"
                f"  ID: {dashboard_id}\n"
                f"  Title: {result.get('title', '')}\n"
                f"  URL: {result.get('imported_url', '')}\n"
                f"  UID: {result.get('imported_uid', '')}"
            )
        return f"Dashboard import FAILED: {result.get('error', 'unknown error')}"

    # ── Fix / remediation ─────────────────────────────────────────────

    def _tool_apply_kubernetes_manifest(self, inp: dict[str, Any]) -> str:
        manifest_yaml = inp["manifest_yaml"]
        namespace = inp.get("namespace", "default")
        try:
            result = self.cluster.apply_manifest(manifest_yaml, namespace)
        except PermissionError as exc:
            return str(exc)
        return result.summary

    # ── Final report ──────────────────────────────────────────────────

    def _tool_generate_validation_report(self, inp: dict[str, Any]) -> str:
        """The agent core intercepts this to parse the structured result."""
        return json.dumps(inp, indent=2)
