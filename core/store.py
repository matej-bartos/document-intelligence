"""SQLite persistence — běhy extrakce, výsledná pole a uložená schémata.

Držíme dvě úrovně: `extractions` s metrikami jednoho běhu a `field_results`
s jednotlivými poli, aby šlo dotazovat review frontu a exportovat tabulku
bez rozbalování JSONu.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .extractor import ExtractionResult
from .schema import ExtractionSchema

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "extractions.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS extractions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL,
    filename       TEXT    NOT NULL,
    schema_name    TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    effort         TEXT    NOT NULL,
    ok             INTEGER NOT NULL,
    document_type  TEXT,
    notes          TEXT,
    error          TEXT,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    thinking_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0,
    raw_json        TEXT
);

CREATE TABLE IF NOT EXISTS field_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id  INTEGER NOT NULL REFERENCES extractions(id) ON DELETE CASCADE,
    field_name     TEXT    NOT NULL,
    field_kind     TEXT    NOT NULL,
    value_json     TEXT,
    confidence     TEXT    NOT NULL,
    source_text    TEXT,
    reviewed       INTEGER NOT NULL DEFAULT 0,
    reviewed_value TEXT
);

CREATE TABLE IF NOT EXISTS schemas (
    name        TEXT PRIMARY KEY,
    updated_at  TEXT NOT NULL,
    payload     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_field_extraction ON field_results(extraction_id);
CREATE INDEX IF NOT EXISTS idx_extraction_created ON extractions(created_at);
"""


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA_SQL)
    return conn


# ------------------------------------------------------------------ zápis


def save_extraction(
    conn: sqlite3.Connection, result: ExtractionResult, schema_name: str
) -> int:
    cur = conn.execute(
        """
        INSERT INTO extractions (
            created_at, filename, schema_name, model, effort, ok, document_type,
            notes, error, latency_ms, prompt_tokens, output_tokens, thinking_tokens,
            cached_tokens, cost_usd, raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            result.filename,
            schema_name,
            result.model,
            result.effort,
            int(result.ok),
            result.document_type,
            result.notes,
            result.error,
            result.latency_ms,
            result.prompt_tokens,
            result.output_tokens,
            result.thinking_tokens,
            result.cached_tokens,
            result.cost_usd,
            json.dumps(result.raw, ensure_ascii=False) if result.raw else None,
        ),
    )
    extraction_id = int(cur.lastrowid)

    conn.executemany(
        """
        INSERT INTO field_results (
            extraction_id, field_name, field_kind, value_json, confidence, source_text
        ) VALUES (?,?,?,?,?,?)
        """,
        [
            (
                extraction_id,
                f.name,
                f.kind,
                json.dumps(f.value, ensure_ascii=False),
                f.confidence,
                f.source_text,
            )
            for f in result.fields
        ],
    )
    conn.commit()
    return extraction_id


def apply_review(
    conn: sqlite3.Connection, field_id: int, corrected_value: str | None
) -> None:
    """Zaznamená lidskou opravu. Původní hodnotu modelu nepřepisujeme —
    je potřeba pro pozdější měření přesnosti."""
    conn.execute(
        "UPDATE field_results SET reviewed = 1, reviewed_value = ? WHERE id = ?",
        (corrected_value, field_id),
    )
    conn.commit()


def save_schema(conn: sqlite3.Connection, schema: ExtractionSchema) -> None:
    conn.execute(
        """
        INSERT INTO schemas (name, updated_at, payload) VALUES (?,?,?)
        ON CONFLICT(name) DO UPDATE SET updated_at = excluded.updated_at,
                                        payload = excluded.payload
        """,
        (
            schema.name,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            json.dumps(schema.to_dict(), ensure_ascii=False),
        ),
    )
    conn.commit()


def delete_schema(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM schemas WHERE name = ?", (name,))
    conn.commit()


# ------------------------------------------------------------------- čtení


def load_schemas(conn: sqlite3.Connection) -> dict[str, ExtractionSchema]:
    rows = conn.execute("SELECT name, payload FROM schemas ORDER BY name").fetchall()
    return {
        r["name"]: ExtractionSchema.from_dict(json.loads(r["payload"])) for r in rows
    }


def list_extractions(
    conn: sqlite3.Connection, limit: int = 200
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM extractions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_fields(conn: sqlite3.Connection, extraction_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM field_results WHERE extraction_id = ? ORDER BY id",
        (extraction_id,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["value"] = json.loads(item["value_json"]) if item["value_json"] else None
        out.append(item)
    return out


def review_queue(
    conn: sqlite3.Connection, confidences: tuple[str, ...] = ("low",)
) -> list[dict[str, Any]]:
    """Pole, která model označil jako nejistá a člověk je zatím nepotvrdil."""
    placeholders = ",".join("?" for _ in confidences)
    rows = conn.execute(
        f"""
        SELECT fr.*, e.filename, e.created_at, e.schema_name
        FROM field_results fr
        JOIN extractions e ON e.id = fr.extraction_id
        WHERE fr.reviewed = 0 AND fr.confidence IN ({placeholders})
        ORDER BY fr.extraction_id DESC, fr.id
        """,
        confidences,
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["value"] = json.loads(item["value_json"]) if item["value_json"] else None
        out.append(item)
    return out


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*)                           AS runs,
               SUM(ok)                            AS ok_runs,
               COALESCE(SUM(cost_usd), 0)         AS total_cost,
               COALESCE(AVG(latency_ms), 0)       AS avg_latency,
               COALESCE(SUM(prompt_tokens), 0)    AS prompt_tokens,
               COALESCE(SUM(output_tokens), 0)    AS output_tokens,
               COALESCE(SUM(thinking_tokens), 0)  AS thinking_tokens
        FROM extractions
        """
    ).fetchone()
    conf = conn.execute(
        "SELECT confidence, COUNT(*) AS n FROM field_results GROUP BY confidence"
    ).fetchall()
    return {
        **dict(row),
        "confidence_counts": {r["confidence"]: r["n"] for r in conf},
    }
