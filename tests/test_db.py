from health_tracker import db
from health_tracker.db import ImportResult
from health_tracker.models import RawMessage
from health_tracker.sample_import import load_sample


def msg(id_=None, text="BP 120/80", sender="Amma", raw="10:02 PM, 10/20/2026", iso=None):
    return RawMessage(id_, sender, raw, iso, text, False)


def test_importing_twice_adds_nothing_the_second_time(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    msgs = load_sample().messages
    n = len(msgs)
    assert db.insert_messages(conn, "sample", msgs) == ImportResult(n, 0, 0, 0)
    assert db.insert_messages(conn, "sample", msgs) == ImportResult(0, 0, n, 0)
    assert len(db.list_messages(conn)) == n


def test_new_messages_are_added_alongside_existing_ones(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.insert_messages(conn, "whatsapp", [msg("A1")])
    r = db.insert_messages(conn, "whatsapp", [msg("A1"), msg("A2")])
    assert r == ImportResult(1, 0, 1, 0)


def test_same_text_with_different_ids_is_kept(tmp_path):
    # Two real messages can say the same thing; ids tell them apart.
    conn = db.connect(tmp_path / "t.db")
    assert db.insert_messages(conn, "whatsapp", [msg("A1"), msg("A2")]).new == 2


def test_without_an_id_content_hash_catches_repeats(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.insert_messages(conn, "whatsapp", [msg(None, "70 kg")]).new == 1
    assert db.insert_messages(conn, "whatsapp", [msg(None, "70 kg")]).duplicate == 1
    assert db.insert_messages(conn, "whatsapp", [msg(None, "71 kg")]).new == 1


def test_unknown_values_stay_null_and_raw_time_is_kept(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.insert_messages(conn, "whatsapp", [msg("A1")])
    row = db.list_messages(conn)[0]
    assert row["sent_at"] is None
    assert row["sent_at_raw"] == "10:02 PM, 10/20/2026"


def test_unrendered_shells_are_skipped_not_stored(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    shell = RawMessage("A1", None, None, None, "", False)
    assert db.insert_messages(conn, "whatsapp", [shell]) == ImportResult(0, 0, 0, 1)
    assert db.list_messages(conn) == []


def test_a_later_complete_version_fills_in_a_partial_one(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    partial = RawMessage("A1", None, "10:02 PM", None, "", False)   # time only
    full = RawMessage("A1", "Amma", "10:02 PM, 10/20/2026", "2026-10-20T22:02",
                      "LB149.4", False)
    assert db.insert_messages(conn, "whatsapp", [partial]).new == 1
    assert db.insert_messages(conn, "whatsapp", [full]).updated == 1
    row = db.list_messages(conn)[0]
    assert (row["text"], row["sender"], row["sent_at"]) == ("LB149.4", "Amma", "2026-10-20T22:02")
    assert db.insert_messages(conn, "whatsapp", [full]).duplicate == 1


def test_filling_blanks_never_overwrites_existing_text(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.insert_messages(conn, "whatsapp", [msg("A1", text="BP 120/80")])
    r = db.insert_messages(conn, "whatsapp", [msg("A1", text="BP 999/99")])
    assert r.duplicate == 1
    assert db.list_messages(conn)[0]["text"] == "BP 120/80"


# ---------- review and readings ----------

def _reading(**kw):
    base = dict(person=None, measured_at=None, systolic=None, diastolic=None, pulse=None,
                weight=None, weight_unit=None, glucose=None, glucose_unit=None, note=None)
    return {**base, **kw}


def _stored_id(conn, text="BP 120/80"):
    db.insert_messages(conn, "export", [msg("M1", text, iso="2026-10-20T22:02:00")])
    return db.list_messages(conn)[0]["id"]


def test_confirming_saves_reading_and_leaves_the_queue(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    mid = _stored_id(conn)
    assert len(db.pending_messages(conn, "export")) == 1
    assert db.add_reading(conn, mid, _reading(systolic=120, diastolic=80)) is True
    assert db.pending_messages(conn, "export") == []
    assert len(db.list_readings(conn)) == 1


def test_saving_twice_does_not_duplicate(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    mid = _stored_id(conn)
    db.add_reading(conn, mid, _reading(systolic=120, diastolic=80))
    assert db.add_reading(conn, mid, _reading(systolic=999, diastolic=99)) is False
    rows = db.list_readings(conn)
    assert len(rows) == 1 and rows[0]["systolic"] == 120


def test_discarded_messages_leave_the_queue_without_a_reading(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    mid = _stored_id(conn)
    db.set_review_status(conn, mid, "discarded")
    assert db.pending_messages(conn, "export") == [] and db.list_readings(conn) == []


def test_message_time_and_measurement_time_are_kept_separately(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    mid = _stored_id(conn)
    db.add_reading(conn, mid, _reading(systolic=120, diastolic=80))          # no measured_at
    row = db.list_readings(conn)[0]
    assert row["measured_at"] is None
    assert row["message_sent_at"] == "2026-10-20T22:02:00"
    assert row["when"] == "2026-10-20T22:02:00"                              # fallback, not stored

    db.add_reading(conn, None, _reading(weight=70, weight_unit="kg", measured_at="2026-10-19T07:30"))
    manual = [r for r in db.list_readings(conn) if r["message_sent_at"] is None][0]
    assert manual["measured_at"] == "2026-10-19T07:30"


def test_unit_stays_null_when_not_stated(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.add_reading(conn, None, _reading(weight=153.6))
    assert db.list_readings(conn)[0]["weight_unit"] is None


def test_reset_clears_readings_too(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.add_reading(conn, _stored_id(conn), _reading(pulse=70))
    db.clear_messages(conn)
    assert db.list_readings(conn) == [] and db.list_messages(conn) == []


def test_old_database_gets_review_status(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, source TEXT NOT NULL, "
                "dedupe_key TEXT NOT NULL, source_message_id TEXT, sender TEXT, sent_at_raw TEXT, "
                "sent_at TEXT, text TEXT NOT NULL, has_image INTEGER NOT NULL, "
                "imported_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (source, dedupe_key))")
    old.execute("INSERT INTO messages (source, dedupe_key, text, has_image) VALUES ('export','k','hi',0)")
    old.commit(); old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT review_status FROM messages").fetchone()[0] == "pending"
