from pathlib import Path
from types import SimpleNamespace

import pytest

from health_tracker.vision import (ClaudeVisionExtractor, ImageDraft, clean_draft,
                                   local_bp_display, local_libre_glucose, parse_vision_json,
                                   read_photo)

FIX = Path(__file__).parent / "fixtures"
needs_tesseract = pytest.mark.skipif(
    __import__("shutil").which("tesseract") is None, reason="tesseract not installed")


@needs_tesseract
@pytest.mark.parametrize("name,value", [("libre_142.jpg", 142), ("libre_128.jpg", 128)])
def test_local_reader_gets_libre_glucose(name, value):
    d = local_libre_glucose((FIX / name).read_bytes())
    assert d.kind == "glucose" and d.glucose == value and d.glucose_unit == "mg/dL"
    assert d.confident and d.reader == "local OCR"


BP_PHOTOS = [("bp_105_73_74.jpg", (105, 73, 74)), ("bp_108_75_74.jpg", (108, 75, 74)),
             ("bp_101_69_76.jpg", (101, 69, 76))]


@needs_tesseract
def test_libre_reader_ignores_bp_photos():
    assert local_libre_glucose((FIX / "bp_108_75_74.jpg").read_bytes()) is None


@pytest.mark.parametrize("name,expected", BP_PHOTOS)
def test_local_bp_reader_reads_the_monitor_display(name, expected):
    d = local_bp_display((FIX / name).read_bytes())
    assert (d.systolic, d.diastolic, d.pulse) == expected
    assert d.kind == "blood_pressure" and d.reader == "local display reader"


@pytest.mark.parametrize("name,expected", BP_PHOTOS)
def test_read_photo_needs_no_cloud_for_bp(name, expected):
    d = read_photo((FIX / name).read_bytes())          # cloud=None
    assert (d.systolic, d.diastolic, d.pulse) == expected


@needs_tesseract
@pytest.mark.parametrize("name", ["libre_142.jpg", "libre_128.jpg"])
def test_bp_reader_declines_glucose_screenshots(name):
    assert local_bp_display((FIX / name).read_bytes()) is None


def test_bp_reader_declines_junk():
    import cv2, numpy as np
    noise = np.random.default_rng(0).integers(0, 255, (800, 600), dtype=np.uint8)
    for img in (noise, np.full((800, 600), 200, np.uint8)):
        assert local_bp_display(cv2.imencode(".jpg", img)[1].tobytes()) is None
    assert local_bp_display(b"not an image") is None


def test_bp_reader_never_returns_a_wrong_value_on_degraded_photos():
    """Blur, dimming, rotation, tiny size, heavy compression: the right numbers or nothing."""
    import cv2, numpy as np
    def rot(im, a):
        h, w = im.shape[:2]
        return cv2.warpAffine(im, cv2.getRotationMatrix2D((w / 2, h / 2), a, 1), (w, h),
                              borderValue=int(np.median(im)))
    variants = [lambda i: cv2.resize(i, None, fx=.4, fy=.4, interpolation=cv2.INTER_AREA),
                lambda i: rot(i, 4), lambda i: (i * .55).astype(np.uint8),
                lambda i: cv2.GaussianBlur(i, (0, 0), 6), lambda i: i[:1200]]
    for name, expected in BP_PHOTOS:
        base = cv2.imread(str(FIX / name), 0)
        for v in variants:
            got = local_bp_display(cv2.imencode(".jpg", v(base), [cv2.IMWRITE_JPEG_QUALITY, 40])[1].tobytes())
            assert got is None or (got.systolic, got.diastolic, got.pulse) == expected


def test_parses_clean_bp_reply():
    d = parse_vision_json('{"kind":"blood_pressure","systolic":108,"diastolic":75,"pulse":74,'
                          '"confident":true}', reader="m")
    assert (d.kind, d.systolic, d.diastolic, d.pulse, d.confident) == ("blood_pressure", 108, 75, 74, True)


def test_tolerates_code_fences_and_chatter():
    d = parse_vision_json('Sure!\n```json\n{"kind":"glucose","glucose":128,"glucose_unit":"mg/dL",'
                          '"confident":true}\n```')
    assert d.glucose == 128 and d.glucose_unit == "mg/dL"


@pytest.mark.parametrize("bad", ["", "no json here", "{broken", "[1,2]"])
def test_garbage_reply_never_raises(bad):
    d = parse_vision_json(bad)
    assert not d.has_values() and d.notes


def test_implausible_or_inverted_bp_is_dropped_and_explained():
    d = parse_vision_json('{"kind":"blood_pressure","systolic":75,"diastolic":108,"pulse":74}')
    assert d.systolic is None and d.diastolic is None and d.pulse == 74
    assert any("implausible" in n for n in d.notes)


def test_one_sided_bp_is_dropped():
    d = clean_draft(ImageDraft(kind="blood_pressure", systolic=120))
    assert d.systolic is None and d.diastolic is None


def test_unknown_units_become_none_and_not_confident_by_default():
    d = parse_vision_json('{"kind":"glucose","glucose":110,"glucose_unit":"mg"}')
    assert d.glucose == 110 and d.glucose_unit is None and not d.confident


def test_not_confident_is_called_out_for_the_reviewer():
    d = parse_vision_json('{"kind":"blood_pressure","systolic":120,"diastolic":80,"confident":false}',
                          reader="m")
    notes = d.to_draft().notes
    assert any("not confident" in n for n in notes) and any("Compare every number" in n for n in notes)


def test_other_photos_have_no_values():
    d = parse_vision_json('{"kind":"other","systolic":null,"confident":true}')
    assert d.kind == "other" and not d.has_values()


def test_cloud_extractor_sends_one_image_and_parses_reply():
    calls = []

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                calls.append(kw)
                return SimpleNamespace(content=[SimpleNamespace(
                    type="text", text='{"kind":"blood_pressure","systolic":101,"diastolic":69,'
                                      '"pulse":76,"confident":true}')])

    d = ClaudeVisionExtractor(model="test-model", client=FakeClient()).extract(b"\xff\xd8x")
    assert (d.systolic, d.diastolic, d.pulse, d.reader) == (101, 69, 76, "test-model")
    content = calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
