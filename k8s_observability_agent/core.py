"""Agent core — drives the Claude agentic loop with tool calling."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import anthropic
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from k8s_observability_agent.analyzer import platform_report
from k8s_observability_agent.cluster import ClusterClient
from k8s_observability_agent.config import Settings
from k8s_observability_agent.grafana import GrafanaClient
from k8s_observability_agent.history import ValidationHistory
from k8s_observability_agent.models import (
    AlertRule,
    DashboardImportResult,
    DashboardPanel,
    DashboardSpec,
    GrafanaDashboardRecommendation,
    MetricRecommendation,
    ObservabilityPlan,
    Platform,
    RemediationStep,
    ValidationCheck,
    ValidationReport,
)
from k8s_observability_agent.prometheus import PrometheusClient
from k8s_observability_agent.tools.live import LIVE_TOOL_DEFINITIONS, LiveToolExecutor
from k8s_observability_agent.tools.registry import TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)
console = Console()

SYSTEM_PROMPT = """\
You are an expert Kubernetes observability engineer. You have been given access \
to a scanned Kubernetes platform extracted from a Git repository. Your job is to:

1. **Understand the platform** — use the provided tools to explore the resources, \
   their relationships, and identify any gaps in health/monitoring configuration.
2. **Classify workloads** — use `get_workload_insights` to retrieve archetype-specific \
   observability knowledge. The system has already classified container images into \
   archetypes (database, cache, message-queue, web-server, etc.) and knows which \
   metrics, alerts, and exporters are appropriate for each. USE THIS DATA. Do NOT \
   generate generic CPU/memory alerts when domain-specific signals exist.
   Each classified workload has a numeric confidence **score** (0.0–1.0):
   • score ≥ 0.60 → HIGH confidence — use technology-specific alerts and metrics.
   • 0.15 ≤ score < 0.60 → MEDIUM confidence — use archetype-level alerts but add \
     a note that the detection is uncertain.
   • score < 0.15 → LOW confidence — fall back to generic Kubernetes metrics \
     and recommend the operator verify the workload type.
3. **Design an observability plan** — for EVERY workload and service, recommend:
   • Key Prometheus metrics to collect (with PromQL queries). For known archetypes \
     (PostgreSQL, Redis, Kafka, etc.), use the archetype-specific golden metrics \
     returned by `get_workload_insights`, not generic container metrics.
   • Alerting rules — use archetype-specific alert expressions (e.g., \
     pg_replication_lag for PostgreSQL, redis eviction rate for Redis, consumer \
     lag for Kafka). Only fall back to generic pod/container alerts for custom apps.
   • **nodata_state calibration** — for every alert rule, set the `nodata_state` field:
     - `"ok"` (default): silence when the metric is absent. Use for optional/exporter \
       metrics that may not exist yet (e.g., exporters not deployed).
     - `"alerting"`: fire the alert when the metric disappears. Use for critical \
       infrastructure where missing data likely means a failure (e.g., replication \
       lag on a multi-replica database, scrape target down).
     - `"nodata"`: enter a special no-data state for triage. Use when absence is \
       ambiguous and needs operator attention.
   • **Ready-made Grafana dashboards** — for every known archetype, `get_workload_insights` \
     returns recommended community dashboard IDs from grafana.com. Include these in \
     `dashboard_recommendations` instead of building panels from scratch. These are \
     battle-tested, maintained dashboards. Only create custom `dashboards` panels for \
     custom applications or edge cases where no community dashboard exists.
   • Textual best-practice recommendations including required exporters.
4. **Call `generate_observability_plan`** as your final action, passing the \
   complete plan as structured data. Include `dashboard_recommendations` with \
   the community dashboard IDs from `get_workload_insights`.

Always start by calling `get_platform_summary`, `list_resources`, and \
`get_workload_insights` to orient yourself, then drill into details as needed. \
If the platform summary mentions IaC resources (Terraform, Helm, Kustomize, Pulumi), \
call `get_iac_resources` to discover infrastructure dependencies like managed databases, \
caches, and message queues. These infrastructure resources need monitoring too — include \
the recommended exporters and dashboards from the IaC analysis in your plan. \
If the platform summary mentions AWS live resources, call `get_aws_resources` to see \
the actual running AWS infrastructure (RDS, ElastiCache, MSK, Lambda, ECS, etc.). \
Each AWS resource includes monitoring notes — use them to recommend appropriate \
CloudWatch metrics, exporters, and Grafana dashboards. \
Be thorough — cover every workload. For each known archetype, include the \
recommended exporter deployment if not already present.
"""


def _build_initial_messages(platform: Platform) -> list[dict[str, Any]]:
    """Build the initial user message that boots the agent."""
    report = platform_report(platform)
    return [
        {
            "role": "user",
            "content": (
                "I have scanned a Kubernetes Git repository. Here is the platform overview:\n\n"
                f"```\n{report}\n```\n\n"
                "Please analyse this platform thoroughly and generate a comprehensive "
                "observability plan. Use the tools to explore resources in detail, check "
                "for health gaps, and then call `generate_observability_plan` with your "
                "complete recommendations."
            ),
        }
    ]


def _parse_observability_plan(raw: dict[str, Any]) -> ObservabilityPlan:
    """Convert the raw tool output from generate_observability_plan into a model."""
    metrics = [MetricRecommendation(**m) for m in raw.get("metrics", [])]
    alerts = [AlertRule(**a) for a in raw.get("alerts", [])]

    dashboards = []
    for d in raw.get("dashboards", []):
        panels = [DashboardPanel(**p) for p in d.get("panels", [])]
        dashboards.append(
            DashboardSpec(
                title=d["title"],
                description=d.get("description", ""),
                panels=panels,
                tags=d.get("tags", []),
            )
        )

    dashboard_recs = [
        GrafanaDashboardRecommendation(**dr)
        for dr in raw.get("dashboard_recommendations", [])
    ]

    return ObservabilityPlan(
        platform_summary=raw.get("platform_summary", ""),
        metrics=metrics,
        alerts=alerts,
        dashboards=dashboards,
        dashboard_recommendations=dashboard_recs,
        recommendations=raw.get("recommendations", []),
    )


def run_agent(platform: Platform, settings: Settings) -> ObservabilityPlan:
    """Execute the agentic loop and return the generated ObservabilityPlan."""
    settings.validate_api_key()

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    messages = _build_initial_messages(platform)

    plan: ObservabilityPlan | None = None

    MAX_RETRIES = 3

    for turn in range(1, settings.max_agent_turns + 1):
        logger.debug("Agent turn %d", turn)
        console.print(f"\n[dim]── Agent turn {turn} ──[/dim]")

        # ---------- LLM call with retries ----------
        response = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = client.messages.create(
                    model=settings.model,
                    max_tokens=settings.max_tokens,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                )
                break  # success
            except anthropic.RateLimitError as exc:
                wait = 2**attempt
                console.print(
                    f"  [yellow]Rate-limited (attempt {attempt}/{MAX_RETRIES}), "
                    f"retrying in {wait}s …[/yellow]"
                )
                logger.warning("Rate-limited: %s — retrying in %ds", exc, wait)
                time.sleep(wait)
            except anthropic.APIConnectionError as exc:
                console.print(f"\n[red]Connection error: {exc}[/red]")
                logger.error("API connection error: %s", exc)
                if attempt < MAX_RETRIES:
                    time.sleep(2**attempt)
                    continue
                console.print(
                    "[red]Could not reach the Anthropic API. "
                    "Run [bold]k8s-obs scan[/bold] for offline analysis.[/red]"
                )
                return ObservabilityPlan(
                    platform_summary="Agent could not reach the Anthropic API.",
                    recommendations=["Run 'k8s-obs scan' for offline structural analysis."],
                )
            except anthropic.APIStatusError as exc:
                console.print(f"\n[red]API error ({exc.status_code}): {exc.message}[/red]")
                logger.error("API status error: %s", exc)
                return ObservabilityPlan(
                    platform_summary=f"Anthropic API error: {exc.message}",
                    recommendations=["Check your API key and account status, then retry."],
                )

        if response is None:
            console.print("[red]Exhausted retries contacting the API.[/red]")
            return ObservabilityPlan(
                platform_summary="Agent exhausted retries contacting the Anthropic API.",
                recommendations=["Run 'k8s-obs scan' for offline structural analysis."],
            )

        # Process content blocks
        assistant_content: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
                if settings.verbose:
                    console.print(Panel(Markdown(block.text), title="Agent", border_style="blue"))
                else:
                    # Show a condensed version
                    preview = block.text[:200] + "…" if len(block.text) > 200 else block.text
                    console.print(f"  [blue]Agent:[/blue] {preview}")

            elif block.type == "tool_use":
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

                console.print(
                    f"  [yellow]Tool call:[/yellow] {block.name}({json.dumps(block.input, default=str)[:120]})"
                )

                # Execute the tool
                result_str = execute_tool(platform, block.name, block.input)

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    }
                )

                # Check if this was the final plan generation
                if block.name == "generate_observability_plan":
                    try:
                        plan = _parse_observability_plan(block.input)
                        console.print("  [green]✓ Observability plan generated[/green]")
                    except Exception as exc:
                        logger.warning("Failed to parse plan: %s", exc)

        # Append assistant message
        messages.append({"role": "assistant", "content": assistant_content})

        # If there were tool calls, append results and continue the loop
        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        # Stop conditions
        if response.stop_reason == "end_turn" and plan is not None:
            console.print("\n[green bold]✓ Agent completed analysis.[/green bold]")
            break
        if response.stop_reason == "end_turn" and not tool_results:
            # Agent ended without producing a plan via the tool — try to
            # extract any textual advice as recommendations.
            console.print(
                "\n[yellow]Agent finished without calling generate_observability_plan.[/yellow]"
            )
            if plan is None:
                plan = ObservabilityPlan(
                    platform_summary="Agent did not produce a structured plan.",
                    recommendations=["Review the agent output above for recommendations."],
                )
            break
    else:
        console.print(f"\n[red]Agent reached the turn limit ({settings.max_agent_turns}).[/red]")
        if plan is None:
            plan = ObservabilityPlan(
                platform_summary="Agent did not complete within the turn limit.",
            )

    return plan


# ═══════════════════════════════════════════════════════════════════════════
# Validation Agent — live cluster testing & remediation
# ═══════════════════════════════════════════════════════════════════════════

VALIDATE_SYSTEM_PROMPT = """\
You are an expert Kubernetes observability engineer performing live validation \
on a cluster. You have access to tools that let you interact with kubectl, \
Prometheus, and Grafana. Your goal is to **fully replace a human observability \
engineer** by:

1. **Discover the monitoring stack** — call `find_monitoring_stack` to locate \
   Prometheus and Grafana instances. If they're not found, flag this as a \
   critical issue.

2. **Inspect the cluster** — use `get_cluster_resources` and \
   `describe_cluster_resource` to understand what's running. Look for workloads, \
   exporters, ServiceMonitors, and PodMonitors.

3. **Validate scrape targets** — call `check_scrape_targets` to verify that \
   Prometheus is successfully scraping all expected targets. Identify any \
   targets that are down and diagnose using pod logs and events.

4. **Validate metrics** — for each expected metric (from the observability \
   plan or archetype knowledge), use `validate_metric_exists` to confirm the \
   metric is being collected. If critical metrics are missing, diagnose why \
   (exporter not deployed? wrong port? scrape config missing?).

5. **Test alert expressions** — use `run_promql_query` to evaluate alert \
   expressions and verify they work correctly. Check for syntax errors, \
   missing labels, and queries that return no data.

6. **Check Grafana** — verify datasources with `check_grafana_datasources`, \
   list existing dashboards, and import recommended community dashboards \
   using `import_grafana_dashboard`.

7. **Diagnose and fix issues** — when you find problems:
   - Check pod logs for error messages
   - Check events for CrashLoopBackOff, ImagePullBackOff, etc.
   - If --allow-writes is enabled, use `apply_kubernetes_manifest` to deploy \
     missing exporters, create ServiceMonitors, or fix configurations.
   - If writes are disabled, document exactly what needs to be done.

8. **Generate a validation report** — call `generate_validation_report` as \
   your final action, with:
   - A summary of the cluster
   - A list of all checks performed (pass/fail/warn)
   - Whether fixes were applied
   - Remaining recommendations for the operator
   - **Concrete remediation_steps** — for EVERY failed or warned check, \
     provide a remediation step with:
     * A clear title and description explaining the root cause
     * The full YAML manifest to fix it (e.g. exporter sidecar Deployment, \
       ServiceMonitor, ConfigMap patch)
     * Or a shell command if a manifest is not applicable
     * Priority (high/medium/low)
   - **dashboards_to_import** — for each workload type detected, recommend \
     the specific Grafana community dashboard ID. Use these known good IDs:
     * Node Exporter Full: 1860
     * Kubernetes cluster monitoring: 315
     * PostgreSQL: 9628
     * MySQL: 7362
     * MongoDB: 2583
     * Redis: 11835
     * Elasticsearch: 4358
     * Kafka: 7589
     * RabbitMQ: 10991
     * NATS: 2279
     * NGINX: 9614
     * ArgoCD: 14584
     * Istio mesh: 7639
     * Istio performance: 11829
     * CoreDNS: 14981
     * Cert-Manager: 11001
     * MinIO: 13502
     * Harbor: 14075
     * Tekton: 15698

IMPORTANT: For each `fix_manifest` in a check or `manifest` in a remediation \
step, provide complete, ready-to-apply YAML. The operator should be able to \
copy-paste it into `kubectl apply -f -` and have it work. Include the \
namespace, labels, image, ports, and any required configuration.

Be thorough and systematic. Check EVERY workload. Don't just verify that \
things exist — verify they WORK. A metric existing doesn't mean it has \
meaningful values. An exporter pod running doesn't mean it's scraping correctly.

When you find issues, explain the ROOT CAUSE, not just the symptom. \
For example: "postgresql_exporter has no data because the ServiceMonitor \
targetting port 9187 doesn't match the pod's label selector" is better \
than "postgresql metrics are missing".
"""


def _build_validate_messages(
    plan: ObservabilityPlan | None = None,
    *,
    prometheus_url: str = "",
    grafana_url: str = "",
    previous_run_summary: str = "",
) -> list[dict[str, Any]]:
    """Build the initial message for the validation agent."""
    content_parts: list[str] = [
        "I need you to validate the observability setup on my live Kubernetes cluster.",
    ]

    if previous_run_summary:
        content_parts.append("")
        content_parts.append(
            "IMPORTANT — A previous validation run is available.  Re-check the "
            "previously failing items first instead of re-discovering everything "
            "from scratch. Only run NEW checks if you finish re-verifying the "
            "earlier issues."
        )
        content_parts.append("")
        content_parts.append(previous_run_summary)

    if prometheus_url or grafana_url:
        content_parts.append("")
        content_parts.append("The following monitoring endpoints are already configured and reachable:")
        if prometheus_url:
            content_parts.append(f"  - Prometheus: {prometheus_url}")
        if grafana_url:
            content_parts.append(f"  - Grafana: {grafana_url}")
        content_parts.append("")
        content_parts.append(
            "Use these URLs directly when calling tools like check_scrape_targets, "
            "validate_metric_exists, run_promql_query, list_grafana_dashboards, etc. "
            "TLS is already handled. Do NOT suggest port-forwarding for these URLs."
        )
    else:
        content_parts.append("")
        content_parts.append(
            "No monitoring URLs were provided. Use find_monitoring_stack to discover them. "
            "If the discovered in-cluster URLs are not reachable from the agent, "
            "suggest port-forwarding as a fallback."
        )

    content_parts.extend([
        "",
        "Please:",
        "1. Connect to the cluster and discover the monitoring stack",
        "2. Check that all scrape targets are healthy",
        "3. Verify that expected metrics are being collected",
        "4. Test alert expressions",
        "5. Import recommended dashboards into Grafana",
        "6. Diagnose and fix any issues you find",
        "7. Generate a validation report",
    ])

    if plan:
        content_parts.append("")
        content_parts.append("Here is the observability plan that was generated for this platform:")
        content_parts.append("")

        # Include expected metrics
        if plan.metrics:
            content_parts.append("Expected metrics:")
            for m in plan.metrics:
                content_parts.append(f"  - {m.metric_name} ({m.resource})")
            content_parts.append("")

        # Include alert expressions to test
        if plan.alerts:
            content_parts.append("Alert rules to validate:")
            for a in plan.alerts:
                content_parts.append(f"  - {a.alert_name}: {a.expr}")
            content_parts.append("")

        # Include dashboards to import
        if plan.dashboard_recommendations:
            content_parts.append("Dashboards to import:")
            for dr in plan.dashboard_recommendations:
                content_parts.append(f"  - ID {dr.dashboard_id}: {dr.title}")
            content_parts.append("")

    return [{"role": "user", "content": "\n".join(content_parts)}]


def _parse_validation_report(raw: dict[str, Any]) -> ValidationReport:
    """Convert the raw tool output from generate_validation_report into a model."""
    checks = [ValidationCheck(**c) for c in raw.get("checks", [])]
    dashboards = [DashboardImportResult(**d) for d in raw.get("dashboards_imported", [])]
    remediation = [RemediationStep(**s) for s in raw.get("remediation_steps", [])]
    dashboards_to_import = [
        DashboardImportResult(**d) for d in raw.get("dashboards_to_import", [])
    ]
    return ValidationReport(
        cluster_summary=raw.get("cluster_summary", ""),
        checks=checks,
        dashboards_imported=dashboards,
        recommendations=raw.get("recommendations", []),
        remediation_steps=remediation,
        dashboards_to_import=dashboards_to_import,
    )


def run_validate_agent(
    settings: Settings,
    plan: ObservabilityPlan | None = None,
    history: ValidationHistory | None = None,
) -> ValidationReport:
    """Run the validation agent against a live cluster.

    Parameters
    ----------
    settings : Settings
        Application settings including cluster connection info.
    plan : ObservabilityPlan | None
        Optional observability plan to validate against. If provided,
        the agent will specifically check that the metrics, alerts, and
        dashboards from the plan are working on the cluster.

    Returns
    -------
    ValidationReport
        The validation results.
    """
    settings.validate_api_key()

    # ── Build clients ──────────────────────────────────────────────────
    cluster = ClusterClient(
        kubeconfig=settings.kubeconfig,
        context=settings.kube_context,
        allow_writes=settings.allow_writes,
    )

    prometheus: PrometheusClient | None = None
    if settings.prometheus_url:
        prometheus = PrometheusClient(
            settings.prometheus_url, ca_cert=settings.ca_cert,
        )

    grafana: GrafanaClient | None = None
    if settings.grafana_url:
        grafana = GrafanaClient(
            settings.grafana_url,
            api_key=settings.grafana_api_key,
            password=settings.grafana_password,
            ca_cert=settings.ca_cert,
        )

    executor = LiveToolExecutor(
        cluster=cluster,
        prometheus=prometheus,
        grafana=grafana,
        ca_cert=settings.ca_cert,
    )

    # ── Load previous run from history ──────────────────────────────
    cluster_context = settings.kube_context or "default"
    previous_summary = ""
    if history is not None:
        previous_summary = history.previous_run_summary(cluster_context)
        if previous_summary:
            console.print(
                "  [cyan]Loaded previous validation run from history — "
                "agent will re-check known issues first.[/cyan]"
            )

    # ── Agentic loop ───────────────────────────────────────────────────
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    messages = _build_validate_messages(
        plan,
        prometheus_url=settings.prometheus_url,
        grafana_url=settings.grafana_url,
        previous_run_summary=previous_summary,
    )

    report: ValidationReport | None = None
    MAX_RETRIES = 3

    # Combine repo analysis tools + live tools so the agent has full context
    all_tools = TOOL_DEFINITIONS + LIVE_TOOL_DEFINITIONS

    for turn in range(1, settings.max_agent_turns + 1):
        logger.debug("Validate agent turn %d", turn)
        console.print(f"\n[dim]── Validate turn {turn} ──[/dim]")

        response = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = client.messages.create(
                    model=settings.model,
                    max_tokens=settings.max_tokens,
                    system=VALIDATE_SYSTEM_PROMPT,
                    tools=all_tools,
                    messages=messages,
                )
                break
            except anthropic.RateLimitError as exc:
                wait = 2**attempt
                console.print(
                    f"  [yellow]Rate-limited (attempt {attempt}/{MAX_RETRIES}), "
                    f"retrying in {wait}s …[/yellow]"
                )
                time.sleep(wait)
            except anthropic.APIConnectionError as exc:
                console.print(f"\n[red]Connection error: {exc}[/red]")
                if attempt < MAX_RETRIES:
                    time.sleep(2**attempt)
                    continue
                return ValidationReport(
                    cluster_summary="Agent could not reach the Anthropic API.",
                    recommendations=["Check API connectivity and retry."],
                )
            except anthropic.APIStatusError as exc:
                console.print(f"\n[red]API error ({exc.status_code}): {exc.message}[/red]")
                return ValidationReport(
                    cluster_summary=f"Anthropic API error: {exc.message}",
                    recommendations=["Check your API key and account status."],
                )

        if response is None:
            return ValidationReport(
                cluster_summary="Agent exhausted retries contacting the API.",
                recommendations=["Retry later."],
            )

        # Process content blocks
        assistant_content: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
                if settings.verbose:
                    console.print(Panel(Markdown(block.text), title="Agent", border_style="cyan"))
                else:
                    preview = block.text[:200] + "…" if len(block.text) > 200 else block.text
                    console.print(f"  [cyan]Agent:[/cyan] {preview}")

            elif block.type == "tool_use":
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

                console.print(
                    f"  [yellow]Tool call:[/yellow] {block.name}("
                    f"{json.dumps(block.input, default=str)[:120]})"
                )

                # Route to live executor or repo tools
                if block.name in {t["name"] for t in LIVE_TOOL_DEFINITIONS}:
                    result_str = executor.execute(block.name, block.input)
                else:
                    # For repo-analysis tools, we need a platform — skip if not available
                    result_str = f"Tool '{block.name}' requires a scanned platform (use 'analyze' first)."

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    }
                )

                # Check for final report
                if block.name == "generate_validation_report":
                    try:
                        report = _parse_validation_report(block.input)
                        console.print("  [green]✓ Validation report generated[/green]")
                    except Exception as exc:
                        logger.warning("Failed to parse report: %s", exc)

        messages.append({"role": "assistant", "content": assistant_content})

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        # Stop conditions
        if response.stop_reason == "end_turn" and report is not None:
            console.print("\n[green bold]✓ Validation complete.[/green bold]")
            break
        if response.stop_reason == "end_turn" and not tool_results:
            console.print(
                "\n[yellow]Agent finished without generating a validation report.[/yellow]"
            )
            if report is None:
                report = ValidationReport(
                    cluster_summary="Agent did not produce a structured report.",
                    recommendations=["Review the agent output above."],
                )
            break
    else:
        console.print(f"\n[red]Agent reached the turn limit ({settings.max_agent_turns}).[/red]")
        if report is None:
            report = ValidationReport(
                cluster_summary="Agent did not complete within the turn limit.",
            )

    # ── Save run to history ─────────────────────────────────────────
    if history is not None and report is not None:
        try:
            run_id = history.save_run(cluster_context, report)
            history.prune(cluster_context, keep=20)
            console.print(f"  [dim]Saved validation run #{run_id} to history.[/dim]")
        except Exception as exc:
            logger.warning("Failed to save validation history: %s", exc)

    # Clean up clients
    if prometheus:
        prometheus.close()
    if grafana:
        grafana.close()

    return report
