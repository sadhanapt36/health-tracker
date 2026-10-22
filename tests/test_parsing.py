import pytest

from health_tracker.parsing import parse_reading
from health_tracker.sample_import import load_sample


def values(text):
    d = parse_reading(text)
    return (d.systolic, d.diastolic, d.pulse, d.weight, d.weight_unit, d.glucose, d.glucose_unit)


# (message, systolic, diastolic, pulse, weight, weight_unit, glucose, glucose_unit)
CASES = [
    ("BP 128/82 pulse 71",            128, 82, 71, None, None, None, None),
    ("130/85 evening",                130, 85, None, None, None, None, None),
    ("Sugar-104 mg/dL fasting",       None, None, None, None, None, 104, "mg/dL"),
    ("Sugar 6.1 mmol/L",              None, None, None, None, None, 6.1, "mmol/L"),
    ("Sugar is -106",                 None, None, None, None, None, 106, None),
    ("L.B-163.2",                     None, None, None, 163.2, "lb", None, None),
    ("LB149.4",                       None, None, None, 149.4, "lb", None, None),
    ("L.B.-150.06",                   None, None, None, 150.06, "lb", None, None),
    ("L.B—151.00",                    None, None, None, 151.0, "lb", None, None),
    ("L.B-weight-150.02",             None, None, None, 150.02, "lb", None, None),
    ("Weight-L.B-150.02",             None, None, None, 150.02, "lb", None, None),
    ("Weight -74.2 kg",               None, None, None, 74.2, "kg", None, None),
    ("Weight -151.02 L.B",            None, None, None, 151.02, "lb", None, None),
    ("151.4 weight (L.B)",            None, None, None, 151.4, "lb", None, None),
    ("150.08 weight(L.B)",            None, None, None, 150.08, "lb", None, None),
    ("149.9(L.B)",                    None, None, None, 149.9, "lb", None, None),
    ("Weight-150.04(L.B)",            None, None, None, 150.04, "lb", None, None),
    ("Weight 153.6",                  None, None, None, 153.6, None, None, None),   # unit not stated
    ("LB-150..02",                    None, None, None, 150.02, "lb", None, None),  # punctuation slip
    ("L.B-150. 02",                   None, None, None, 150.02, "lb", None, None),
    ("L.B-150\u201902",               None, None, None, 150.02, "lb", None, None),
    ("120/80 pulse 72 weight 70 kg sugar 100 mg/dL", 120, 80, 72, 70.0, "kg", 100.0, "mg/dL"),
]

# Things that must NOT become readings.
NOT_READINGS = [
    "bp was fine today",
    "Kg-600Rs", "Kg-600Rd",
    "Raju garu me ki 06/10/2025 clear chastar",
    "Door no-9-1234/5678",
    "Due On-28/04/2026 Amount-7280.0",
    "LIC Policy 804425157",
    "Call me at 10:30",
    "L.B-15.02",        # typing slip: 15 lb
    "L.B-151.oo",       # letters where digits belong
    "BP 12/8",
    "",
]


@pytest.mark.parametrize("text,sys,dia,pulse,w,wu,g,gu", CASES)
def test_known_formats(text, sys, dia, pulse, w, wu, g, gu):
    assert values(text) == (sys, dia, pulse, w, wu, g, gu)


@pytest.mark.parametrize("text", NOT_READINGS)
def test_not_readings(text):
    assert not parse_reading(text).has_values()


def test_unlabeled_number_after_bp_is_noted_not_used():
    d = parse_reading("Mommy b.p.  155/85// 76")
    assert (d.systolic, d.diastolic, d.pulse) == (155, 85, None)
    assert any("76" in n for n in d.notes)


def test_missing_units_are_noted():
    assert any("no unit" in n for n in parse_reading("Weight 153.6").notes)
    assert any("no unit" in n for n in parse_reading("Sugar-132").notes)


def test_implausible_weight_explains_itself():
    d = parse_reading("L.B-15.02")
    assert d.weight is None and any("typing slip" in n for n in d.notes)


def test_unusual_punctuation_is_flagged_for_checking():
    assert any("punctuation" in n for n in parse_reading("LB-150..02").notes)


def test_multiple_bp_readings_only_first_used_and_noted():
    d = parse_reading("May 4 149 114/77 86\nMay 6 121 117/78 75 151.4")
    assert (d.systolic, d.diastolic) == (114, 77)
    assert any("2 blood pressure" in n for n in d.notes)


def test_multiline_message():
    d = parse_reading("Today's numbers\nBP 118/76\nPulse 68")
    assert (d.systolic, d.diastolic, d.pulse) == (118, 76, 68)


def test_sample_export_yields_expected_drafts():
    msgs = load_sample().messages
    drafts = [(m.text, parse_reading(m.text)) for m in msgs if m.text]
    with_values = [t for t, d in drafts if d.has_values()]
    without = [t for t, d in drafts if not d.has_values()]
    assert len(with_values) == 11
    assert sorted(without) == ["Kg-600Rs", "bp was fine today"]
