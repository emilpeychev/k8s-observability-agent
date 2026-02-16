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
from k8s_observability_agent.renderer import render_validation_report_html, write_outputs
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
    "--grafana-password", default="admin", help="Grafana admin password (default: admin)."
)
@click.option(
    "--ca-cert", default="", help="Path to CA certificate for TLS verification (e.g. tls/ca.crt)."
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
@click.option(
    "--no-history", is_flag=True,
    help="Disable SQLite history — start fresh without re-checking previous results.",
)
def validate(
    kubeconfig: str,
    kube_context: str,
    prometheus_url: str,
    grafana_url: str,
    grafana_api_key: str,
    grafana_password: str,
    ca_cert: str,
    allow_writes: bool,
    plan_file: str,
    model: str,
    api_key: str,
    max_turns: int,
    output_dir: str,
    verbose: bool,
    no_history: bool,
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
        ca_cert=ca_cert,
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
    if grafana_password != "admin":
        settings.grafana_password = grafana_password

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

    # ── Set up validation history ──────────────────────────────────
    history = None
    if not no_history:
        from k8s_observability_agent.history import ValidationHistory

        history_dir = Path(output_dir).resolve()
        history_dir.mkdir(parents=True, exist_ok=True)
        history_db = history_dir / "validation_history.db"
        history = ValidationHistory(history_db)
        prev_count = history.run_count(kube_context or "default")
        if prev_count:
            console.print(
                f"  [cyan]History:[/cyan] {prev_count} previous run(s) found in "
                f"[dim]{history_db}[/dim]"
            )
        else:
            console.print(f"  [dim]History: first run (will be saved to {history_db})[/dim]")

    report = run_validate_agent(settings, plan=plan, history=history)

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

    # Dashboards to import
    if report.dashboards_to_import:
        console.print("\n[bold]Recommended Dashboards to Import:[/bold]")
        for d in report.dashboards_to_import:
            console.print(f"  • {d.title} (ID: {d.dashboard_id}) — https://grafana.com/grafana/dashboards/{d.dashboard_id}")

    # Remediation steps
    if report.remediation_steps:
        console.print(f"\n[bold]Remediation Steps ({len(report.remediation_steps)}):[/bold]")
        for i, step in enumerate(report.remediation_steps, 1):
            priority_style = {"high": "[red]HIGH[/red]", "medium": "[yellow]MED[/yellow]", "low": "[dim]LOW[/dim]"}.get(step.priority, step.priority)
            console.print(f"  {i}. {priority_style} {step.title}")

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

    # ── Step 4: HTML report deployed as in-cluster pod ────────────────
    html = render_validation_report_html(report, plan=plan)
    html_path = out_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    console.print(f"  HTML report saved to [green]{html_path}[/green]")

    if allow_writes:
        _deploy_report_to_cluster(html, kubeconfig, kube_context)
    else:
        console.print(
            "  [dim]Skipping in-cluster report deployment (read-only mode). "
            "Use --allow-writes to deploy the report pod.[/dim]"
        )

    # ── Close history ─────────────────────────────────────────────────
    if history is not None:
        history.close()


_REPORT_NAMESPACE = "observability-report"
_REPORT_APP_LABEL = "obs-report"


def _deploy_report_to_cluster(html: str, kubeconfig: str = "", context: str = "") -> None:
    """Deploy the HTML report as an nginx pod behind Istio on the cluster.

    Creates:
      1. Namespace  ``observability-report``
      2. ConfigMap  ``report-html`` (holds report.html)
      3. ConfigMap  ``nginx-conf`` (nginx server config to serve report.html)
      4. Deployment ``obs-report`` (nginx serving the ConfigMaps)
      5. Service    ``obs-report`` (ClusterIP → nginx:80)
      6. ReferenceGrant allowing the shared gateway to route to this namespace
      7. HTTPRoute  ``report`` for ``report.local`` via the shared gateway
      8. /etc/hosts entry  report.local → <gateway-ip>
    """
    import subprocess as _sp
    import textwrap

    base = ["kubectl"]
    if kubeconfig:
        base += ["--kubeconfig", kubeconfig]
    if context:
        base += ["--context", context]

    def _apply(manifest: str) -> bool:
        try:
            proc = _sp.run(
                base + ["apply", "-f", "-"],
                input=manifest,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                console.print(f"  [red]kubectl apply failed:[/red] {proc.stderr.strip()}")
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]kubectl apply error:[/red] {exc}")
            return False

    # 1. Namespace
    ns_manifest = textwrap.dedent(f"""\
        apiVersion: v1
        kind: Namespace
        metadata:
          name: {_REPORT_NAMESPACE}
          labels:
            istio-injection: enabled
    """)
    if _apply(ns_manifest):
        console.print(f"  Namespace [cyan]{_REPORT_NAMESPACE}[/cyan] ready")

    # 2. ConfigMap with HTML (delete + recreate to handle updates)
    try:
        _sp.run(
            base + ["delete", "configmap", "report-html",
                    "-n", _REPORT_NAMESPACE, "--ignore-not-found"],
            capture_output=True, text=True, timeout=10,
        )
        proc = _sp.run(
            base + ["create", "configmap", "report-html",
                    f"--from-literal=report.html={html}",
                    "-n", _REPORT_NAMESPACE],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            console.print("  ConfigMap [cyan]report-html[/cyan] created")
        else:
            console.print(f"  [red]ConfigMap creation failed:[/red] {proc.stderr.strip()}")
            return
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]ConfigMap error:[/red] {exc}")
        return

    # 3. Nginx config (serve report.html as default index)
    nginx_conf_manifest = textwrap.dedent(f"""\
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: nginx-conf
          namespace: {_REPORT_NAMESPACE}
        data:
          default.conf: |
            server {{
                listen 80;
                server_name _;
                root /usr/share/nginx/html;
                index report.html;
                location / {{
                    try_files $uri $uri/ /report.html;
                }}
            }}
    """)
    if _apply(nginx_conf_manifest):
        console.print("  ConfigMap [cyan]nginx-conf[/cyan] applied")

    # 4. Deployment (nginx serving the ConfigMap as /usr/share/nginx/html/)
    deploy_manifest = textwrap.dedent(f"""\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: {_REPORT_APP_LABEL}
          namespace: {_REPORT_NAMESPACE}
          labels:
            app: {_REPORT_APP_LABEL}
        spec:
          replicas: 1
          selector:
            matchLabels:
              app: {_REPORT_APP_LABEL}
          template:
            metadata:
              labels:
                app: {_REPORT_APP_LABEL}
            spec:
              containers:
              - name: nginx
                image: nginx:1-alpine
                ports:
                - containerPort: 80
                volumeMounts:
                - name: html
                  mountPath: /usr/share/nginx/html
                  readOnly: true
                - name: nginx-conf
                  mountPath: /etc/nginx/conf.d
                  readOnly: true
              volumes:
              - name: html
                configMap:
                  name: report-html
              - name: nginx-conf
                configMap:
                  name: nginx-conf
    """)
    if _apply(deploy_manifest):
        console.print(f"  Deployment [cyan]{_REPORT_APP_LABEL}[/cyan] applied")

    # 5. Service
    svc_manifest = textwrap.dedent(f"""\
        apiVersion: v1
        kind: Service
        metadata:
          name: {_REPORT_APP_LABEL}
          namespace: {_REPORT_NAMESPACE}
          labels:
            app: {_REPORT_APP_LABEL}
        spec:
          selector:
            app: {_REPORT_APP_LABEL}
          ports:
          - port: 80
            targetPort: 80
            name: http
    """)
    if _apply(svc_manifest):
        console.print(f"  Service [cyan]{_REPORT_APP_LABEL}[/cyan] applied")

    # 5. Discover the shared K8s Gateway API gateway, then create
    #    a ReferenceGrant + HTTPRoute for report.local
    gw_ns, gw_name = _find_k8s_gateway(base)
    if not gw_name:
        console.print("  [yellow]No K8s Gateway API gateway found — skipping HTTPRoute.[/yellow]")
    else:
        # ReferenceGrant: allow the gateway namespace to reference our Service
        ref_grant = textwrap.dedent(f"""\
            apiVersion: gateway.networking.k8s.io/v1beta1
            kind: ReferenceGrant
            metadata:
              name: allow-gateway
              namespace: {_REPORT_NAMESPACE}
            spec:
              from:
              - group: gateway.networking.k8s.io
                kind: HTTPRoute
                namespace: {gw_ns}
              to:
              - group: ""
                kind: Service
                name: {_REPORT_APP_LABEL}
        """)
        if _apply(ref_grant):
            console.print("  ReferenceGrant [cyan]allow-gateway[/cyan] applied")

        # HTTPRoute: route report.local → obs-report service (HTTPS)
        httproute = textwrap.dedent(f"""\
            apiVersion: gateway.networking.k8s.io/v1
            kind: HTTPRoute
            metadata:
              name: report
              namespace: {_REPORT_NAMESPACE}
            spec:
              parentRefs:
              - name: {gw_name}
                namespace: {gw_ns}
                sectionName: https
              hostnames:
              - "report.local"
              rules:
              - backendRefs:
                - name: {_REPORT_APP_LABEL}
                  port: 80
        """)
        if _apply(httproute):
            console.print("  HTTPRoute [cyan]report.local[/cyan] (HTTPS) applied")

    # 6. /etc/hosts → gateway IP
    gateway_ip = _get_gateway_ip(base, gw_ns, gw_name)
    if gateway_ip:
        _ensure_hosts_entry(gateway_ip, "report.local")
        console.print(
            f"\n  [bold green]Report is live at[/bold green] "
            f"[link=https://report.local]https://report.local[/link]"
        )
    else:
        console.print(
            "\n  [yellow]Could not detect Istio gateway IP.[/yellow]\n"
            "  Once you know the IP, run:\n"
            "    echo '<GATEWAY_IP>  report.local' | sudo tee -a /etc/hosts"
        )


def _find_k8s_gateway(base_cmd: list[str]) -> tuple[str, str]:
    """Find the shared K8s Gateway API gateway in the cluster.

    Returns (namespace, name) or ("", "") if none found.
    """
    import subprocess as _sp

    try:
        result = _sp.run(
            base_cmd + [
                "get", "gateways.gateway.networking.k8s.io",
                "--all-namespaces", "-o", "json",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            import json as _json

            gws = _json.loads(result.stdout)
            for item in gws.get("items", []):
                ns = item["metadata"]["namespace"]
                name = item["metadata"]["name"]
                # Prefer a gateway with "Programmed" = True
                conditions = item.get("status", {}).get("conditions", [])
                for c in conditions:
                    if c.get("type") == "Programmed" and c.get("status") == "True":
                        console.print(f"  Found gateway [cyan]{ns}/{name}[/cyan]")
                        return ns, name
                # Fallback: return first gateway found
                console.print(f"  Found gateway [cyan]{ns}/{name}[/cyan]")
                return ns, name
    except Exception:  # noqa: BLE001
        pass

    return "", ""


def _get_gateway_ip(base_cmd: list[str], gw_ns: str, gw_name: str) -> str:
    """Get the external IP of a K8s Gateway API gateway."""
    import subprocess as _sp

    if not gw_name:
        return ""

    try:
        result = _sp.run(
            base_cmd + [
                "get", "gateways.gateway.networking.k8s.io", gw_name,
                "-n", gw_ns,
                "-o", "jsonpath={.status.addresses[0].value}",
            ],
            capture_output=True, text=True, timeout=10,
        )
        ip = result.stdout.strip()
        if ip and result.returncode == 0:
            console.print(f"  Gateway IP: [cyan]{ip}[/cyan]")
            return ip
    except Exception:  # noqa: BLE001
        pass

    # Fallback: try the associated LoadBalancer service
    return _get_istio_gateway_ip(base_cmd)


def _get_istio_gateway_ip(base_cmd: list[str]) -> str:
    """Fallback: discover the external IP of the Istio ingress gateway LoadBalancer."""
    import subprocess as _sp

    candidates = [
        ("istio-system", "istio-ingressgateway"),
        ("istio-ingress", "istio-ingressgateway"),
        ("istio-system", "istio-ingress"),
    ]

    for ns, svc in candidates:
        try:
            result = _sp.run(
                base_cmd + [
                    "get", "svc", svc,
                    "-n", ns,
                    "-o", "jsonpath={.status.loadBalancer.ingress[0].ip}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ip = result.stdout.strip()
            if ip and result.returncode == 0:
                console.print(f"  Detected Istio gateway IP: [cyan]{ip}[/cyan] ({ns}/{svc})")
                return ip
        except Exception:  # noqa: BLE001
            continue

    # Fallback: any LoadBalancer in istio-* namespaces
    try:
        result = _sp.run(
            base_cmd + ["get", "svc", "--all-namespaces", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            import json as _json

            svcs = _json.loads(result.stdout)
            for item in svcs.get("items", []):
                ns = item.get("metadata", {}).get("namespace", "")
                if "istio" not in ns:
                    continue
                ingress_list = (
                    item.get("status", {})
                    .get("loadBalancer", {})
                    .get("ingress", [])
                )
                for entry in ingress_list:
                    ip = entry.get("ip", "")
                    if ip:
                        svc_name = item["metadata"]["name"]
                        console.print(f"  Detected Istio gateway IP: [cyan]{ip}[/cyan] ({ns}/{svc_name})")
                        return ip
    except Exception:  # noqa: BLE001
        pass

    return ""


def _ensure_hosts_entry(ip: str, hostname: str) -> None:
    """Add *hostname* → *ip* to /etc/hosts if not already present."""
    hosts = Path("/etc/hosts")
    try:
        text = hosts.read_text()
    except PermissionError:
        console.print(f"  [yellow]Cannot read /etc/hosts — skipping {hostname} entry.[/yellow]")
        return

    for line in text.splitlines():
        stripped = line.split("#")[0].strip()
        parts = stripped.split()
        if len(parts) >= 2 and hostname in parts[1:]:
            if parts[0] == ip:
                console.print(f"  [dim]/etc/hosts already has {hostname} → {ip}[/dim]")
                return
            # Wrong IP — remove stale entry
            console.print(f"  [yellow]Removing stale /etc/hosts entry: {parts[0]} → {hostname}[/yellow]")
            try:
                import subprocess

                subprocess.run(  # noqa: S603, S607
                    ["sudo", "sed", "-i", f"/{hostname}/d", "/etc/hosts"],
                    check=True,
                    timeout=5,
                )
            except Exception:  # noqa: BLE001
                pass
            break

    entry = f"{ip}  {hostname}\n"
    try:
        import subprocess

        subprocess.run(  # noqa: S603, S607
            ["sudo", "tee", "-a", "/etc/hosts"],
            input=entry.encode(),
            stdout=subprocess.DEVNULL,
            check=True,
        )
        console.print(f"  Added [cyan]{hostname}[/cyan] → [cyan]{ip}[/cyan] to /etc/hosts")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]Could not update /etc/hosts: {exc}[/yellow]")
        console.print(f"  Run manually: echo '{ip}  {hostname}' | sudo tee -a /etc/hosts")


if __name__ == "__main__":
    main()
