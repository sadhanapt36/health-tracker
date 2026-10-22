"""Turn message text into a DRAFT reading.

Rules of thumb:
  * Only fill a field when the pattern is clear. Otherwise leave it blank and,
    if useful, add a plain-language note for the reviewer.
  * Units are kept only if the message states them. Weight "153.6" with no
    unit stays unit-less; we do not guess kg vs lb.
  * Plausibility ranges below only catch typing mistakes (15.02 lb, 600 kg).
    They are not medical thresholds, and nothing here judges a value as
    good or bad.
"""
import re

from .models import DraftReading

_SEP = r"[-:=\u2013\u2014]?"                       # dash, colon, equals, en/em dash
_NUM = r"(?P<num>\d{1,3}(?:\.\d{1,2})?)"
_LB = r"l\.?b\.?s?|pounds?"                        # lb, lbs, L.B, L.B., pounds
_KG = r"kgs?|kilos?|kilograms?"
_END = r"(?![\d/:.])"                               # number must not run into more digits

# --- weight: three ways people write it ---
_W_UNIT_FIRST = re.compile(                         # "L.B-151.4", "LB149.4", "L.B-weight-151.4"
    rf"(?<![a-z])(?P<unit>{_LB})(?![a-z])\s*{_SEP}\s*(?:(?:weight|wt)\s*{_SEP}\s*)?{_NUM}{_END}", re.I)
_W_LABEL = re.compile(                              # "Weight 153.6", "Weight -151.02 L.B", "Weight-L.B-150"
    rf"\b(?:weight|wt)\b\s*(?:is|was)?\s*{_SEP}\s*(?:(?P<unit0>{_LB}|{_KG})(?![a-z])\s*{_SEP}\s*)?"
    rf"{_NUM}{_END}(?:\s*\(?\s*(?P<unit1>{_LB}|{_KG})(?![a-z]))?", re.I)
_W_NUMBER_UNIT = re.compile(                        # "74.2 kg", "151.4 weight (L.B)", "149.9(L.B)"
    rf"(?<![\d.]){_NUM}\s*(?:(?:weight|wt)\s*)?\(?\s*(?P<unit>{_LB}|{_KG})(?![a-z])", re.I)
_W_BROKEN = re.compile(                             # "L.B-151.oo": letters where digits belong
    rf"(?<![a-z])(?:{_LB})(?![a-z])\s*{_SEP}\s*\d{{2,3}}\.[a-z]", re.I)
_MONEY = re.compile(r"\s*(?:rs|rd|inr|usd|\u20b9|\$)", re.I)
_WEIGHT_RANGE = {"kg": (20, 250), "lb": (50, 600), None: (20, 600)}

# --- blood pressure and pulse ---
# Not part of a date: "06/10/2025" has a "/digit" after the pair or before it.
_BP = re.compile(r"(?<![\d/.])(\d{2,3})\s*/\s*(\d{2,3})(?![\d.]|/\d)")
_PULSE_LABEL = re.compile(
    r"\b(?:pulse|heart\s*rate|hr)\b\s*(?:is|was|rate)?\s*[-:=]?\s*(?P<num>\d{2,3})(?![\d/:.])", re.I)
_PULSE_BPM = re.compile(r"(?<![\d./])(?P<num>\d{2,3})\s*bpm\b", re.I)

# --- glucose ---
_G_LABEL = re.compile(
    r"\b(?:blood\s+sugar|sugar|glucose|fbs|ppbs|rbs)\b\s*(?:level|is|was)?\s*[-:=]?\s*"
    r"(?P<num>\d{1,3}(?:\.\d{1,2})?)(?![\d/:])(?:\s*(?P<unit>mg\s*/\s*dl|mmol\s*/\s*l)(?![a-z]))?", re.I)
_G_UNIT = re.compile(
    r"(?<![\d.])(?P<num>\d{1,3}(?:\.\d{1,2})?)\s*(?P<unit>mg\s*/\s*dl|mmol\s*/\s*l)(?![a-z])", re.I)
_GLUCOSE_RANGE = {"mg/dL": (20, 600), "mmol/L": (1, 35), None: (1, 600)}


def _normalize_numbers(text: str):
    """Repair obvious punctuation slips in numbers. Returns (text, changed)."""
    fixed = re.sub(r"(?<=\d)[\u2019'`](?=\d)", ".", text)      # 150'02 -> 150.02
    fixed = re.sub(r"(?<=\d)\.{2,}(?=\d)", ".", fixed)           # 150..02 -> 150.02
    fixed = re.sub(r"(?<=\d)\.\s+(?=\d)", ".", fixed)            # 150. 02 -> 150.02
    return fixed, fixed != text


def _weight_unit(token):
    if not token:
        return None
    return "lb" if re.match(r"l|pound", token, re.I) else "kg"


def _glucose_unit(token):
    if not token:
        return None
    return "mg/dL" if "mg" in token.lower() else "mmol/L"


def _find_weight(text: str, notes: list):
    implausible = []
    for pattern in (_W_UNIT_FIRST, _W_LABEL, _W_NUMBER_UNIT):
        for m in pattern.finditer(text):
            if _MONEY.match(text, m.end()):          # "Kg-600Rs" is a price
                continue
            groups = m.groupdict()
            unit = _weight_unit(groups.get("unit") or groups.get("unit0") or groups.get("unit1"))
            value = float(m["num"])
            lo, hi = _WEIGHT_RANGE[unit]
            if lo <= value <= hi:
                if unit is None:
                    notes.append("Weight has no unit in the message. Choose kg or lb.")
                return value, unit
            implausible.append(m["num"])
    if implausible:
        notes.append(f"Weight {implausible[0]} looks like a typing slip, so it was left blank.")
    elif _W_BROKEN.search(text):
        notes.append("A weight has letters where digits should be, so it was left blank.")
    return None, None


def _find_glucose(text: str, notes: list):
    for pattern in (_G_LABEL, _G_UNIT):
        for m in pattern.finditer(text):
            unit = _glucose_unit(m.groupdict().get("unit"))
            value = float(m["num"])
            lo, hi = _GLUCOSE_RANGE[unit]
            if lo <= value <= hi:
                if unit is None:
                    notes.append("Glucose has no unit in the message. Choose mg/dL or mmol/L.")
                return value, unit
    return None, None


def parse_reading(text: str) -> DraftReading:
    draft = DraftReading()
    text, repaired = _normalize_numbers(text or "")

    # Blood pressure: the first plausible "sys/dia" pair.
    pairs = [(m, int(m[1]), int(m[2])) for m in _BP.finditer(text)]
    good = [(m, s, d) for m, s, d in pairs if 70 <= s <= 260 and 40 <= d <= 150 and s > d]
    bp_end = None
    if good:
        m, draft.systolic, draft.diastolic = good[0]
        bp_end = m.end()
        if len(good) > 1:
            draft.notes.append(f"{len(good)} blood pressure readings are in this message; "
                               "only the first is shown. Add the others with Manual entry.")

    # Pulse: only when labelled (or "bpm").
    for pattern in (_PULSE_LABEL, _PULSE_BPM):
        m = pattern.search(text)
        if m and 30 <= int(m["num"]) <= 220:
            draft.pulse = int(m["num"])
            break
    if draft.pulse is None and bp_end is not None:
        extra = re.match(r"\s*(?:/+|,)?\s*(\d{2,3})(?![\d/:.])", text[bp_end:])
        if extra:
            draft.notes.append(f"There is an unlabeled number ({extra[1]}) right after the "
                               "blood pressure. It was not used as pulse.")

    draft.weight, draft.weight_unit = _find_weight(text, draft.notes)
    draft.glucose, draft.glucose_unit = _find_glucose(text, draft.notes)

    if repaired and (draft.weight is not None or draft.glucose is not None):
        draft.notes.append("A number was typed with unusual punctuation. Check the decimal point.")
    return draft
