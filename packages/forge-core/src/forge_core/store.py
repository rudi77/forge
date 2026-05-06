"""DuckDB-Sink für Events.

Eine append-only `events`-Tabelle plus indizierte Views auf häufig gefragte
Aggregationen (Spec Teil 4.3). DuckDB schluckt 100M+ Events ohne Server, hat
First-Class-JSON-Support, exportiert nach Parquet für spätere Aggregation.

Lokale Datei: `.forge/events.duckdb` per Default. Geöffnet wird die DB on-demand;
mehrere Prozesse können nicht gleichzeitig schreiben (DuckDB-Limitation), aber für
v1 läuft nur ein forge-Prozess pro Repo.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any

import duckdb

from forge_core.events import Event, EventKind

SCHEMA_VERSION = 1


_CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    event_id               VARCHAR PRIMARY KEY,
    run_id                 VARCHAR NOT NULL,
    generation_id          VARCHAR,
    parent_event_id        VARCHAR,

    project                VARCHAR NOT NULL,
    project_fingerprint    VARCHAR NOT NULL,
    factory_version        VARCHAR NOT NULL,
    spec_version           VARCHAR NOT NULL,

    ts                     TIMESTAMPTZ NOT NULL,
    duration_ms            BIGINT,

    kind                   VARCHAR NOT NULL,
    payload_schema_version VARCHAR NOT NULL,
    payload                JSON NOT NULL,
    artifacts              JSON NOT NULL DEFAULT '{}',

    tokens_in              BIGINT NOT NULL DEFAULT 0,
    tokens_out             BIGINT NOT NULL DEFAULT 0,
    cost_usd               DECIMAL(18, 6) NOT NULL DEFAULT 0,
    model                  VARCHAR,
    model_version          VARCHAR,

    success                BOOLEAN,
    error_class            VARCHAR,
    error_msg              VARCHAR
);
"""


_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS forge_meta (
    key VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);
"""


_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);",
    "CREATE INDEX IF NOT EXISTS idx_events_project ON events(project);",
]


_VIEW_RUNS_WITH_OUTCOMES = """
CREATE OR REPLACE VIEW runs_with_outcomes AS
SELECT
    s.run_id,
    s.project,
    s.ts                               AS started_at,
    f.ts                               AS finished_at,
    EXTRACT(EPOCH FROM (f.ts - s.ts))  AS duration_s,
    json_extract_string(s.payload, '$.trigger')   AS trigger,
    json_extract_string(s.payload, '$.strategy')  AS strategy,
    json_extract_string(s.payload, '$.focus')     AS focus,
    json_extract_string(f.payload, '$.decision')  AS decision,
    CAST(json_extract(f.payload, '$.final_score') AS DOUBLE)   AS final_score,
    CAST(json_extract(f.payload, '$.score_delta') AS DOUBLE)   AS score_delta,
    CAST(json_extract(f.payload, '$.total_cost_usd') AS DOUBLE) AS total_cost_usd,
    CAST(json_extract(f.payload, '$.pr_number') AS BIGINT)     AS pr_number
FROM events s
LEFT JOIN events f
    ON f.run_id = s.run_id AND f.kind = 'RunFinished'
WHERE s.kind = 'RunStarted';
"""


_VIEW_COST_PER_FOCUS = """
CREATE OR REPLACE VIEW cost_per_focus AS
SELECT
    focus,
    project,
    COUNT(*)                         AS run_count,
    COUNT(*) FILTER (WHERE decision = 'pr_created') AS pr_count,
    SUM(total_cost_usd)              AS total_cost_usd,
    AVG(total_cost_usd)              AS mean_cost_usd
FROM runs_with_outcomes
GROUP BY focus, project;
"""


_VIEW_PR_MERGE_RATE = """
CREATE OR REPLACE VIEW pr_merge_rate_by_focus AS
WITH created AS (
    SELECT
        run_id,
        CAST(json_extract(payload, '$.pr_number') AS BIGINT) AS pr_number
    FROM events WHERE kind = 'PRCreated'
),
merged AS (
    SELECT CAST(json_extract(payload, '$.pr_number') AS BIGINT) AS pr_number
    FROM events WHERE kind = 'PRMerged'
)
SELECT
    r.focus,
    r.project,
    COUNT(c.pr_number)                              AS prs_created,
    COUNT(m.pr_number)                              AS prs_merged,
    CAST(COUNT(m.pr_number) AS DOUBLE) /
        NULLIF(COUNT(c.pr_number), 0)               AS merge_rate
FROM runs_with_outcomes r
LEFT JOIN created c ON c.run_id = r.run_id
LEFT JOIN merged  m ON m.pr_number = c.pr_number
GROUP BY r.focus, r.project;
"""


_VIEW_FAILURE_MODES = """
CREATE OR REPLACE VIEW top_failure_modes AS
SELECT
    project,
    kind,
    error_class,
    COUNT(*)            AS occurrences,
    MAX(ts)             AS last_seen
FROM events
WHERE success = FALSE OR error_class IS NOT NULL
GROUP BY project, kind, error_class
ORDER BY occurrences DESC;
"""


_VIEWS = [
    _VIEW_RUNS_WITH_OUTCOMES,
    _VIEW_COST_PER_FOCUS,
    _VIEW_PR_MERGE_RATE,
    _VIEW_FAILURE_MODES,
]


class EventStore:
    """DuckDB-Sink + Reader für Events.

    Verwendung::

        store = EventStore(Path(".forge/events.duckdb"))
        store.append(event)
        for evt in store.events_for_run("01HZX..."):
            ...
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: duckdb.DuckDBPyConnection = duckdb.connect(str(self.db_path))
        self._init_schema()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _init_schema(self) -> None:
        conn = self._conn
        conn.execute(_CREATE_EVENTS_TABLE)
        conn.execute(_CREATE_META_TABLE)
        for stmt in _INDEXES:
            conn.execute(stmt)
        for view in _VIEWS:
            conn.execute(view)
        conn.execute(
            "INSERT OR REPLACE INTO forge_meta(key, value) VALUES (?, ?)",
            ["schema_version", str(SCHEMA_VERSION)],
        )

    # --- Writes ---------------------------------------------------------

    _INSERT_SQL = """
        INSERT OR IGNORE INTO events (
            event_id, run_id, generation_id, parent_event_id,
            project, project_fingerprint, factory_version, spec_version,
            ts, duration_ms,
            kind, payload_schema_version, payload, artifacts,
            tokens_in, tokens_out, cost_usd, model, model_version,
            success, error_class, error_msg
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def append(self, event: Event) -> None:
        """Schreibt ein einzelnes Event. Idempotent über `event_id`."""
        self._conn.execute(self._INSERT_SQL, _event_to_row(event))

    def append_many(self, events: list[Event]) -> None:
        """Bulk-Insert via DuckDB `executemany`.

        Eine Transaktion umschließt den Aufruf — bei Fehler wird gerollbackt.
        Für DuckDB ist `executemany` der schnelle Pfad; einzelne `execute`-Calls
        in einer Schleife sind selbst innerhalb einer Transaktion deutlich
        langsamer.
        """
        if not events:
            return
        conn = self._conn
        rows = [_event_to_row(evt) for evt in events]
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.executemany(self._INSERT_SQL, rows)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # --- Reads ----------------------------------------------------------

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0]) if row else 0

    def events_for_run(self, run_id: str) -> list[Event]:
        cur = self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY ts ASC, event_id ASC",
            [run_id],
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [_row_to_event(dict(zip(cols, r, strict=True))) for r in rows]

    def events_by_kind(self, kind: EventKind, *, limit: int = 1000) -> list[Event]:
        cur = self._conn.execute(
            "SELECT * FROM events WHERE kind = ? ORDER BY ts DESC LIMIT ?",
            [kind.value, limit],
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [_row_to_event(dict(zip(cols, r, strict=True))) for r in rows]

    def referenced_artifact_hashes(self, *, since_days: int | None = None) -> set[str]:
        """Sammelt alle Artefakt-Hashes aus Events.

        Wird vom CAS-Garbage-Collector als `keep`-Set genutzt: Blobs, die in einem
        Event referenziert sind, werden nicht gelöscht.
        """
        sql = "SELECT artifacts FROM events"
        params: list[Any] = []
        if since_days is not None:
            sql += f" WHERE ts >= now() - INTERVAL '{int(since_days)} days'"
        rows = self._conn.execute(sql, params).fetchall()
        out: set[str] = set()
        for (artifacts_json,) in rows:
            if not artifacts_json:
                continue
            try:
                d = json.loads(artifacts_json)
            except json.JSONDecodeError:
                continue
            for v in d.values():
                if isinstance(v, str):
                    out.add(v)
        return out

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Read-only SQL für `forge analyze`. Liefert Liste von Dicts."""
        cur = self._conn.execute(sql, params or [])
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in rows]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"not JSON-serializable: {type(obj).__name__}")


def _event_to_row(evt: Event) -> list[Any]:
    return [
        evt.event_id,
        evt.run_id,
        evt.generation_id,
        evt.parent_event_id,
        evt.project,
        evt.project_fingerprint,
        evt.factory_version,
        evt.spec_version,
        evt.ts,
        evt.duration_ms,
        evt.kind.value,
        evt.payload_schema_version,
        json.dumps(evt.payload, default=_json_default),
        json.dumps(evt.artifacts),
        evt.tokens_in,
        evt.tokens_out,
        evt.cost_usd,
        evt.model,
        evt.model_version,
        evt.success,
        evt.error_class,
        evt.error_msg,
    ]


def _row_to_event(row: dict[str, Any]) -> Event:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    artifacts = row["artifacts"]
    if isinstance(artifacts, str):
        artifacts = json.loads(artifacts)
    ts = row["ts"]
    # DuckDB returns TIMESTAMPTZ in process-local timezone; normalise to UTC
    # so Event's ts validator accepts it.
    if isinstance(ts, datetime) and ts.tzinfo is not None:
        ts = ts.astimezone(UTC)
    return Event(
        event_id=row["event_id"],
        run_id=row["run_id"],
        generation_id=row["generation_id"],
        parent_event_id=row["parent_event_id"],
        project=row["project"],
        project_fingerprint=row["project_fingerprint"],
        factory_version=row["factory_version"],
        spec_version=row["spec_version"],
        ts=ts,
        duration_ms=row["duration_ms"],
        kind=EventKind(row["kind"]),
        payload_schema_version=row["payload_schema_version"],
        payload=payload,
        artifacts=artifacts or {},
        tokens_in=row["tokens_in"] or 0,
        tokens_out=row["tokens_out"] or 0,
        cost_usd=Decimal(str(row["cost_usd"] or "0")),
        model=row["model"],
        model_version=row["model_version"],
        success=row["success"],
        error_class=row["error_class"],
        error_msg=row["error_msg"],
    )
