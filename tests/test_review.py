import pytest

from health_tracker.review import build_reading, fmt, parse_measured_at


def raw(**kw):
    base = dict(person="", measured_at="", systolic="", diastolic="", pulse="",
                weight="", weight_unit="", glucose="", glucose_unit="", note="")
    return {**base, **kw}


def test_blank_fields_stay_none_and_units_survive():
    r = build_reading(raw(systolic="128", diastolic="82", weight="163.2", weight_unit="lb"))
    assert r["systolic"] == 128 and r["pulse"] is None and r["glucose"] is None
    assert r["weight"] == 163.2 and r["weight_unit"] == "lb" and r["measured_at"] is None


def test_unit_left_blank_stays_blank():
    r = build_reading(raw(weight="153.6"))
    assert r["weight_unit"] is None


def test_unit_without_a_value_is_dropped():
    r = build_reading(raw(systolic="120", diastolic="80", weight_unit="kg"))
    assert r["weight"] is None and r["weight_unit"] is None


def test_bp_needs_both_numbers():
    with pytest.raises(ValueError, match="both"):
        build_reading(raw(systolic="120"))


def test_needs_at_least_one_measurement():
    with pytest.raises(ValueError, match="at least one"):
        build_reading(raw(person="Grandpa"))


@pytest.mark.parametrize("field,value", [("pulse", "7l"), ("systolic", "12.5"),
                                         ("weight", "abc"), ("glucose", "-5")])
def test_rejects_non_numbers(field, value):
    kw = {field: value}
    if field == "systolic":
        kw["diastolic"] = "80"
    with pytest.raises(ValueError):
        build_reading(raw(**kw))


def test_measured_at_formats():
    assert parse_measured_at("") is None
    assert parse_measured_at("2026-10-21 08:10") == "2026-10-21T08:10"
    assert parse_measured_at("2026-10-21") == "2026-10-21"
    with pytest.raises(ValueError):
        parse_measured_at("yesterday morning")


def test_person_and_note_are_trimmed():
    r = build_reading(raw(person="  Grandpa ", note=" after walk ", pulse="70"))
    assert r["person"] == "Grandpa" and r["note"] == "after walk"


def test_fmt():
    assert fmt(None) == "" and fmt(128) == "128" and fmt(150.08) == "150.08" and fmt(74.0) == "74"


# ---------- bulk confirm ----------
from health_tracker.parsing import parse_reading
from health_tracker.review import bulk_weight_candidates


def rows(*texts):
    return [{"text": t, "id": i} for i, t in enumerate(texts)]


def test_clean_weights_are_safe_to_bulk_confirm():
    safe, look = bulk_weight_candidates(rows("L.B-146.00", "LB149.4", "Weight-150.04(L.B)"), parse_reading)
    assert len(safe) == 3 and look == []


def test_unitless_weight_needs_a_look():
    safe, look = bulk_weight_candidates(rows("L.B-146", "L.B-147", "Weight 153.6"), parse_reading)
    assert len(safe) == 2 and len(look) == 1 and look[0][1].weight == 153.6


def test_punctuation_slip_needs_a_look():
    safe, look = bulk_weight_candidates(rows("L.B-150.02", "L.B-150.04", "LB-150..02"), parse_reading)
    assert len(safe) == 2 and len(look) == 1


def test_outlier_weight_is_held_back():
    safe, look = bulk_weight_candidates(
        rows("L.B-146", "L.B-147", "L.B-145", "L.B-146.5", "L.B-196"), parse_reading)
    assert len(safe) == 4 and look[0][1].weight == 196


def test_messages_with_bp_or_glucose_are_never_bulk_confirmed():
    safe, look = bulk_weight_candidates(
        rows("L.B-146", "L.B-146 BP 120/80", "L.B-146 Sugar 104 mg/dL"), parse_reading)
    assert len(safe) == 1 and len(look) == 2


def test_non_weight_messages_are_ignored():
    safe, look = bulk_weight_candidates(rows("hello", "BP 120/80", "Kg-600Rs"), parse_reading)
    assert safe == [] and look == []
