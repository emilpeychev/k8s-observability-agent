"""Tests for agent.models."""

from k8s_observability_agent.models import (
    AlertRule,
    DashboardPanel,
    DashboardSpec,
    GrafanaDashboardRecommendation,
    K8sResource,
    MetricRecommendation,
    ObservabilityPlan,
    Platform,
)


class TestK8sResource:
    def test_is_workload(self) -> None:
        deploy = K8sResource(kind="Deployment", name="app")
        svc = K8sResource(kind="Service", name="svc")
        assert deploy.is_workload
        assert not svc.is_workload

    def test_qualified_name(self) -> None:
        r = K8sResource(kind="Deployment", name="web", namespace="prod")
        assert r.qualified_name == "prod/Deployment/web"

    def test_default_namespace(self) -> None:
        r = K8sResource(kind="ConfigMap", name="cfg")
        assert r.namespace == "default"


class TestPlatform:
    def test_summary_counts(self) -> None:
        platform = Platform(
            resources=[
                K8sResource(kind="Deployment", name="a"),
                K8sResource(kind="Deployment", name="b"),
                K8sResource(kind="Service", name="s"),
            ]
        )
        summary = platform.summary()
        assert summary == {"Deployment": 2, "Service": 1}

    def test_workloads_filter(self) -> None:
        platform = Platform(
            resources=[
                K8sResource(kind="Deployment", name="a"),
                K8sResource(kind="Service", name="s"),
                K8sResource(kind="StatefulSet", name="db"),
            ]
        )
        wls = platform.workloads
        assert len(wls) == 2
        assert {w.name for w in wls} == {"a", "db"}


class TestObservabilityPlan:
    def test_empty_plan(self) -> None:
        plan = ObservabilityPlan()
        assert plan.metrics == []
        assert plan.alerts == []
        assert plan.dashboards == []
        assert plan.dashboard_recommendations == []
        assert plan.recommendations == []

    def test_plan_with_data(self) -> None:
        plan = ObservabilityPlan(
            platform_summary="test",
            metrics=[MetricRecommendation(metric_name="up", query="up", resource="r")],
            alerts=[AlertRule(alert_name="HighCPU", expr="rate(cpu[5m]) > 0.9")],
            dashboards=[
                DashboardSpec(
                    title="Overview",
                    panels=[DashboardPanel(title="CPU", queries=["rate(cpu[5m])"])],
                )
            ],
            recommendations=["Add probes"],
        )
        assert len(plan.metrics) == 1
        assert len(plan.alerts) == 1
        assert len(plan.dashboards) == 1
        assert plan.dashboards[0].panels[0].title == "CPU"

    def test_alert_rule_nodata_state_default(self) -> None:
        alert = AlertRule(alert_name="Test", expr="up == 0")
        assert alert.nodata_state == "ok"

    def test_alert_rule_nodata_state_alerting(self) -> None:
        alert = AlertRule(alert_name="Test", expr="up == 0", nodata_state="alerting")
        assert alert.nodata_state == "alerting"

    def test_dashboard_recommendations(self) -> None:
        rec = GrafanaDashboardRecommendation(
            dashboard_id=9628,
            title="PostgreSQL Database",
            description="PostgreSQL monitoring",
            url="https://grafana.com/grafana/dashboards/9628/",
            resource="default/StatefulSet/db",
            archetype="database",
        )
        plan = ObservabilityPlan(
            platform_summary="test",
            dashboard_recommendations=[rec],
        )
        assert len(plan.dashboard_recommendations) == 1
        assert plan.dashboard_recommendations[0].dashboard_id == 9628
        assert plan.dashboard_recommendations[0].archetype == "database"
