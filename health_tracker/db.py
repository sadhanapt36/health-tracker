"""SQLite storage. Plain sqlite3, no ORM, so every query is visible.

Milestone 2 stores collected messages and refuses duplicates.
Readings and confirmation come in later milestones.
"""
import hashlib
import sqlite3
from pathlib import Path
from typing import NamedTuple, Optional

from .models import RawMessage

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL,   -- 'whatsapp' or 'sample'
    dedupe_key        TEXT NOT NULL,   -- see dedupe_key() below
    source_message_id TEXT,            -- WhatsApp's own id, when we have it
    sender            TEXT,
    sent_at_raw       TEXT,            -- exactly as WhatsApp displayed it
    sent_at           TEXT,            -- ISO time, NULL when unknown
    text              TEXT NOT NULL,
    attachment        TEXT,            -- media file name from a chat export
    has_image         INTEGER NOT NULL,
    review_status     TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'confirmed' | 'discarded'
    imported_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, dedupe_key)
);

-- Only reviewed, confirmed readings live here. message_id is NULL for manual entry.
-- sent_at on the message and measured_at here are separate on purpose: the time a
-- message arrived is not necessarily the time the measurement was taken.
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY,
    message_id    INTEGER REFERENCES messages(id),
    person        TEXT,
    measured_at   TEXT,               -- ISO text, NULL when unknown
    systolic      INTEGER,
    diastolic     INTEGER,
    pulse         INTEGER,
    weight        REAL,
    weight_unit   TEXT,               -- 'kg' | 'lb' | NULL (not stated)
    glucose       REAL,
    glucose_unit  TEXT,               -- 'mg/dL' | 'mmol/L' | NULL (not stated)
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (message_id)
);

-- What was read from a photo, kept so each photo is scanned once (and so the
-- cloud reader is never called twice for the same picture). A draft only:
-- nothing here counts as a reading until a person confirms it.
CREATE TABLE IF NOT EXISTS image_extractions (
    message_id    INTEGER PRIMARY KEY REFERENCES messages(id),
    kind          TEXT NOT NULL,      -- 'blood_pressure' | 'glucose' | 'weight' | 'other'
    values_json   TEXT NOT NULL,      -- the ImageDraft fields, as JSON
    reader        TEXT,
    scanned_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class ImportResult(NamedTuple):
    new: int         # stored for the first time
    updated: int     # already stored, but missing details we now have
    duplicate: int   # already stored, nothing to add
    skipped: int     # WhatsApp hadn't finished rendering them; not stored


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Databases made before the attachment column existed get it added.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "attachment" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment TEXT")
    if "review_status" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'")
    return conn


def dedupe_key(m: RawMessage) -> str:
    """Prefer WhatsApp's stable message id. Without one, hash the content."""
    if m.source_message_id:
        return f"id:{m.source_message_id}"
    basis = "|".join([m.sender or "", m.sent_at_raw or "", m.text])
    return "hash:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def is_unrendered(m: RawMessage) -> bool:
    """An empty shell: WhatsApp gave us an id before the content had loaded.

    Storing it would be harmful. The same id would later count as a duplicate
    and block the real content from ever being saved.
    """
    return not m.text and not m.has_image and not m.sent_at_raw


def _fill_blanks(conn: sqlite3.Connection, row: sqlite3.Row, m: RawMessage) -> bool:
    """Complete a stored row with details it lacked. Never overwrites."""
    changes = {}
    if not row["text"] and m.text:
        changes["text"] = m.text
    if not row["sender"] and m.sender:
        changes["sender"] = m.sender
    if row["sent_at"] is None and m.sent_at:
        changes["sent_at"] = m.sent_at          # a full date beats time-of-day
        changes["sent_at_raw"] = m.sent_at_raw
    elif not row["sent_at_raw"] and m.sent_at_raw:
        changes["sent_at_raw"] = m.sent_at_raw
    if m.has_image and not row["has_image"]:
        changes["has_image"] = 1
    if not changes:
        return False
    sets = ", ".join(f"{col} = ?" for col in changes)  # col names are fixed above
    conn.execute(f"UPDATE messages SET {sets} WHERE id = ?", (*changes.values(), row["id"]))
    return True


def insert_messages(conn: sqlite3.Connection, source: str,
                    messages: list[RawMessage]) -> ImportResult:
    new = updated = duplicate = skipped = 0
    for m in messages:
        if is_unrendered(m):
            skipped += 1
            continue
        key = dedupe_key(m)
        existing = conn.execute(
            "SELECT * FROM messages WHERE source = ? AND dedupe_key = ?", (source, key)
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO messages
                   (source, dedupe_key, source_message_id, sender,
                    sent_at_raw, sent_at, text, attachment, has_image)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, key, m.source_message_id, m.sender,
                 m.sent_at_raw, m.sent_at, m.text, m.attachment, int(m.has_image)),
            )
            new += 1
        elif _fill_blanks(conn, existing, m):
            updated += 1
        else:
            duplicate += 1
    conn.commit()
    return ImportResult(new, updated, duplicate, skipped)


def list_messages(conn: sqlite3.Connection, source: Optional[str] = None) -> list[sqlite3.Row]:
    """Newest first. Optionally only one source ('export', 'sample', 'whatsapp')."""
    where, args = ("WHERE source = ?", (source,)) if source else ("", ())
    return conn.execute(
        f"SELECT * FROM messages {where} ORDER BY sent_at DESC, id DESC", args
    ).fetchall()


def clear_messages(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM readings")
    conn.execute("DELETE FROM image_extractions")
    conn.execute("DELETE FROM messages")
    conn.commit()


# ---------- review and confirmed readings ----------

def pending_messages(conn: sqlite3.Connection, source: str) -> list[sqlite3.Row]:
    """Typed messages nobody has confirmed or discarded yet, newest first."""
    return conn.execute(
        "SELECT * FROM messages WHERE source = ? AND review_status = 'pending' AND text != '' "
        "ORDER BY sent_at DESC, id DESC", (source,)).fetchall()


def set_review_status(conn: sqlite3.Connection, message_id: int, status: str) -> None:
    conn.execute("UPDATE messages SET review_status = ? WHERE id = ?", (status, message_id))
    conn.commit()


def add_reading(conn: sqlite3.Connection, message_id: Optional[int], reading: dict) -> bool:
    """Save a confirmed reading and mark its message confirmed, together.
    Returns False if that message already has a saved reading (e.g. a double click)."""
    try:
        conn.execute(
            """INSERT INTO readings (message_id, person, measured_at, systolic, diastolic, pulse,
                                     weight, weight_unit, glucose, glucose_unit, note)
               VALUES (:message_id, :person, :measured_at, :systolic, :diastolic, :pulse,
                       :weight, :weight_unit, :glucose, :glucose_unit, :note)""",
            {**reading, "message_id": message_id})
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    if message_id is not None:
        conn.execute("UPDATE messages SET review_status = 'confirmed' WHERE id = ?", (message_id,))
    conn.commit()
    return True


def list_readings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Confirmed readings, newest first. "when" falls back to the message time
    only when no measurement time was entered."""
    return conn.execute(
        """SELECT r.id, r.person, r.measured_at, m.sent_at AS message_sent_at,
                  COALESCE(r.measured_at, m.sent_at, r.created_at) AS "when",
                  r.systolic, r.diastolic, r.pulse, r.weight, r.weight_unit,
                  r.glucose, r.glucose_unit, r.note, m.text AS source_text
           FROM readings r LEFT JOIN messages m ON m.id = r.message_id
           ORDER BY "when" DESC, r.id DESC""").fetchall()


# ---------- photo scans ----------

def save_extraction(conn: sqlite3.Connection, message_id: int, kind: str,
                    values: dict, reader: str) -> None:
    import json
    conn.execute(
        "INSERT OR REPLACE INTO image_extractions (message_id, kind, values_json, reader) "
        "VALUES (?, ?, ?, ?)", (message_id, kind, json.dumps(values), reader))
    conn.commit()


def get_extraction(conn: sqlite3.Connection, message_id: int) -> Optional[dict]:
    import json
    row = conn.execute("SELECT * FROM image_extractions WHERE message_id = ?",
                       (message_id,)).fetchone()
    return None if row is None else {"kind": row["kind"], "reader": row["reader"],
                                     **json.loads(row["values_json"])}


def photos_to_scan(conn: sqlite3.Connection, source: str, senders=None,
                   since: Optional[str] = None) -> list[sqlite3.Row]:
    """Photo messages that have a file name and have not been scanned yet.
    `senders` limits who (the chat holds other people's photos too);
    `since` is an ISO date, e.g. '2026-06-01'."""
    sql = ("SELECT m.* FROM messages m LEFT JOIN image_extractions e ON e.message_id = m.id "
           "WHERE m.source = ? AND m.has_image = 1 AND m.attachment IS NOT NULL "
           "AND e.message_id IS NULL AND m.review_status = 'pending'")
    args: list = [source]
    if senders:
        sql += f" AND m.sender IN ({','.join('?' * len(senders))})"
        args += list(senders)
    if since:
        sql += " AND m.sent_at >= ?"
        args.append(since)
    return conn.execute(sql + " ORDER BY m.sent_at, m.id", args).fetchall()


def senders(conn: sqlite3.Connection, source: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT sender FROM messages WHERE source = ? AND sender IS NOT NULL "
        "ORDER BY sender", (source,))]


def pending_photo_readings(conn: sqlite3.Connection, source: str) -> list[tuple]:
    """(message row, extraction dict) for scanned photos that hold values and are
    still unreviewed, newest first."""
    out = []
    rows = conn.execute(
        "SELECT m.* FROM messages m JOIN image_extractions e ON e.message_id = m.id "
        "WHERE m.source = ? AND m.review_status = 'pending' AND e.kind != 'other' "
        "ORDER BY m.sent_at DESC, m.id DESC", (source,)).fetchall()
    for r in rows:
        ex = get_extraction(conn, r["id"])
        if ex and any(ex.get(k) is not None for k in
                      ("systolic", "diastolic", "pulse", "glucose", "weight")):
            out.append((r, ex))
    return out
