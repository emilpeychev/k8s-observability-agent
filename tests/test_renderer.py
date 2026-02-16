"""Tests for agent.renderer."""

from pathlib import Path

from k8s_observability_agent.models import (
    AlertRule,
    DashboardPanel,
    DashboardSpec,
    GrafanaDashboardRecommendation,
    MetricRecommendation,
    ObservabilityPlan,
)
from k8s_observability_agent.renderer import (
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
                expr="rate(http_errors_total[5m]) / rate(http_requests_total[5m]) > 0.05",
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
                        queries=["rate(http_errors_total[5m]) / rate(http_requests_total[5m])"],
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

    def test_dashboard_recommendations_section(self) -> None:
        plan = _sample_plan()
        plan.dashboard_recommendations = [
            GrafanaDashboardRecommendation(
                dashboard_id=9628,
                title="PostgreSQL Database",
                url="https://grafana.com/grafana/dashboards/9628/",
                archetype="database",
            ),
        ]
        output = render_plan_summary(plan)
        assert "Recommended Grafana Dashboards" in output
        assert "9628" in output
        assert "PostgreSQL Database" in output
        assert "grafana.com" in output


class TestRenderPrometheusRulesNodata:
    def test_nodata_alerting_generates_absent_rule(self) -> None:
        import yaml

        plan = ObservabilityPlan(
            platform_summary="test",
            alerts=[
                AlertRule(
                    alert_name="ReplicationLagHigh",
                    severity="critical",
                    expr="pg_replication_lag_bytes > 100",
                    nodata_state="alerting",
                ),
            ],
        )
        output = render_prometheus_rules(plan)
        parsed = yaml.safe_load(output)
        group_names = [g["name"] for g in parsed["groups"]]
        assert "k8s_observability_absent_metrics" in group_names
        absent_group = [g for g in parsed["groups"] if g["name"] == "k8s_observability_absent_metrics"][0]
        assert any("Absent" in r["alert"] for r in absent_group["rules"])

    def test_nodata_ok_no_absent_rule(self) -> None:
        import yaml

        plan = ObservabilityPlan(
            platform_summary="test",
            alerts=[
                AlertRule(
                    alert_name="HighCPU",
                    severity="warning",
                    expr="rate(cpu[5m]) > 0.9",
                    nodata_state="ok",
                ),
            ],
        )
        output = render_prometheus_rules(plan)
        parsed = yaml.safe_load(output)
        # Should only have the main group, no absent metrics group
        group_names = [g["name"] for g in parsed["groups"]]
        assert "k8s_observability_absent_metrics" not in group_names

    def test_nodata_label_present(self) -> None:
        plan = ObservabilityPlan(
            platform_summary="test",
            alerts=[
                AlertRule(
                    alert_name="CritAlert",
                    severity="critical",
                    expr="up == 0",
                    nodata_state="alerting",
                ),
            ],
        )
        output = render_prometheus_rules(plan)
        assert 'nodata_state: "alerting"' in output


class TestWriteOutputs:
    def test_writes_files(self, tmp_path: Path) -> None:
        plan = _sample_plan()
        written = write_outputs(plan, tmp_path / "out")
        assert len(written) >= 3  # prometheus, grafana, summary
        for path_str in written:
            assert Path(path_str).exists()
