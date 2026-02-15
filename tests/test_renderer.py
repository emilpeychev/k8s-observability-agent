"""Tests for agent.renderer."""

from pathlib import Path

from agent.models import (
    AlertRule,
    DashboardPanel,
    DashboardSpec,
    MetricRecommendation,
    ObservabilityPlan,
)
from agent.renderer import (
    render_grafana_dashboards,
    render_plan_summary,
    render_prometheus_rules,
    write_outputs,
)


def _sample_plan() -> ObservabilityPlan:
    return ObservabilityPlan(
        platform_summary="Test platform with 1 deployment",
        metrics=[
            MetricRecommendation(
                metric_name="http_requests_total",
                description="Total HTTP requests",
                query='rate(http_requests_total{job="web"}[5m])',
                resource="default/Deployment/web",
            ),
        ],
        alerts=[
            AlertRule(
                alert_name="HighErrorRate",
                severity="critical",
                expr='rate(http_errors_total[5m]) / rate(http_requests_total[5m]) > 0.05',
                for_duration="5m",
                summary="High error rate on web",
                description="Error rate exceeds 5%",
                resource="default/Deployment/web",
            ),
        ],
        dashboards=[
            DashboardSpec(
                title="Web Overview",
                description="Overview dashboard for web service",
                panels=[
                    DashboardPanel(
                        title="Request Rate",
                        panel_type="timeseries",
                        queries=['rate(http_requests_total{job="web"}[5m])'],
                        resource="default/Deployment/web",
                    ),
                    DashboardPanel(
                        title="Error Rate",
                        panel_type="timeseries",
                        queries=['rate(http_errors_total[5m]) / rate(http_requests_total[5m])'],
                    ),
                ],
                tags=["web", "k8s"],
            ),
        ],
        recommendations=["Add readiness probes to all containers"],
    )


class TestRenderPrometheusRules:
    def test_contains_alert(self) -> None:
        plan = _sample_plan()
        output = render_prometheus_rules(plan)
        assert "HighErrorRate" in output
        assert "critical" in output
        assert "5m" in output

    def test_valid_yaml_structure(self) -> None:
        import yaml

        plan = _sample_plan()
        output = render_prometheus_rules(plan)
        parsed = yaml.safe_load(output)
        assert "groups" in parsed
        assert len(parsed["groups"]) > 0


class TestRenderGrafanaDashboards:
    def test_produces_dashboard(self) -> None:
        import json

        plan = _sample_plan()
        results = render_grafana_dashboards(plan)
        assert len(results) == 1
        filename, content = results[0]
        assert "grafana-" in filename
        assert filename.endswith(".json")
        parsed = json.loads(content)
        assert parsed["title"] == "Web Overview"
        assert len(parsed["panels"]) == 2


class TestRenderPlanSummary:
    def test_markdown_contains_sections(self) -> None:
        plan = _sample_plan()
        output = render_plan_summary(plan)
        assert "## Platform Summary" in output
        assert "## Recommended Metrics" in output
        assert "## Alert Rules" in output
        assert "## Dashboards" in output
        assert "## Recommendations" in output


class TestWriteOutputs:
    def test_writes_files(self, tmp_path: Path) -> None:
        plan = _sample_plan()
        written = write_outputs(plan, tmp_path / "out")
        assert len(written) >= 3  # prometheus, grafana, summary
        for path_str in written:
            assert Path(path_str).exists()
