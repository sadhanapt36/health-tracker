import zipfile

import pytest

from health_tracker import db
from health_tracker.chat_export import (ChatExportError, detect_date_order,
                                        parse_chat, read_chat_text)
from health_tracker.sample_import import load_sample

NNBSP, LRM = "\u202f", "\u200e"


def test_parses_invisible_characters_and_timestamp():
    text = f"{LRM}[11/9/23, 10:16:01{NNBSP}PM] Gramps: BP 120/80\r\n"
    m = parse_chat(text).messages[0]
    assert m.text == "BP 120/80" and m.sender == "Gramps"
    assert m.sent_at == "2023-11-09T22:16:01"
    assert m.sent_at_raw == "11/9/23, 10:16:01 PM"


def test_12am_and_12pm_convert_correctly():
    text = "[1/2/24, 12:05:00 AM] A: x\n[1/2/24, 12:05:00 PM] A: y\n"
    a, b = parse_chat(text).messages
    assert a.sent_at == "2024-01-02T00:05:00" and b.sent_at == "2024-01-02T12:05:00"


def test_multiline_message_is_joined():
    text = "[1/2/24, 9:00:00 AM] A: Numbers\nBP 118/76\nPulse 68\n[1/2/24, 9:01:00 AM] A: ok\n"
    msgs = parse_chat(text).messages
    assert msgs[0].text == "Numbers\nBP 118/76\nPulse 68" and len(msgs) == 2


def test_calls_and_system_notices_are_ignored_and_counted():
    text = ("[1/2/24, 9:00:00 AM] Chat: Messages and calls are end-to-end encrypted.\n"
            "[1/2/24, 9:01:00 AM] A: Voice call, No answer\n"
            "[1/2/24, 9:02:00 AM] A: Missed voice call, Tap to call back\n"
            "[1/2/24, 9:03:00 AM] A: hello\n")
    r = parse_chat(text)
    assert [m.text for m in r.messages] == ["hello"]
    assert r.ignored == {"system notices": 1, "call notices": 2}


def test_photo_attachment_and_caption():
    text = "[1/2/24, 9:00:00 AM] A: Look <attached: 00000001-PHOTO-2024-01-02-09-00-00.jpg>\n"
    m = parse_chat(text).messages[0]
    assert m.has_image and m.text == "Look"
    assert m.attachment == "00000001-PHOTO-2024-01-02-09-00-00.jpg"


def test_non_image_media_is_ignored_unless_captioned():
    text = ("[1/2/24, 9:00:00 AM] A: <attached: 00000002-VIDEO-x.mp4>\n"
            "[1/2/24, 9:01:00 AM] A: BP reading <attached: 00000003-VIDEO-y.mp4>\n")
    r = parse_chat(text)
    assert len(r.messages) == 1 and r.messages[0].text == "BP reading"
    assert not r.messages[0].has_image
    assert r.ignored == {"videos/audio/documents": 1}


def test_without_media_export_still_flags_photos():
    r = parse_chat("[1/2/24, 9:00:00 AM] A: image omitted\n[1/2/24, 9:01:00 AM] A: video omitted\n")
    assert len(r.messages) == 1
    assert r.messages[0].has_image and r.messages[0].attachment is None


def test_empty_sender_line_does_not_crash_or_create_a_message():
    assert parse_chat("[1/2/24, 9:00:00 AM] A:\n").messages == []


def test_date_order_detection():
    assert detect_date_order(["11/21/23", "1/2/24"]) == ("MDY", True)
    assert detect_date_order(["21/11/23", "1/2/24"]) == ("DMY", True)
    assert detect_date_order(["1/2/24", "3/4/24"]) == ("MDY", False)  # a guess, flagged
    with pytest.raises(ChatExportError):
        detect_date_order(["21/21/23"])


def test_day_first_file_parses_with_detected_order():
    r = parse_chat("[21/11/23, 9:00:00 AM] A: x\n[1/2/24, 9:00:00 AM] A: y\n")
    assert r.messages[0].sent_at == "2023-11-21T09:00:00"
    assert r.messages[1].sent_at == "2024-02-01T09:00:00"


def test_identical_messages_get_distinct_stable_ids():
    text = "[1/2/24, 9:00:00 AM] A: 70 kg\n[1/2/24, 9:00:00 AM] A: 70 kg\n"
    ids = [m.source_message_id for m in parse_chat(text).messages]
    assert len(set(ids)) == 2
    assert ids == [m.source_message_id for m in parse_chat(text).messages]


def test_incremental_import_adds_only_what_is_new(tmp_path):
    first = "[1/2/24, 9:00:00 AM] A: one\n[1/2/24, 9:01:00 AM] A: two\n"
    later = first + "[1/3/24, 9:00:00 AM] A: three\n"  # a newer export holds everything
    conn = db.connect(tmp_path / "t.db")
    assert db.insert_messages(conn, "export", parse_chat(first).messages).new == 2
    r = db.insert_messages(conn, "export", parse_chat(later).messages)
    assert (r.new, r.duplicate) == (1, 2)


def test_sample_export_parses():
    r = load_sample()
    assert r.date_order == "MDY" and r.date_order_certain
    assert r.ignored == {"system notices": 1, "call notices": 1}
    assert all(m.sent_at for m in r.messages)
    assert any(m.has_image for m in r.messages)
    texts = [m.text for m in r.messages]
    assert texts.count("Sugar 6.1 mmol/L") == 2          # duplicates kept apart...
    ids = [m.source_message_id for m in r.messages]
    assert len(ids) == len(set(ids))                      # ...by distinct ids


def test_read_chat_text_from_zip_ignores_macos_junk(tmp_path):
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("__MACOSX/Chat/._x.txt", "junk")
        f.writestr("Chat/_chat.txt", "[1/2/24, 9:00:00 AM] A: hi\n")
        f.writestr("Chat/00000001-PHOTO-x.jpg", b"\xff\xd8")
    assert "hi" in read_chat_text(z)


def test_zip_without_chat_text_gives_a_helpful_error(tmp_path):
    z = tmp_path / "media_only.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Chat/00000001-PHOTO-x.jpg", b"\xff\xd8")
    with pytest.raises(ChatExportError, match="_chat.txt"):
        read_chat_text(z)


def test_old_database_gets_attachment_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, source TEXT NOT NULL, "
                "dedupe_key TEXT NOT NULL, source_message_id TEXT, sender TEXT, sent_at_raw TEXT, "
                "sent_at TEXT, text TEXT NOT NULL, has_image INTEGER NOT NULL, "
                "imported_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (source, dedupe_key))")
    old.commit(); old.close()
    conn = db.connect(path)
    assert "attachment" in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}


def test_file_with_no_chat_messages_is_an_error_not_an_empty_success():
    with pytest.raises(ChatExportError, match="No chat messages"):
        parse_chat("just some notes\nnot a chat export\n")
    with pytest.raises(ChatExportError):
        parse_chat("")


def test_android_layout_and_attachments_parse():
    text = ("11/9/23, 10:16 PM - Messages and calls are end-to-end encrypted.\n"
            "11/9/23, 10:17 PM - Gramps: BP 120/80\n"
            "11/10/23, 9:22 AM - Gramps: IMG-20231110-WA0001.jpg (file attached)\n"
            "11/10/23, 9:23 AM - Gramps: <Media omitted>\n")
    r = parse_chat(text)
    assert [m.text for m in r.messages] == ["BP 120/80", ""]
    assert r.messages[1].attachment == "IMG-20231110-WA0001.jpg" and r.messages[1].has_image
    assert r.messages[0].sent_at == "2023-11-09T22:17:00"


def test_dotted_dates_parse():
    m = parse_chat("21.11.23, 9:00 - A: hi\n").messages[0]
    assert m.sent_at == "2023-11-21T09:00:00"
