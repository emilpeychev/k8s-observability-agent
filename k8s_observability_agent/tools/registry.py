
"""Tool definitions exposed to the Claude agent via function-calling."""

from __future__ import annotations

import json
from typing import Any

from k8s_observability_agent.classifier import get_profile
from k8s_observability_agent.models import Platform

# ──────────────────────────── Tool Schemas ─────────────────────────────────
# Each schema follows the Anthropic tool-use format.

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_resources",
        "description": (
            "List all Kubernetes resources discovered in the repository. "
            "Optionally filter by kind (e.g. Deployment, Service) or namespace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Filter by resource kind (e.g. 'Deployment'). Leave empty for all.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Filter by namespace. Leave empty for all.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_resource_detail",
        "description": (
            "Get detailed information about a specific Kubernetes resource, "
            "including containers, probes, resource limits, labels, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "qualified_name": {
                    "type": "string",
                    "description": "The qualified name in the form namespace/Kind/name.",
                },
            },
            "required": ["qualified_name"],
        },
    },
    {
        "name": "get_relationships",
        "description": (
            "Get the relationships between Kubernetes resources "
            "(Service→Deployment selectors, Ingress→Service routing, HPA→Deployment scaling)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource": {
                    "type": "string",
                    "description": "Optional qualified name to filter relationships involving this resource.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_platform_summary",
        "description": "Get a high-level summary of the entire Kubernetes platform with resource counts and namespaces.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "check_health_gaps",
        "description": (
            "Identify observability gaps: workloads missing probes, resource limits, "
            "services without matching workloads, AND archetype-specific gaps like "
            "missing exporters (e.g. postgres_exporter for PostgreSQL databases) or "
            "missing configuration for known workload types."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_workload_insights",
        "description": (
            "Get archetype-specific observability knowledge for the workloads in this platform. "
            "Returns, for each classified workload, the recommended Prometheus exporter, "
            "golden metrics with PromQL queries, alert rules with expressions, "
            "dashboard tags, and operational recommendations. "
            "This is the KEY tool for producing intelligent, domain-specific alerts "
            "instead of generic ones. ALWAYS call this before generating the plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "qualified_name": {
                    "type": "string",
                    "description": "Optional: get insights for a specific workload only. Leave empty for all.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "generate_observability_plan",
        "description": (
            "Generate the final observability plan including Prometheus alert rules, "
            "recommended metrics, Grafana dashboard specifications, and ready-made "
            "Grafana community dashboard recommendations. "
            "Call this after you have analysed the platform."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "platform_summary": {
                    "type": "string",
                    "description": "Your textual summary of the platform.",
                },
                "metrics": {
                    "type": "array",
                    "description": "List of recommended metrics.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "metric_name": {"type": "string"},
                            "description": {"type": "string"},
                            "query": {"type": "string"},
                            "resource": {"type": "string"},
                        },
                        "required": ["metric_name", "query", "resource"],
                    },
                },
                "alerts": {
                    "type": "array",
                    "description": "List of recommended Prometheus alert rules.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "alert_name": {"type": "string"},
                            "severity": {"type": "string"},
                            "expr": {"type": "string"},
                            "for_duration": {"type": "string"},
                            "summary": {"type": "string"},
                            "description": {"type": "string"},
                            "resource": {"type": "string"},
                            "nodata_state": {
                                "type": "string",
                                "enum": ["ok", "alerting", "nodata"],
                                "description": (
                                    "Behaviour when the metric is absent. "
                                    "'ok' = silence (default for optional/exporter metrics), "
                                    "'alerting' = fire alert (use for critical infrastructure "
                                    "where missing data likely means failure), "
                                    "'nodata' = mark as no-data state."
                                ),
                            },
                        },
                        "required": ["alert_name", "expr"],
                    },
                },
                "dashboards": {
                    "type": "array",
                    "description": "List of Grafana dashboard specifications.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "panels": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "panel_type": {"type": "string"},
                                        "queries": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "description": {"type": "string"},
                                        "resource": {"type": "string"},
                                    },
                                    "required": ["title", "queries"],
                                },
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["title", "panels"],
                    },
                },
                "dashboard_recommendations": {
                    "type": "array",
                    "description": (
                        "Ready-made Grafana community dashboards to recommend for import. "
                        "Use the dashboard IDs and URLs from get_workload_insights."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "dashboard_id": {
                                "type": "integer",
                                "description": "grafana.com dashboard ID",
                            },
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "url": {"type": "string"},
                            "resource": {
                                "type": "string",
                                "description": "Qualified name of the K8s resource",
                            },
                            "archetype": {
                                "type": "string",
                                "description": "Workload archetype",
                            },
                        },
                        "required": ["dashboard_id", "title"],
                    },
                },
                "recommendations": {
                    "type": "array",
                    "description": "Free-form textual recommendations.",
                    "items": {"type": "string"},
                },
            },
            "required": ["platform_summary", "metrics", "alerts", "dashboards", "recommendations"],
        },
    },
]


# ──────────────────────────── Tool Implementations ─────────────────────────


def _list_resources(platform: Platform, kind: str = "", namespace: str = "") -> str:
    filtered = platform.resources
    if kind:
        filtered = [r for r in filtered if r.kind.lower() == kind.lower()]
    if namespace:
        filtered = [r for r in filtered if r.namespace == namespace]
    if not filtered:
        return "No resources matched the filter."
    lines = [f"Found {len(filtered)} resource(s):\n"]
    for r in filtered:
        extras = []
        if r.replicas is not None:
            extras.append(f"replicas={r.replicas}")
        if r.service_type:
            extras.append(f"type={r.service_type}")
        extra_str = f"  ({', '.join(extras)})" if extras else ""
        lines.append(f"  • {r.qualified_name}{extra_str}  [source: {r.source_file}]")
    return "\n".join(lines)


def _get_resource_detail(platform: Platform, qualified_name: str) -> str:
    for r in platform.resources:
        if r.qualified_name == qualified_name:
            info = r.model_dump(exclude={"raw"})
            return json.dumps(info, indent=2, default=str)
    return f"Resource '{qualified_name}' not found."


def _get_relationships(platform: Platform, resource: str = "") -> str:
    rels = platform.relationships
    if resource:
        rels = [r for r in rels if resource in (r.source, r.target)]
    if not rels:
        return "No relationships found."
    lines = [f"Found {len(rels)} relationship(s):\n"]
    for r in rels:
        lines.append(f"  {r.source}  --[{r.rel_type}]-->  {r.target}")
    return "\n".join(lines)


def _get_platform_summary(platform: Platform) -> str:
    summary = platform.summary()
    lines = [
        f"Repository: {platform.repo_path}",
        f"Namespaces: {', '.join(platform.namespaces)}",
        f"Total resources: {len(platform.resources)}",
        f"Manifest files: {len(platform.manifest_files)}",
        "",
        "Resource counts:",
    ]
    for kind, count in sorted(summary.items()):
        lines.append(f"  {kind}: {count}")

    # Platform-wide observability readiness
    workloads = platform.workloads
    if workloads:
        ready = 0
        partial = 0
        not_ready = 0
        for wl in workloads:
            has_exporter = any(
                t.startswith("exporter:") or t == "builtin_metrics" for t in wl.telemetry
            )
            has_scrape = any(
                t == "scrape_annotations" or t.startswith("metrics_port:") for t in wl.telemetry
            )
            if has_exporter and has_scrape:
                ready += 1
            elif has_exporter or has_scrape:
                partial += 1
            else:
                not_ready += 1
        total = len(workloads)
        lines.append("")
        lines.append("Observability Readiness:")
        lines.append(f"  READY:     {ready}/{total} workloads (exporter + scrape path)")
        lines.append(f"  PARTIAL:   {partial}/{total} workloads (exporter OR scrape, not both)")
        lines.append(f"  NOT READY: {not_ready}/{total} workloads (no metrics exposure)")

    return "\n".join(lines)


def _check_health_gaps(platform: Platform) -> str:
    gaps: list[str] = []
    for wl in platform.workloads:
        for c in wl.containers:
            if not c.liveness_probe:
                gaps.append(
                    f"  ⚠ {wl.qualified_name} / container '{c.name}': missing liveness probe"
                )
            if not c.readiness_probe:
                gaps.append(
                    f"  ⚠ {wl.qualified_name} / container '{c.name}': missing readiness probe"
                )
            if not c.resource_requests:
                gaps.append(
                    f"  ⚠ {wl.qualified_name} / container '{c.name}': no resource requests set"
                )
            if not c.resource_limits:
                gaps.append(
                    f"  ⚠ {wl.qualified_name} / container '{c.name}': no resource limits set"
                )

            # Archetype-specific gaps
            if c.archetype != "custom-app":
                profile_key = (
                    c.archetype_display.lower().replace(" ", "_").replace("/", "_")
                    if c.archetype_display
                    else c.archetype
                )
                profile = get_profile(profile_key)
                if profile:
                    # Use capability-inferred telemetry to check for exporter
                    has_exporter = any(
                        t.startswith("exporter:") or t == "builtin_metrics" for t in wl.telemetry
                    )
                    if profile.exporter and not has_exporter:
                        gaps.append(
                            f"  ⚠ {wl.qualified_name} / '{c.name}' ({profile.display_name}): "
                            f"missing {profile.exporter} — domain metrics will not be available. "
                            f"All {profile.display_name}-specific alerts require this exporter."
                        )
                    for req in profile.health_requirements:
                        gaps.append(
                            f"  ℹ {wl.qualified_name} / '{c.name}' ({profile.display_name}): {req}"
                        )

    # Services with no matching workload
    workload_qnames = {r.qualified_name for r in platform.workloads}
    for svc in platform.services:
        has_target = any(
            rel.source == svc.qualified_name and rel.target in workload_qnames
            for rel in platform.relationships
        )
        if not has_target and svc.selector:
            gaps.append(f"  ⚠ {svc.qualified_name}: selector does not match any workload")

    if not gaps:
        return "No observability gaps detected — all workloads have probes and resource specs."

    # Platform-level insight: ServiceMonitor / PodMonitor presence
    if platform.has_service_monitors:
        gaps.insert(
            0,
            "  ℹ ServiceMonitor/PodMonitor resources detected — advanced Prometheus Operator scraping is configured.",
        )
    else:
        gaps.append(
            "  ℹ No ServiceMonitor/PodMonitor resources found — consider adding them for Prometheus Operator auto-discovery."
        )

    return f"Found {len(gaps)} gap(s):\n" + "\n".join(gaps)


def _check_requires(requires: str, wl: Any) -> bool:
    """Check whether a signal's prerequisite is met by the workload.

    Supports comma-separated compound requirements (ALL must be met):
    * ``""``            — always applicable (no prerequisite)
    * ``"replicas>1"``  — only relevant if the workload has >1 replica
    * ``"statefulset"`` — only relevant if the workload is a StatefulSet
    * ``"exporter"``    — only relevant if an exporter sidecar (or built-in
                          metrics source) was detected in the pod spec
    * ``"exporter,replicas>1"`` — both conditions must be true

    Returns *True* if the signal should be included as-is, *False* if it
    should be annotated as conditional / skipped.
    """
    if not requires:
        return True

    # Compound requirements — all must pass
    parts = [r.strip().lower() for r in requires.split(",")]
    return all(_check_single_req(p, wl) for p in parts)


def _check_single_req(req: str, wl: Any) -> bool:
    """Evaluate a single prerequisite token."""
    if req == "replicas>1":
        return (wl.replicas or 1) > 1
    if req == "statefulset":
        return wl.kind == "StatefulSet"
    if req == "exporter":
        # Check telemetry capabilities populated by the scanner
        telemetry = getattr(wl, "telemetry", [])
        return any(t.startswith("exporter:") or t == "builtin_metrics" for t in telemetry)
    # Unknown prerequisite — include but let the LLM decide
    return True


def _unmet_reason(requires: str, wl: Any, profile: Any) -> str:
    """Build a human-readable explanation of why a requirement is not met,
    including specific remediation steps."""
    parts = [r.strip().lower() for r in requires.split(",")]
    reasons: list[str] = []
    for p in parts:
        if p == "exporter" and not _check_single_req(p, wl):
            exporter_name = getattr(profile, "exporter", "") if profile else ""
            if exporter_name:
                reasons.append(f"deploy {exporter_name} sidecar")
            else:
                reasons.append("deploy a metrics exporter sidecar")
        elif p == "replicas>1" and not _check_single_req(p, wl):
            reasons.append(f"replicas={wl.replicas or 1}, need >1")
        elif p == "statefulset" and not _check_single_req(p, wl):
            reasons.append(f"kind={wl.kind}, need StatefulSet")
    if not reasons:
        return requires
    return "; ".join(reasons)


def _get_workload_insights(platform: Platform, qualified_name: str = "") -> str:
    """Return archetype-specific observability knowledge for workloads."""
    workloads = platform.workloads
    if qualified_name:
        workloads = [w for w in workloads if w.qualified_name == qualified_name]
        if not workloads:
            return f"Workload '{qualified_name}' not found."

    sections: list[str] = []
    for wl in workloads:
        for c in wl.containers:
            header = f"\n{'=' * 60}\n{wl.qualified_name} / container '{c.name}'\n{'=' * 60}"
            header += f"\nImage: {c.image}"
            header += f"\nArchetype: {c.archetype}"
            if c.archetype_display:
                header += f" ({c.archetype_display})"
            header += f"\nConfidence: {c.archetype_confidence} (score: {c.archetype_score:.2f})"
            header += f"\nPrimary signal: {c.archetype_match_source}"
            if c.archetype_evidence:
                header += f"\nEvidence: {' + '.join(c.archetype_evidence)}"

            # Telemetry capabilities
            if wl.telemetry:
                header += f"\nTelemetry capabilities: {', '.join(wl.telemetry)}"
            else:
                header += (
                    "\nTelemetry capabilities: NONE DETECTED — domain metrics are NOT collectable"
                )

            # Observability readiness verdict
            has_exporter = any(
                t.startswith("exporter:") or t == "builtin_metrics" for t in wl.telemetry
            )
            has_scrape = any(
                t == "scrape_annotations" or t.startswith("metrics_port:") for t in wl.telemetry
            )
            if has_exporter and has_scrape:
                header += (
                    "\nObservability readiness: READY — exporter present + scrape path configured"
                )
            elif has_exporter:
                header += "\nObservability readiness: PARTIAL — exporter present but no scrape annotations/ServiceMonitor detected"
            elif has_scrape:
                header += "\nObservability readiness: PARTIAL — scrape config exists but no known exporter detected"
            else:
                header += "\nObservability readiness: NOT READY — no metrics exposure detected"

            # Look up the profile by registry key, falling back to archetype
            profile_key = (
                c.archetype_display.lower().replace(" ", "_").replace("/", "_")
                if c.archetype_display
                else c.archetype
            )
            profile = get_profile(profile_key)
            if profile is None:
                header += (
                    "\n\nNo archetype profile available — this appears to be a custom application."
                )
                if c.archetype_score < 0.25:
                    header += "\nDetection certainty is very low. Use generic Kubernetes metrics: "
                else:
                    header += "\nUse generic Kubernetes metrics: "
                header += "container_cpu_usage_seconds_total, "
                header += "container_memory_working_set_bytes, kube_pod_status_phase, "
                header += "kube_deployment_status_replicas_unavailable."
                sections.append(header)
                continue

            lines = [header]
            lines.append(f"\nDescription: {profile.description}")

            if profile.exporter:
                lines.append(
                    f"\nRequired exporter: {profile.exporter} (port {profile.exporter_port})"
                )

            if profile.golden_metrics:
                lines.append("\n--- Golden Metrics ---")
                for m in profile.golden_metrics:
                    if m.requires and not _check_requires(m.requires, wl):
                        lines.append(f"  • {m.name}: {m.description}")
                        lines.append(f"    PromQL: {m.query}")
                        fix = _unmet_reason(m.requires, wl, profile)
                        lines.append(f"    ⚠ CONDITIONAL — not collectable: {fix}")
                    else:
                        lines.append(f"  • {m.name}: {m.description}")
                        lines.append(f"    PromQL: {m.query}")

            if profile.alerts:
                lines.append("\n--- Recommended Alerts ---")
                for a in profile.alerts:
                    nodata_label = f" [nodata→{a.nodata_state}]" if a.nodata_state != "ok" else ""
                    if a.requires and not _check_requires(a.requires, wl):
                        lines.append(f"  • {a.name} [{a.severity}] (for: {a.for_duration}){nodata_label}")
                        lines.append(f"    expr: {a.expr}")
                        lines.append(f"    summary: {a.summary}")
                        fix = _unmet_reason(a.requires, wl, profile)
                        lines.append(f"    ⚠ CONDITIONAL — not collectable: {fix}")
                    else:
                        lines.append(f"  • {a.name} [{a.severity}] (for: {a.for_duration}){nodata_label}")
                        lines.append(f"    expr: {a.expr}")
                        lines.append(f"    summary: {a.summary}")

            if profile.grafana_dashboards:
                lines.append("\n--- Recommended Grafana Dashboards (ready to import) ---")
                for gd in profile.grafana_dashboards:
                    lines.append(f"  • ID: {gd.dashboard_id} — {gd.title}")
                    if gd.description:
                        lines.append(f"    {gd.description}")
                    lines.append(f"    Import: {gd.url}")

            if profile.dashboard_tags:
                lines.append(f"\nDashboard tags: {', '.join(profile.dashboard_tags)}")

            if profile.recommendations:
                lines.append("\n--- Recommendations ---")
                for r in profile.recommendations:
                    lines.append(f"  • {r}")

            sections.append("\n".join(lines))

    if not sections:
        return "No workloads found in the platform."
    return "\n".join(sections)


def execute_tool(platform: Platform, tool_name: str, tool_input: dict[str, Any]) -> str:
    """Dispatch a tool call and return the string result."""
    match tool_name:
        case "list_resources":
            return _list_resources(platform, **tool_input)
        case "get_resource_detail":
            return _get_resource_detail(platform, **tool_input)
        case "get_relationships":
            return _get_relationships(platform, **tool_input)
        case "get_platform_summary":
            return _get_platform_summary(platform)
        case "check_health_gaps":
            return _check_health_gaps(platform)
        case "get_workload_insights":
            return _get_workload_insights(platform, **tool_input)
        case "generate_observability_plan":
            # This tool's output is structured — the agent core handles it specially.
            return json.dumps(tool_input, indent=2)
        case _:
            return f"Unknown tool: {tool_name}"
