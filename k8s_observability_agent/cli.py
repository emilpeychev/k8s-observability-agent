"""CLI entry-point for the k8s-observability-agent."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from k8s_observability_agent import __version__
from k8s_observability_agent.analyzer import build_platform, platform_report
from k8s_observability_agent.config import Settings
from k8s_observability_agent.core import run_agent, run_validate_agent
from k8s_observability_agent.renderer import write_outputs
from k8s_observability_agent.scanner import scan_repository

console = Console()


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quieten noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("git").setLevel(logging.WARNING)


@click.group()
@click.version_option(version=__version__, prog_name="k8s-obs")
def main() -> None:
    """K8s Observability Agent — AI-powered infrastructure analysis."""


@main.command()
@click.argument("repo", default=".")
@click.option(
    "--github", "github_url", default="", help="Clone from a GitHub URL instead of a local path."
)
@click.option("--branch", default="main", help="Git branch to checkout (used with --github).")
@click.option(
    "--output", "-o", "output_dir", default="observability-output", help="Output directory."
)
@click.option("--model", default="claude-sonnet-4-20250514", help="Anthropic model to use.")
@click.option("--api-key", default="", help="Anthropic API key (or set ANTHROPIC_API_KEY env var).")
@click.option("--max-turns", default=30, type=int, help="Maximum agent reasoning turns.")
@click.option("--verbose", "-v", is_flag=True, help="Show full agent reasoning.")
def analyze(
    repo: str,
    github_url: str,
    branch: str,
    output_dir: str,
    model: str,
    api_key: str,
    max_turns: int,
    verbose: bool,
) -> None:
    """Analyse a K8s Git repository and generate an observability plan.

    REPO is a local path to a Git repository containing Kubernetes manifests.
    Use --github to clone a remote repository instead.
    """
    _configure_logging(verbose)

    settings = Settings(
        repo_path=repo,
        github_url=github_url,
        branch=branch,
        output_dir=output_dir,
        model=model,
        max_agent_turns=max_turns,
        verbose=verbose,
    )
    if api_key:
        settings.anthropic_api_key = api_key

    try:
        settings.validate_api_key()
    except ValueError as exc:
        console.print(f"[red bold]Error:[/red bold] {exc}")
        sys.exit(1)

    # ── Step 1: Scan ──────────────────────────────────────────────────────
    console.print(Panel("Step 1 / 3  —  Scanning repository", style="bold cyan"))
    try:
        resources, manifest_files, errors = scan_repository(settings)
    except FileNotFoundError as exc:
        console.print(f"[red bold]Error:[/red bold] {exc}")
        sys.exit(1)

    if not resources:
        console.print("[yellow]No Kubernetes resources found in the repository.[/yellow]")
        sys.exit(0)

    console.print(
        f"  Found [green]{len(resources)}[/green] resources in {len(manifest_files)} manifest files."
    )

    # ── Step 2: Analyse ───────────────────────────────────────────────────
    console.print(Panel("Step 2 / 3  —  Running AI analysis", style="bold cyan"))
    platform = build_platform(resources, manifest_files, errors, repo_path=repo)

    if verbose:
        console.print(platform_report(platform))

    plan = run_agent(platform, settings)

    # ── Step 3: Render ────────────────────────────────────────────────────
    console.print(Panel("Step 3 / 3  —  Generating outputs", style="bold cyan"))
    written = write_outputs(plan, Path(output_dir).resolve())

    console.print()
    console.print("[green bold]Done![/green bold] Files written:")
    for f in written:
        console.print(f"  • {f}")


@main.command()
@click.argument("repo", default=".")
@click.option(
    "--github", "github_url", default="", help="Clone from a GitHub URL instead of a local path."
)
@click.option("--branch", default="main", help="Git branch to checkout.")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output.")
def scan(
    repo: str,
    github_url: str,
    branch: str,
    verbose: bool,
) -> None:
    """Scan a repository and print a platform summary (no AI analysis)."""
    _configure_logging(verbose)

    settings = Settings(
        repo_path=repo,
        github_url=github_url,
        branch=branch,
    )

    try:
        resources, manifest_files, errors = scan_repository(settings)
    except FileNotFoundError as exc:
        console.print(f"[red bold]Error:[/red bold] {exc}")
        sys.exit(1)

    if not resources:
        console.print("[yellow]No Kubernetes resources found.[/yellow]")
        sys.exit(0)

    platform = build_platform(resources, manifest_files, errors, repo_path=repo)
    console.print(platform_report(platform))


@main.command()
@click.option(
    "--kubeconfig", default="", help="Path to kubeconfig file (default: ~/.kube/config)."
)
@click.option("--context", "kube_context", default="", help="Kubernetes context to use.")
@click.option(
    "--prometheus-url", default="", help="Prometheus URL (e.g. http://localhost:9090). Auto-discovered if empty."
)
@click.option(
    "--grafana-url", default="", help="Grafana URL (e.g. http://localhost:3000). Auto-discovered if empty."
)
@click.option(
    "--grafana-api-key", default="", help="Grafana API key (or set GRAFANA_API_KEY env var)."
)
@click.option(
    "--allow-writes", is_flag=True, help="Allow the agent to apply manifests to the cluster."
)
@click.option(
    "--plan", "plan_file", default="", help="Path to a previously generated observability plan JSON to validate."
)
@click.option("--model", default="claude-sonnet-4-20250514", help="Anthropic model to use.")
@click.option("--api-key", default="", help="Anthropic API key (or set ANTHROPIC_API_KEY env var).")
@click.option("--max-turns", default=40, type=int, help="Maximum agent reasoning turns.")
@click.option(
    "--output", "-o", "output_dir", default="observability-output", help="Output directory."
)
@click.option("--verbose", "-v", is_flag=True, help="Show full agent reasoning.")
def validate(
    kubeconfig: str,
    kube_context: str,
    prometheus_url: str,
    grafana_url: str,
    grafana_api_key: str,
    allow_writes: bool,
    plan_file: str,
    model: str,
    api_key: str,
    max_turns: int,
    output_dir: str,
    verbose: bool,
) -> None:
    """Validate observability on a live Kubernetes cluster.

    Connects to a cluster, discovers Prometheus and Grafana, validates
    scrape targets, metrics, alert rules, and imports dashboards.
    Optionally applies fixes (with --allow-writes).

    Examples:

      k8s-obs validate

      k8s-obs validate --prometheus-url http://localhost:9090 --grafana-url http://localhost:3000

      k8s-obs validate --plan observability-output/plan.json --allow-writes
    """
    _configure_logging(verbose)

    settings = Settings(
        kubeconfig=kubeconfig,
        kube_context=kube_context,
        prometheus_url=prometheus_url,
        grafana_url=grafana_url,
        allow_writes=allow_writes,
        model=model,
        max_agent_turns=max_turns,
        output_dir=output_dir,
        verbose=verbose,
    )
    if api_key:
        settings.anthropic_api_key = api_key
    if grafana_api_key:
        settings.grafana_api_key = grafana_api_key

    try:
        settings.validate_api_key()
    except ValueError as exc:
        console.print(f"[red bold]Error:[/red bold] {exc}")
        sys.exit(1)

    # Load existing plan if specified
    plan = None
    if plan_file:
        try:
            from k8s_observability_agent.models import ObservabilityPlan

            plan_path = Path(plan_file)
            raw = json.loads(plan_path.read_text())
            plan = ObservabilityPlan(**raw)
            console.print(f"  Loaded plan from [green]{plan_file}[/green]")
        except Exception as exc:
            console.print(f"[yellow]Warning: Could not load plan file: {exc}[/yellow]")

    # ── Step 1: Validate ──────────────────────────────────────────────
    console.print(
        Panel(
            "Validating live cluster observability",
            style="bold cyan",
        )
    )
    if allow_writes:
        console.print("  [yellow]Write mode enabled — agent may apply manifests to the cluster.[/yellow]")
    else:
        console.print("  [dim]Read-only mode — use --allow-writes to let the agent fix issues.[/dim]")

    report = run_validate_agent(settings, plan=plan)

    # ── Step 2: Display report ────────────────────────────────────────
    console.print()
    console.print(Panel("Validation Report", style="bold green"))

    console.print(f"\n[bold]Cluster:[/bold] {report.cluster_summary}")

    # Checks table
    if report.checks:
        table = Table(title="Validation Checks", show_lines=True)
        table.add_column("Check", style="bold")
        table.add_column("Status")
        table.add_column("Details")
        table.add_column("Fix Applied")

        for check in report.checks:
            status_style = {
                "pass": "[green]PASS[/green]",
                "fail": "[red]FAIL[/red]",
                "warn": "[yellow]WARN[/yellow]",
                "skip": "[dim]SKIP[/dim]",
            }.get(check.status, check.status)

            fix_text = ""
            if check.fix_applied:
                fix_text = f"[green]Yes:[/green] {check.fix_description}"

            table.add_row(check.name, status_style, check.message[:100], fix_text)

        console.print(table)

    console.print(
        f"\n  Passed: [green]{report.passed}[/green]  "
        f"Failed: [red]{report.failed}[/red]  "
        f"Warnings: [yellow]{report.warnings}[/yellow]  "
        f"Fixes applied: [cyan]{report.fixes_applied}[/cyan]"
    )

    # Dashboards imported
    if report.dashboards_imported:
        console.print("\n[bold]Dashboards Imported:[/bold]")
        for d in report.dashboards_imported:
            icon = "[green]✓[/green]" if d.status == "imported" else "[red]✗[/red]"
            console.print(f"  {icon} {d.title} (ID: {d.dashboard_id})")

    # Remaining recommendations
    if report.recommendations:
        console.print("\n[bold]Remaining Action Items:[/bold]")
        for i, rec in enumerate(report.recommendations, 1):
            console.print(f"  {i}. {rec}")

    # ── Step 3: Save report ───────────────────────────────────────────
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "validation_report.json"
    report_path.write_text(json.dumps(report.model_dump(), indent=2))
    console.print(f"\n  Report saved to [green]{report_path}[/green]")


if __name__ == "__main__":
    main()
