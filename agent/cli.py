"""CLI entry-point for the k8s-observability-agent."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from agent import __version__
from agent.analyzer import build_platform, platform_report
from agent.config import Settings
from agent.core import run_agent
from agent.renderer import write_outputs
from agent.scanner import scan_repository

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
@click.option("--github", "github_url", default="", help="Clone from a GitHub URL instead of a local path.")
@click.option("--branch", default="main", help="Git branch to checkout (used with --github).")
@click.option("--output", "-o", "output_dir", default="observability-output", help="Output directory.")
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

    console.print(f"  Found [green]{len(resources)}[/green] resources in {len(manifest_files)} manifest files.")

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
@click.option("--github", "github_url", default="", help="Clone from a GitHub URL instead of a local path.")
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


if __name__ == "__main__":
    main()
