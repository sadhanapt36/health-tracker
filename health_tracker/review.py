"""Validate and convert the values a person confirms on the review screen.

Pure functions (no Streamlit) so they are easy to test.
"""
from datetime import datetime
from typing import Optional

MEASURED_AT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def fmt(value) -> str:
    """Draft value -> text for a form field. None becomes blank."""
    return "" if value is None else f"{value:g}"


def _whole(text: str, label: str) -> Optional[int]:
    text = (text or "").strip()
    if not text:
        return None
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{label} must be a whole number.")
    return int(text)


def _decimal(text: str, label: str) -> Optional[float]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{label} must be a number.") from None
    if value <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return value


def parse_measured_at(text: str) -> Optional[str]:
    """'2026-10-21 08:10' -> ISO text. Blank means unknown and stays None."""
    text = (text or "").strip()
    if not text:
        return None
    for f in MEASURED_AT_FORMATS:
        try:
            parsed = datetime.strptime(text, f)
            return parsed.date().isoformat() if f == "%Y-%m-%d" else parsed.isoformat(timespec="minutes")
        except ValueError:
            continue
    raise ValueError("Measured at must look like 2026-10-21 08:10 (or just 2026-10-21).")


def build_reading(raw: dict) -> dict:
    """Raw form strings -> a clean reading dict. Raises ValueError with a
    message the person can act on."""
    systolic = _whole(raw.get("systolic"), "Systolic")
    diastolic = _whole(raw.get("diastolic"), "Diastolic")
    if (systolic is None) != (diastolic is None):
        raise ValueError("Enter both systolic and diastolic, or leave both blank.")
    pulse = _whole(raw.get("pulse"), "Pulse")
    weight = _decimal(raw.get("weight"), "Weight")
    glucose = _decimal(raw.get("glucose"), "Glucose")

    if all(v is None for v in (systolic, pulse, weight, glucose)):
        raise ValueError("Enter at least one measurement.")

    return {
        "person": (raw.get("person") or "").strip() or None,
        "measured_at": parse_measured_at(raw.get("measured_at")),
        "systolic": systolic, "diastolic": diastolic, "pulse": pulse,
        "weight": weight, "weight_unit": (raw.get("weight_unit") or None) if weight is not None else None,
        "glucose": glucose, "glucose_unit": (raw.get("glucose_unit") or None) if glucose is not None else None,
        "note": (raw.get("note") or "").strip() or None,
    }


# ---------- bulk confirm ----------
OUTLIER_FRACTION = 0.10   # a weight this far from the typical one is held back for a human look


def bulk_weight_candidates(pending, parse):
    """Split pending text messages into (safe_to_bulk_confirm, needs_a_look).

    Safe means the message is ONLY a weight, with a stated unit, no parser warnings,
    and a value close to the person's typical weight. `pending` is an iterable of
    message rows (need ["text"]); `parse` is parsing.parse_reading. Returns two lists
    of (row, draft). Messages with no weight at all are in neither list.
    """
    from statistics import median
    weights = []
    for row in pending:
        d = parse(row["text"])
        if d.weight is not None:
            weights.append((row, d))
    typical = median(d.weight for _, d in weights) if weights else None
    safe, look = [], []
    for row, d in weights:
        only_weight = all(v is None for v in (d.systolic, d.diastolic, d.pulse, d.glucose))
        ok = (only_weight and d.weight_unit is not None and not d.notes
              and abs(d.weight - typical) <= OUTLIER_FRACTION * typical)
        (safe if ok else look).append((row, d))
    return safe, look
