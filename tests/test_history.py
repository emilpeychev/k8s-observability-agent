"""Tests for the SQLite-backed validation history store."""

from __future__ import annotations

from pathlib import Path

import pytest

from k8s_observability_agent.history import ValidationHistory
from k8s_observability_agent.models import (
    DashboardImportResult,
    RemediationStep,
    ValidationCheck,
    ValidationReport,
)


@pytest.fixture()
def history(tmp_path: Path) -> ValidationHistory:
    """Return a fresh ValidationHistory backed by a temp database."""
    return ValidationHistory(tmp_path / "test_history.db")


def _sample_report(**overrides) -> ValidationReport:
    """Build a minimal ValidationReport with sensible defaults."""
    defaults = dict(
        cluster_summary="kind-kind / Prometheus up / Grafana up",
        checks=[
            ValidationCheck(name="scrape-targets", status="pass", message="All healthy"),
            ValidationCheck(
                name="node-exporter-metrics",
                status="fail",
                message="node_cpu_seconds_total not found",
                fix_manifest="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: fix",
            ),
            ValidationCheck(name="alertmanager", status="warn", message="No receivers"),
        ],
        dashboards_imported=[
            DashboardImportResult(dashboard_id=1860, title="Node Exporter", status="imported"),
        ],
        recommendations=["Deploy node_exporter DaemonSet", "Configure Alertmanager receivers"],
        remediation_steps=[
            RemediationStep(
                title="Deploy node_exporter",
                description="Install node exporter DaemonSet",
                manifest="apiVersion: apps/v1\nkind: DaemonSet ...",
                priority="high",
            ),
        ],
        dashboards_to_import=[
            DashboardImportResult(dashboard_id=315, title="K8s Cluster", status="recommended"),
        ],
    )
    defaults.update(overrides)
    return ValidationReport(**defaults)


class TestValidationHistory:
    """Tests for ValidationHistory."""

    def test_save_and_retrieve(self, history: ValidationHistory) -> None:
        report = _sample_report()
        run_id = history.save_run("kind-kind", report)
        assert run_id >= 1

        last = history.last_run("kind-kind")
        assert last is not None
        assert last["cluster_summary"] == report.cluster_summary
        assert last["cluster_context"] == "kind-kind"

    def test_last_report_returns_model(self, history: ValidationHistory) -> None:
        report = _sample_report()
        history.save_run("ctx", report)

        restored = history.last_report("ctx")
        assert restored is not None
        assert restored.cluster_summary == report.cluster_summary
        assert len(restored.checks) == 3
        assert restored.checks[1].status == "fail"
        assert restored.checks[1].fix_manifest.startswith("apiVersion")
        assert len(restored.dashboards_imported) == 1
        assert restored.dashboards_imported[0].dashboard_id == 1860
        assert len(restored.remediation_steps) == 1
        assert restored.remediation_steps[0].title == "Deploy node_exporter"
        assert len(restored.dashboards_to_import) == 1
        assert restored.dashboards_to_import[0].dashboard_id == 315
        assert restored.recommendations == report.recommendations

    def test_no_history_returns_none(self, history: ValidationHistory) -> None:
        assert history.last_run("nonexistent") is None
        assert history.last_report("nonexistent") is None

    def test_run_count(self, history: ValidationHistory) -> None:
        assert history.run_count("ctx") == 0
        history.save_run("ctx", _sample_report())
        assert history.run_count("ctx") == 1
        history.save_run("ctx", _sample_report())
        assert history.run_count("ctx") == 2
        # Different context doesn't count
        history.save_run("other", _sample_report())
        assert history.run_count("ctx") == 2

    def test_all_runs_ordering(self, history: ValidationHistory) -> None:
        for i in range(5):
            history.save_run("ctx", _sample_report(cluster_summary=f"run-{i}"))

        runs = history.all_runs("ctx", limit=3)
        assert len(runs) == 3
        # Most recent first
        assert runs[0]["cluster_summary"] == "run-4"
        assert runs[2]["cluster_summary"] == "run-2"

    def test_prune_keeps_recent(self, history: ValidationHistory) -> None:
        for i in range(10):
            history.save_run("ctx", _sample_report(cluster_summary=f"run-{i}"))

        deleted = history.prune("ctx", keep=3)
        assert deleted == 7
        assert history.run_count("ctx") == 3

        runs = history.all_runs("ctx")
        summaries = [r["cluster_summary"] for r in runs]
        assert summaries == ["run-9", "run-8", "run-7"]

    def test_previous_run_summary_empty(self, history: ValidationHistory) -> None:
        assert history.previous_run_summary("no-ctx") == ""

    def test_previous_run_summary_content(self, history: ValidationHistory) -> None:
        history.save_run("ctx", _sample_report())
        summary = history.previous_run_summary("ctx")

        assert "Previous validation run" in summary
        assert "1 passed, 1 failed, 1 warnings" in summary
        assert "node-exporter-metrics" in summary
        assert "node_cpu_seconds_total" in summary
        assert "Deploy node_exporter DaemonSet" in summary
        assert "Deploy node_exporter" in summary
        assert "Re-check the previously failing items" in summary
        assert "ID 315" in summary

    def test_separate_cluster_contexts(self, history: ValidationHistory) -> None:
        history.save_run("cluster-a", _sample_report(cluster_summary="A"))
        history.save_run("cluster-b", _sample_report(cluster_summary="B"))

        a = history.last_report("cluster-a")
        b = history.last_report("cluster-b")
        assert a is not None and a.cluster_summary == "A"
        assert b is not None and b.cluster_summary == "B"

    def test_close_and_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "history.db"
        h1 = ValidationHistory(db)
        h1.save_run("ctx", _sample_report())
        h1.close()

        h2 = ValidationHistory(db)
        assert h2.run_count("ctx") == 1
        h2.close()

    def test_multiple_runs_returns_latest(self, history: ValidationHistory) -> None:
        history.save_run("ctx", _sample_report(cluster_summary="old"))
        history.save_run("ctx", _sample_report(cluster_summary="new"))

        report = history.last_report("ctx")
        assert report is not None
        assert report.cluster_summary == "new"
