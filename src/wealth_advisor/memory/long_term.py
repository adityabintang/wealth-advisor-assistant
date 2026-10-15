"""Long-term memory (SQLite): run summaries, anomaly history, advisor decisions, CRM cache (PRD §13)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    total_assets REAL,
    confidence REAL,
    degraded INTEGER,
    review_status TEXT,
    anomaly_fingerprints TEXT,
    summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_client ON runs(client_id, generated_at);
CREATE TABLE IF NOT EXISTS anomaly_history (
    client_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    anomaly_type TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (client_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    reviewer TEXT,
    mode TEXT,
    status TEXT,
    note TEXT,
    changes TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS crm_cache (
    client_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

_ACK_STATUSES = {"approved", "edited", "not_required"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LongTermMemory:
    """Thread-safe SQLite store. Use ``":memory:"`` for an ephemeral store (``--no-memory``)."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ CRM cache
    def save_crm(self, client_id: str, payload: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO crm_cache(client_id, payload, fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(client_id) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
                (client_id, json.dumps(payload, default=str), _now()),
            )

    def get_crm(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT payload, fetched_at FROM crm_cache WHERE client_id=?", (client_id,)).fetchone()
        return {"context": json.loads(row["payload"]), "fetched_at": row["fetched_at"]} if row else None

    # ------------------------------------------------------------------ runs + anomalies
    def save_run(self, output: dict[str, Any]) -> None:
        client_id = output["client_id"]
        status = output["review"]["status"]
        anomalies = output.get("anomalies", [])
        fps = [a["fingerprint"] for a in anomalies]
        acknowledged = 1 if status in _ACK_STATUSES else 0
        now = output["generated_at"]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    output["run_id"],
                    client_id,
                    now,
                    (output.get("metrics") or {}).get("total_assets"),
                    output.get("confidence"),
                    int(bool(output.get("degraded"))),
                    status,
                    json.dumps(fps),
                    output.get("summary"),
                ),
            )
            for a in anomalies:
                self._conn.execute(
                    "INSERT INTO anomaly_history(client_id, fingerprint, anomaly_type, first_seen, last_seen, occurrences, acknowledged) "
                    "VALUES(?,?,?,?,?,1,?) ON CONFLICT(client_id, fingerprint) DO UPDATE SET "
                    "last_seen=excluded.last_seen, occurrences=occurrences+1, acknowledged=MAX(acknowledged, excluded.acknowledged)",
                    (client_id, a["fingerprint"], a["type"], now, now, acknowledged),
                )

    def record_decision(self, run_id: str, client_id: str, review: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO decisions(run_id, client_id, reviewer, mode, status, note, changes, decided_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    client_id,
                    review.get("reviewer"),
                    review.get("mode"),
                    review.get("status"),
                    review.get("reviewer_note"),
                    json.dumps(review.get("changes", [])),
                    review.get("decided_at") or _now(),
                ),
            )

    def get_client_history(self, client_id: str, limit: int = 5) -> dict[str, Any]:
        with self._lock:
            runs = self._conn.execute(
                "SELECT * FROM runs WHERE client_id=? ORDER BY generated_at DESC LIMIT ?", (client_id, limit)
            ).fetchall()
            anomalies = self._conn.execute("SELECT * FROM anomaly_history WHERE client_id=?", (client_id,)).fetchall()
            decisions = self._conn.execute(
                "SELECT * FROM decisions WHERE client_id=? ORDER BY id DESC LIMIT ?", (client_id, limit)
            ).fetchall()
        return {
            "runs": [dict(r) | {"anomaly_fingerprints": json.loads(r["anomaly_fingerprints"] or "[]")} for r in runs],
            "anomalies": {r["fingerprint"]: dict(r) for r in anomalies},
            "decisions": [dict(d) | {"changes": json.loads(d["changes"] or "[]")} for d in decisions],
        }

    def reset(self) -> None:
        with self._lock, self._conn:
            for table in ("runs", "anomaly_history", "decisions", "crm_cache"):
                self._conn.execute(f"DELETE FROM {table}")
