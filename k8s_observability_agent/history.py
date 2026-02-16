"""SQLite-backed validation history store.

Records each validation run so the agent can skip re-discovering known issues
and focus on what has changed since the last run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from k8s_observability_agent.models import (
    DashboardImportResult,
    RemediationStep,
    ValidationCheck,
    ValidationReport,
)

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_DDL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_context TEXT    NOT NULL,
    run_at          TEXT    NOT NULL,           -- ISO-8601 UTC
    cluster_summary TEXT    NOT NULL DEFAULT '',
    checks_json     TEXT    NOT NULL DEFAULT '[]',
    dashboards_json TEXT    NOT NULL DEFAULT '[]',
    recommendations_json TEXT NOT NULL DEFAULT '[]',
    remediation_json     TEXT NOT NULL DEFAULT '[]',
    dashboards_to_import_json TEXT NOT NULL DEFAULT '[]',
    plan_hash       TEXT    NOT NULL DEFAULT '' -- sha256 of the plan JSON (optional)
);

CREATE INDEX IF NOT EXISTS idx_runs_cluster ON validation_runs(cluster_context);
CREATE INDEX IF NOT EXISTS idx_runs_time    ON validation_runs(run_at DESC);
"""


class ValidationHistory:
    """Persistent validation history backed by a SQLite database.

    Parameters
    ----------
    db_path : str | Path
        Path to the SQLite database file.  Created automatically if missing.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    # ── Schema management ──────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(_DDL)
        # Check / set version
        row = cur.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            cur.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
        self._conn.commit()

    # ── Write ──────────────────────────────────────────────────────────

    def save_run(
        self,
        cluster_context: str,
        report: ValidationReport,
        plan_hash: str = "",
    ) -> int:
        """Persist a finished validation run. Returns the new run id."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO validation_runs
                (cluster_context, run_at, cluster_summary,
                 checks_json, dashboards_json, recommendations_json,
                 remediation_json, dashboards_to_import_json, plan_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cluster_context,
                datetime.now(timezone.utc).isoformat(),
                report.cluster_summary,
                json.dumps([c.model_dump() for c in report.checks]),
                json.dumps([d.model_dump() for d in report.dashboards_imported]),
                json.dumps(report.recommendations),
                json.dumps([s.model_dump() for s in report.remediation_steps]),
                json.dumps([d.model_dump() for d in report.dashboards_to_import]),
                plan_hash,
            ),
        )
        self._conn.commit()
        run_id = cur.lastrowid
        logger.info("Saved validation run %d for context %r", run_id, cluster_context)
        return run_id  # type: ignore[return-value]

    # ── Read ───────────────────────────────────────────────────────────

    def last_run(self, cluster_context: str) -> dict[str, Any] | None:
        """Return the most recent run for a cluster context, or *None*."""
        row = self._conn.execute(
            """
            SELECT * FROM validation_runs
            WHERE cluster_context = ?
            ORDER BY run_at DESC
            LIMIT 1
            """,
            (cluster_context,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def last_report(self, cluster_context: str) -> ValidationReport | None:
        """Return the most recent *ValidationReport* for a context, or *None*."""
        raw = self.last_run(cluster_context)
        if raw is None:
            return None
        return self._dict_to_report(raw)

    def all_runs(self, cluster_context: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return the *limit* most recent runs for a cluster context."""
        rows = self._conn.execute(
            """
            SELECT * FROM validation_runs
            WHERE cluster_context = ?
            ORDER BY run_at DESC
            LIMIT ?
            """,
            (cluster_context, limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def run_count(self, cluster_context: str) -> int:
        """How many validation runs exist for this context."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM validation_runs WHERE cluster_context = ?",
            (cluster_context,),
        ).fetchone()
        return row[0] if row else 0

    # ── Summaries for the agent ────────────────────────────────────────

    def previous_run_summary(self, cluster_context: str) -> str:
        """Build a concise text summary of the last run for injection into the agent prompt.

        Returns an empty string if there is no prior run.
        """
        raw = self.last_run(cluster_context)
        if raw is None:
            return ""

        parts: list[str] = []
        parts.append(f"=== Previous validation run ({raw['run_at']}) ===")
        parts.append(f"Cluster summary: {raw['cluster_summary']}")
        parts.append("")

        checks: list[dict] = json.loads(raw["checks_json"])
        if checks:
            passed = sum(1 for c in checks if c.get("status") == "pass")
            failed = sum(1 for c in checks if c.get("status") == "fail")
            warned = sum(1 for c in checks if c.get("status") == "warn")
            parts.append(f"Results: {passed} passed, {failed} failed, {warned} warnings")

            failing = [c for c in checks if c.get("status") in ("fail", "warn")]
            if failing:
                parts.append("")
                parts.append("Issues found last time:")
                for c in failing:
                    status = c.get("status", "?").upper()
                    parts.append(f"  [{status}] {c.get('name', '?')}: {c.get('message', '')}")
                    if c.get("fix_manifest"):
                        parts.append(f"        Suggested manifest was provided")
            parts.append("")

        recs: list[str] = json.loads(raw["recommendations_json"])
        if recs:
            parts.append("Previous recommendations:")
            for r in recs:
                parts.append(f"  - {r}")
            parts.append("")

        remediation: list[dict] = json.loads(raw["remediation_json"])
        if remediation:
            parts.append(f"Previous remediation steps ({len(remediation)}):")
            for s in remediation:
                parts.append(f"  - [{s.get('priority', '?').upper()}] {s.get('title', '?')}")
            parts.append("")

        dashboards_to_import: list[dict] = json.loads(raw["dashboards_to_import_json"])
        if dashboards_to_import:
            parts.append("Dashboards previously recommended:")
            for d in dashboards_to_import:
                parts.append(f"  - ID {d.get('dashboard_id', '?')}: {d.get('title', '?')}")
            parts.append("")

        parts.append(
            "Re-check the previously failing items first. If they are now fixed, mark them "
            "as PASS. If they still fail, keep them as FAIL and update the remediation. "
            "Also check for any NEW issues that were not in the previous run."
        )
        return "\n".join(parts)

    # ── Housekeeping ───────────────────────────────────────────────────

    def prune(self, cluster_context: str, keep: int = 10) -> int:
        """Delete old runs, keeping the *keep* most recent. Returns rows deleted."""
        cur = self._conn.cursor()
        cur.execute(
            """
            DELETE FROM validation_runs
            WHERE cluster_context = ? AND id NOT IN (
                SELECT id FROM validation_runs
                WHERE cluster_context = ?
                ORDER BY run_at DESC
                LIMIT ?
            )
            """,
            (cluster_context, cluster_context, keep),
        )
        self._conn.commit()
        deleted = cur.rowcount
        if deleted:
            logger.info("Pruned %d old runs for context %r", deleted, cluster_context)
        return deleted

    def close(self) -> None:
        self._conn.close()

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _dict_to_report(raw: dict[str, Any]) -> ValidationReport:
        checks = [ValidationCheck(**c) for c in json.loads(raw["checks_json"])]
        dashboards = [DashboardImportResult(**d) for d in json.loads(raw["dashboards_json"])]
        remediation = [RemediationStep(**s) for s in json.loads(raw["remediation_json"])]
        dashboards_to_import = [
            DashboardImportResult(**d) for d in json.loads(raw["dashboards_to_import_json"])
        ]
        return ValidationReport(
            cluster_summary=raw["cluster_summary"],
            checks=checks,
            dashboards_imported=dashboards,
            recommendations=json.loads(raw["recommendations_json"]),
            remediation_steps=remediation,
            dashboards_to_import=dashboards_to_import,
        )
