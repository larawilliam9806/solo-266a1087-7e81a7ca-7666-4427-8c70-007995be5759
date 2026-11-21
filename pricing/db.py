"""SQLite storage for uploaded boxes, ruling lines and human decisions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path, timeout=15.0):
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # WAL + a busy timeout keep two gunicorn workers from tripping over each
    # other's short writes.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def init_db(path):
    connection = connect(path)
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


def _rows_to_dicts(rows):
    return [dict(row) for row in rows]


def get_job(connection, job_id):
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def list_jobs(connection):
    rows = connection.execute(
        "SELECT j.*, (SELECT COUNT(*) FROM boxes b WHERE b.job_id = j.id) AS box_count,"
        " (SELECT COUNT(*) FROM lines l WHERE l.job_id = j.id) AS line_count"
        " FROM jobs j ORDER BY j.id DESC"
    ).fetchall()
    return _rows_to_dicts(rows)


def create_job(connection, filename):
    cursor = connection.execute(
        "INSERT INTO jobs (filename) VALUES (?)", (filename,)
    )
    connection.commit()
    return cursor.lastrowid


def delete_job(connection, job_id):
    connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    connection.commit()


def replace_box(connection, job_id, row):
    """Insert one OCR box; UNIQUE(job_id,row_no) makes re-upload idempotent."""
    connection.execute(
        "INSERT OR REPLACE INTO boxes"
        " (job_id, row_no, box_ref, text, x1, y1, x2, y2)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, row["row_no"], row["box_ref"], row["text"],
         row["x1"], row["y1"], row["x2"], row["y2"]),
    )


def add_boxes(connection, job_id, rows):
    for row in rows:
        replace_box(connection, job_id, row)
    connection.commit()


def get_boxes(connection, job_id):
    rows = connection.execute(
        "SELECT * FROM boxes WHERE job_id = ? ORDER BY row_no", (job_id,)
    ).fetchall()
    return _rows_to_dicts(rows)


def set_lines(connection, job_id, horizontal, vertical):
    """Replace a job's lines.  The caller commits (possibly several jobs)."""
    connection.execute("DELETE FROM lines WHERE job_id = ?", (job_id,))
    for position in horizontal:
        connection.execute(
            "INSERT INTO lines (job_id, orientation, position) VALUES (?, 'h', ?)",
            (job_id, position),
        )
    for position in vertical:
        connection.execute(
            "INSERT INTO lines (job_id, orientation, position) VALUES (?, 'v', ?)",
            (job_id, position),
        )


def replace_lines(connection, job_id, horizontal, vertical):
    set_lines(connection, job_id, horizontal, vertical)
    connection.commit()


def get_lines(connection, job_id):
    rows = connection.execute(
        "SELECT orientation, position FROM lines WHERE job_id = ?"
        " ORDER BY orientation, position", (job_id,)
    ).fetchall()
    horizontal = [row["position"] for row in rows if row["orientation"] == "h"]
    vertical = [row["position"] for row in rows if row["orientation"] == "v"]
    return horizontal, vertical


def get_decisions(connection, job_id):
    rows = connection.execute(
        "SELECT * FROM decisions WHERE job_id = ?", (job_id,)
    ).fetchall()
    decisions = {}
    for row in rows:
        record = dict(row)
        try:
            record["overlap_cells"] = json.loads(record["overlap_cells"] or "[]")
        except ValueError:
            record["overlap_cells"] = []
        decisions[record["box_id"]] = record
    return decisions


def set_decision(connection, job_id, box_id, kind, target_cell, overlap_cells):
    connection.execute(
        "INSERT INTO decisions"
        " (job_id, box_id, kind, target_cell, overlap_cells)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (job_id, box_id) DO UPDATE SET"
        " kind = excluded.kind, target_cell = excluded.target_cell,"
        " overlap_cells = excluded.overlap_cells,"
        " created_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (job_id, box_id, kind, target_cell,
         json.dumps(sorted(overlap_cells), ensure_ascii=False)),
    )
    connection.commit()


def clear_decision(connection, job_id, box_id):
    connection.execute(
        "DELETE FROM decisions WHERE job_id = ? AND box_id = ?", (job_id, box_id)
    )
    connection.commit()


# ---------------------------------------------------------------------------
# Cross-page bindings
# ---------------------------------------------------------------------------

def _binding_record(row):
    record = dict(row)
    for field in ("pause_reasons", "affected_boxes"):
        try:
            record[field] = json.loads(record[field] or "[]")
        except ValueError:
            record[field] = []
    record["paused"] = bool(record["paused"])
    return record


def get_binding(connection, target_job_id):
    """The binding where this job is the follower, or None."""
    row = connection.execute(
        "SELECT * FROM bindings WHERE target_job_id = ?", (target_job_id,)
    ).fetchone()
    return _binding_record(row) if row else None


def list_followers(connection, reference_job_id):
    """Bindings where this job is the reference, oldest first."""
    rows = connection.execute(
        "SELECT * FROM bindings WHERE reference_job_id = ?"
        " ORDER BY created_at, target_job_id", (reference_job_id,)
    ).fetchall()
    return [_binding_record(row) for row in rows]


def count_followers(connection, reference_job_id):
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM bindings WHERE reference_job_id = ?",
        (reference_job_id,),
    ).fetchone()
    return int(row["n"])


def create_binding(connection, target_job_id, reference_job_id, paused=False,
                   pause_reasons=None, affected_boxes=None):
    """Bind target -> reference.  The caller commits with any line writes."""
    connection.execute(
        "INSERT INTO bindings"
        " (target_job_id, reference_job_id, paused, pause_reasons, affected_boxes)"
        " VALUES (?, ?, ?, ?, ?)",
        (target_job_id, reference_job_id, 1 if paused else 0,
         json.dumps(list(pause_reasons or []), ensure_ascii=False),
         json.dumps(list(affected_boxes or []), ensure_ascii=False)),
    )


def update_binding(connection, target_job_id, paused, pause_reasons=None,
                   affected_boxes=None):
    connection.execute(
        "UPDATE bindings SET paused = ?, pause_reasons = ?, affected_boxes = ?,"
        " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        " WHERE target_job_id = ?",
        (1 if paused else 0,
         json.dumps(list(pause_reasons or []), ensure_ascii=False),
         json.dumps(list(affected_boxes or []), ensure_ascii=False),
         target_job_id),
    )


def delete_binding(connection, target_job_id):
    connection.execute(
        "DELETE FROM bindings WHERE target_job_id = ?", (target_job_id,)
    )


@contextmanager
def transaction(connection):
    """All-or-nothing write block.

    The web layer commits once per request; if anything raises, every write
    from this block is rolled back so no page keeps a half-applied state.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
