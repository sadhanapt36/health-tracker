import zipfile
from types import SimpleNamespace

from health_tracker import db
from health_tracker.chat_export import parse_chat
from health_tracker.media import MediaStore
from health_tracker.scan import scan_photos
from health_tracker.vision import ImageDraft

CHAT = ("[6/14/26, 6:44:22 PM] Grandpa: L.B-145.60\n"
        "[6/14/26, 6:47:38 PM] Grandpa: <attached: 00000001-PHOTO-2026-06-14-18-47-38.jpg>\n"
        "[6/14/26, 6:48:00 PM] Grandpa: <attached: 00000002-PHOTO-2026-06-14-18-48-00.jpg>\n"
        "[6/14/26, 6:49:00 PM] Mom: <attached: 00000003-PHOTO-2026-06-14-18-49-00.jpg>\n")


def make(tmp_path, with_files=("00000001-PHOTO-2026-06-14-18-47-38.jpg",
                               "00000003-PHOTO-2026-06-14-18-49-00.jpg")):
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Chat/_chat.txt", CHAT)
        f.writestr("__MACOSX/Chat/._x.jpg", b"junk")
        for n in with_files:
            f.writestr(f"Chat/{n}", b"\xff\xd8fake")
    conn = db.connect(tmp_path / "t.db")
    db.insert_messages(conn, "export", parse_chat(CHAT).messages)
    return conn, MediaStore(z)


class FakeCloud:
    def __init__(self, draft=None, fail=False):
        self.draft, self.fail, self.calls = draft, fail, 0

    def extract(self, data, media_type="image/jpeg"):
        self.calls += 1
        if self.fail:
            raise RuntimeError("401")
        return self.draft


BP = ImageDraft(kind="blood_pressure", systolic=108, diastolic=75, pulse=74, confident=True, reader="fake")


def test_media_store_finds_by_name_and_skips_macos_junk(tmp_path):
    _, media = make(tmp_path)
    assert len(media) == 2 and media.has("00000001-PHOTO-2026-06-14-18-47-38.jpg")
    assert not media.has("00000002-PHOTO-2026-06-14-18-48-00.jpg")
    assert media.read("nope.jpg") is None


def test_media_store_from_folder(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "A-PHOTO.jpg").write_bytes(b"x")
    assert MediaStore(tmp_path).read("A-PHOTO.jpg") == b"x"


def test_sender_filter_and_missing_files(tmp_path):
    conn, media = make(tmp_path)
    assert len(db.photos_to_scan(conn, "export")) == 3
    mine = db.photos_to_scan(conn, "export", senders=["Grandpa"])
    assert len(mine) == 2
    res = scan_photos(conn, "export", media, mine, FakeCloud(BP))
    assert (res.scanned, res.with_values, res.missing_file) == (1, 1, 1)


def test_scanned_photos_are_never_sent_twice(tmp_path):
    conn, media = make(tmp_path)
    cloud = FakeCloud(BP)
    scan_photos(conn, "export", media, db.photos_to_scan(conn, "export"), cloud)
    first = cloud.calls
    again = scan_photos(conn, "export", media, db.photos_to_scan(conn, "export"), cloud)
    assert cloud.calls == first and again.scanned == 0


def test_without_cloud_unreadable_photos_are_left_for_later(tmp_path):
    conn, media = make(tmp_path)
    res = scan_photos(conn, "export", media, db.photos_to_scan(conn, "export"), cloud=None)
    assert res.not_read == 2 and res.scanned == 0
    assert len(db.photos_to_scan(conn, "export")) == 3     # still queued


def test_repeated_errors_stop_the_scan_and_store_nothing(tmp_path):
    conn, media = make(tmp_path)
    res = scan_photos(conn, "export", media, db.photos_to_scan(conn, "export"),
                      FakeCloud(fail=True), max_consecutive_errors=2)
    assert res.stopped_early and res.errors == 2 and db.pending_photo_readings(conn, "export") == []


def test_review_queue_has_values_and_hides_other_photos(tmp_path):
    conn, media = make(tmp_path)
    rows = db.photos_to_scan(conn, "export")
    scan_photos(conn, "export", media, rows[:1], FakeCloud(BP))
    scan_photos(conn, "export", media, rows[2:], FakeCloud(ImageDraft(kind="other", reader="fake")))
    queue = db.pending_photo_readings(conn, "export")
    assert len(queue) == 1 and queue[0][1]["systolic"] == 108


def test_confirming_a_photo_reading_removes_it_from_the_queue(tmp_path):
    conn, media = make(tmp_path)
    scan_photos(conn, "export", media, db.photos_to_scan(conn, "export")[:1], FakeCloud(BP))
    row, ex = db.pending_photo_readings(conn, "export")[0]
    reading = dict(person=None, measured_at=None, systolic=ex["systolic"], diastolic=ex["diastolic"],
                   pulse=ex["pulse"], weight=None, weight_unit=None, glucose=None,
                   glucose_unit=None, note=None)
    assert db.add_reading(conn, row["id"], reading)
    assert db.pending_photo_readings(conn, "export") == []
    assert db.list_readings(conn)[0]["when"] == "2026-06-14T18:47:38"   # message time as fallback


def test_reset_clears_scans(tmp_path):
    conn, media = make(tmp_path)
    scan_photos(conn, "export", media, db.photos_to_scan(conn, "export")[:1], FakeCloud(BP))
    db.clear_messages(conn)
    assert conn.execute("SELECT COUNT(*) FROM image_extractions").fetchone()[0] == 0


def test_missing_opencv_is_reported_not_swallowed(tmp_path, monkeypatch):
    import builtins, pytest
    from health_tracker.vision import BpReaderUnavailable
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda n, *a, **k: (_ for _ in ()).throw(ImportError()) if n.endswith("lcd7seg") else real(n, *a, **k))
    conn, media = make(tmp_path)
    with pytest.raises(BpReaderUnavailable):
        scan_photos(conn, "export", media, db.photos_to_scan(conn, "export"), cloud=None)
