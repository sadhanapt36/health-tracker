"""Read measurements out of photos.

Three readers, same output (ImageDraft). The first two run on this computer, free:
  * local_libre_glucose: FreeStyle Libre screenshots (Tesseract).
  * local_bp_display: Omron-style blood pressure monitor LCDs (OpenCV, lcd7seg.py).
  * ClaudeVisionExtractor: OPTIONAL fallback for photos the local readers decline.
    It sends the photo to the Anthropic API (paid), so it only runs when turned on.

Whatever the reader says is only a DRAFT. A person confirms it on the Review
screen, and values outside plausible ranges are dropped, not trusted.
"""
import base64
import io
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .models import DraftReading

DEFAULT_MODEL = os.environ.get("HEALTH_TRACKER_VISION_MODEL", "claude-haiku-5-5")

KINDS = ("blood_pressure", "glucose", "weight", "other")


@dataclass
class ImageDraft:
    kind: str = "other"                       # one of KINDS
    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    pulse: Optional[int] = None
    glucose: Optional[float] = None
    glucose_unit: Optional[str] = None
    weight: Optional[float] = None
    weight_unit: Optional[str] = None
    confident: bool = False                   # the reader's own "I could read it clearly"
    reader: str = ""                          # 'local-ocr' | model name
    notes: list = field(default_factory=list)

    def has_values(self) -> bool:
        return any(v is not None for v in (self.systolic, self.diastolic, self.pulse,
                                           self.glucose, self.weight))

    def to_draft(self) -> DraftReading:
        notes = list(self.notes)
        if self.has_values():
            notes.insert(0, f"Read from the photo by {self.reader or 'the image reader'}. "
                            "Compare every number with the photo before confirming.")
            if not self.confident:
                notes.append("The reader was not confident about this photo.")
        return DraftReading(self.systolic, self.diastolic, self.pulse, self.weight,
                            self.weight_unit, self.glucose, self.glucose_unit, notes)


# ---------------------------------------------------------------- validation
def _int_in(v, lo, hi) -> Optional[int]:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _num_in(v, lo, hi) -> Optional[float]:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def clean_draft(d: ImageDraft) -> ImageDraft:
    """Drop anything implausible (a mis-read digit), noting what was dropped.
    These ranges catch misreads; they are not medical thresholds."""
    s, di = _int_in(d.systolic, 70, 260), _int_in(d.diastolic, 40, 150)
    if d.systolic is not None and (s is None or di is None or s <= di):
        d.notes.append("Blood pressure numbers looked implausible, so they were left blank.")
        s = di = None
    elif d.diastolic is not None and di is None:
        d.notes.append("Blood pressure numbers looked implausible, so they were left blank.")
        s = di = None
    if (s is None) != (di is None):
        s = di = None
    d.systolic, d.diastolic = s, di
    if d.pulse is not None and _int_in(d.pulse, 30, 220) is None:
        d.notes.append(f"Pulse {d.pulse} looked implausible, so it was left blank.")
    d.pulse = _int_in(d.pulse, 30, 220)
    unit = d.glucose_unit if d.glucose_unit in ("mg/dL", "mmol/L") else None
    lo, hi = {"mg/dL": (20, 600), "mmol/L": (1, 35)}.get(unit, (1, 600))
    if d.glucose is not None and _num_in(d.glucose, lo, hi) is None:
        d.notes.append(f"Glucose {d.glucose} looked implausible, so it was left blank.")
    d.glucose = _num_in(d.glucose, lo, hi)
    d.glucose_unit = unit if d.glucose is not None else None
    wunit = d.weight_unit if d.weight_unit in ("kg", "lb") else None
    d.weight = _num_in(d.weight, 20, 600)
    d.weight_unit = wunit if d.weight is not None else None
    if d.kind not in KINDS:
        d.kind = "other"
    return d


# ------------------------------------------------------------ local (Libre)
def _ocr_digits(img, box, scale=3) -> Optional[int]:
    import pytesseract
    from PIL import ImageOps
    w, h = img.size
    l, t, r, b = (int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
    crop = ImageOps.autocontrast(img.crop((l, t, r, b)).convert("L"))
    crop = crop.resize((crop.width * scale, crop.height * scale))
    text = pytesseract.image_to_string(
        crop, config="--psm 7 -c tessedit_char_whitelist=0123456789").strip()
    return int(text) if text.isdigit() else None


def local_libre_glucose(data: bytes) -> Optional[ImageDraft]:
    """Return a draft if this is a FreeStyle Libre 3 app screenshot, else None.
    Needs the `tesseract` program and `pytesseract`; returns None if missing."""
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        text = pytesseract.image_to_string(img.convert("L"), config="--psm 6").lower()
    except Exception:
        return None
    if "freestyle" not in text and "libre" not in text:
        return None
    d = ImageDraft(kind="glucose", reader="local OCR", glucose_unit="mg/dL" if "mg/dl" in text else None)
    # The big reading sits in the green banner, in the upper part of the screenshot.
    value = _ocr_digits(img, (0.10, 0.20, 0.68, 0.34))
    if value is None:
        d.notes.append("This is a Libre screenshot but the number could not be read.")
        return d
    d.glucose = float(value)
    d.confident = True
    if d.glucose_unit is None:
        d.notes.append("Glucose unit not detected. Choose mg/dL or mmol/L.")
    return clean_draft(d)


# --------------------------------------------------- local (blood pressure)
class BpReaderUnavailable(RuntimeError):
    """The local blood pressure reader can't run on this computer (OpenCV not installed)."""


def bp_reader_available() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


def local_bp_display(data: bytes) -> Optional[ImageDraft]:
    """Return a draft if a blood pressure monitor display can be read, else None.
    Strict by design: it declines rather than guess."""
    try:
        from .lcd7seg import read_bp_display
    except ImportError:                       # OpenCV missing: say so, don't fail silently
        raise BpReaderUnavailable(
            "The blood pressure reader needs OpenCV. Run: pip install -r requirements.txt")
    try:
        vals = read_bp_display(data)
    except Exception:
        return None
    if not vals:
        return None
    d = ImageDraft(kind="blood_pressure", reader="local display reader", confident=True, **vals)
    clean = clean_draft(d)
    return clean if clean.has_values() else None


# ------------------------------------------------------------ cloud (Claude)
PROMPT = """You are reading a photo that a family member sent in a chat. It may show a
home blood pressure monitor, a glucose app screenshot, a scale, or something unrelated.

Return ONLY a JSON object, no other text:
{"kind": "blood_pressure" | "glucose" | "weight" | "other",
 "systolic": int|null, "diastolic": int|null, "pulse": int|null,
 "glucose": number|null, "glucose_unit": "mg/dL"|"mmol/L"|null,
 "weight": number|null, "weight_unit": "kg"|"lb"|null,
 "confident": true|false}

Rules:
- Copy only digits you can clearly see on the display. Use null for anything blurry,
  cut off, or not shown. Never guess or estimate. Do not interpret the numbers.
- On a blood pressure monitor: SYS is systolic, DIA is diastolic, PULSE is pulse.
- Use "other" with all values null for anything that is not a health reading.
- "confident" is false if any digit was hard to read."""


def parse_vision_json(text: str, reader: str = "") -> ImageDraft:
    """Model reply -> validated ImageDraft. Never raises on bad model output."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        raw = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict) or not raw:
        return ImageDraft(reader=reader, notes=["The image reader's answer could not be understood."])
    d = ImageDraft(
        kind=raw.get("kind") if raw.get("kind") in KINDS else "other",
        systolic=raw.get("systolic"), diastolic=raw.get("diastolic"), pulse=raw.get("pulse"),
        glucose=raw.get("glucose"), glucose_unit=raw.get("glucose_unit"),
        weight=raw.get("weight"), weight_unit=raw.get("weight_unit"),
        confident=raw.get("confident") is True, reader=reader)
    return clean_draft(d)


class ClaudeVisionExtractor:
    """Sends ONE photo per call to the Anthropic API. Needs ANTHROPIC_API_KEY."""

    def __init__(self, model: str = DEFAULT_MODEL, client=None):
        self.model = model
        if client is None:
            import anthropic                      # imported late: optional dependency
            client = anthropic.Anthropic()        # reads ANTHROPIC_API_KEY
        self.client = client

    def extract(self, data: bytes, media_type: str = "image/jpeg") -> ImageDraft:
        reply = self.client.messages.create(
            model=self.model, max_tokens=300,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.b64encode(data).decode()}},
                {"type": "text", "text": PROMPT}]}])
        text = "".join(b.text for b in reply.content if getattr(b, "type", "") == "text")
        return parse_vision_json(text, reader=self.model)


def read_photo(data: bytes, media_type: str = "image/jpeg", cloud: Optional[ClaudeVisionExtractor] = None
               ) -> Optional[ImageDraft]:
    """Local readers first (free, private). Cloud only if provided. None = nothing could be done."""
    local = local_libre_glucose(data)
    if local is not None and local.has_values():
        return local
    bp = local_bp_display(data)
    if bp is not None:
        return bp
    if cloud is not None:
        return cloud.extract(data, media_type)
    return local
