"""Render observability plan outputs using Jinja2 templates."""

from __future__ import annotations

import json
import logging
import re
from importlib.resources import files as importlib_files
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from k8s_observability_agent.models import ObservabilityPlan

logger = logging.getLogger(__name__)

# Resolve the templates directory via importlib.resources so it works in
# both editable installs and built wheels / sdists.
_TEMPLATES_REF = importlib_files("k8s_observability_agent") / "templates"


def _get_jinja_env() -> Environment:
    # importlib.resources may return a Traversable that isn't on the real
    # filesystem (e.g. inside a zip).  Use as_posix() on the resolved path.
    templates_dir = str(_TEMPLATES_REF)
    return Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(default=False),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def render_prometheus_rules(plan: ObservabilityPlan) -> str:
    """Render Prometheus alerting rules YAML from the plan."""
    env = _get_jinja_env()
    template = env.get_template("prometheus_rules.yml.j2")
    return template.render(plan=plan)


def render_grafana_dashboards(plan: ObservabilityPlan) -> list[tuple[str, str]]:
    """Render Grafana dashboard JSON files.

    Returns a list of (filename, json_content) tuples.
    """
    env = _get_jinja_env()
    template = env.get_template("grafana_dashboard.json.j2")
    results: list[tuple[str, str]] = []
    for dashboard in plan.dashboards:
        raw = template.render(dashboard=dashboard)
        # Validate / pretty-print the JSON
        try:
            parsed = json.loads(raw)
            content = json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            content = raw
        slug = re.sub(r"[^\w\s-]", "", dashboard.title.lower()).strip().replace(" ", "-")[:40]
        filename = f"grafana-{slug}.json"
        results.append((filename, content))
    return results


def render_plan_summary(plan: ObservabilityPlan) -> str:
    """Render a Markdown summary of the plan."""
    env = _get_jinja_env()
    template = env.get_template("plan_summary.md.j2")
    return template.render(plan=plan)


def write_outputs(plan: ObservabilityPlan, output_dir: Path) -> list[str]:
    """Write all rendered outputs to *output_dir* and return the list of written file paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    # Prometheus rules
    if plan.alerts:
        prom_path = output_dir / "prometheus-rules.yml"
        prom_path.write_text(render_prometheus_rules(plan), encoding="utf-8")
        written.append(str(prom_path))
        logger.info("Wrote %s", prom_path)

    # Grafana dashboards
    for filename, content in render_grafana_dashboards(plan):
        dash_path = output_dir / filename
        dash_path.write_text(content, encoding="utf-8")
        written.append(str(dash_path))
        logger.info("Wrote %s", dash_path)

    # Summary
    summary_path = output_dir / "observability-plan.md"
    summary_path.write_text(render_plan_summary(plan), encoding="utf-8")
    written.append(str(summary_path))
    logger.info("Wrote %s", summary_path)

    return written
