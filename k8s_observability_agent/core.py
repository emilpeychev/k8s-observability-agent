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
from k8s_observability_agent.config import Settings
from k8s_observability_agent.models import (
    AlertRule,
    DashboardPanel,
    DashboardSpec,
    MetricRecommendation,
    ObservabilityPlan,
    Platform,
)
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
   • Grafana dashboard panels grouped into logical dashboards per archetype/service.
   • Textual best-practice recommendations including required exporters.
4. **Call `generate_observability_plan`** as your final action, passing the \
   complete plan as structured data.

Always start by calling `get_platform_summary`, `list_resources`, and \
`get_workload_insights` to orient yourself, then drill into details as needed. \
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

    return ObservabilityPlan(
        platform_summary=raw.get("platform_summary", ""),
        metrics=metrics,
        alerts=alerts,
        dashboards=dashboards,
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
