import pandas as pd

from health_tracker import trends


def frame(rows):
    base = dict(person=None, systolic=None, diastolic=None, pulse=None, weight=None,
                weight_unit=None, glucose=None, glucose_unit=None, note=None)
    return trends.readings_frame([{**base, **r} for r in rows])


ROWS = [
    {"when": "2026-06-10T19:00", "weight": 146.0, "weight_unit": "lb"},
    {"when": "2026-06-14T18:44", "weight": 145.6, "weight_unit": "lb", "systolic": 108, "diastolic": 75, "pulse": 74},
    {"when": "2026-06-14T18:47", "glucose": 142, "glucose_unit": "mg/dL"},
    {"when": "2026-06-15T19:28", "systolic": 106, "diastolic": 73, "pulse": 72, "glucose": 6.1, "glucose_unit": "mmol/L"},
]


def test_summary_per_measure():
    s = trends.summarize(frame(ROWS))
    assert s["Systolic (mmHg)"]["mean"] == 107 and s["Systolic (mmHg)"]["last"] == 106
    assert s["Weight (lb)"]["first"] == 146.0 and s["Weight (lb)"]["n"] == 2
    assert s["Glucose (mg/dL)"]["n"] == 1        # the mmol/L reading is not silently converted


def test_period_filter_is_relative_to_newest_reading():
    s = trends.summarize(frame(ROWS), days=2)
    assert "Weight (lb)" in s and s["Weight (lb)"]["n"] == 1


def test_unitless_weight_is_left_out_not_guessed():
    w, unit = trends.weights_in(frame([{"when": "2026-06-10T19:00", "weight": 153.6}]))
    assert w.empty and unit is None


def test_mixed_weight_units_convert_to_the_common_one():
    df = frame([{"when": "2026-06-10", "weight": 146.0, "weight_unit": "lb"},
                {"when": "2026-06-11", "weight": 146.0, "weight_unit": "lb"},
                {"when": "2026-06-12", "weight": 66.2, "weight_unit": "kg"}])
    w, unit = trends.weights_in(df)
    assert unit == "lb" and abs(w.iloc[-1] - 145.9) < 0.1


def test_daily_averages_by_day():
    d = trends.daily(frame(ROWS))
    assert len(d) == 3 and d.loc[pd.Timestamp("2026-06-14"), "glucose_mgdl"] == 142


def test_rows_with_unparseable_dates_are_dropped():
    assert len(frame([{"when": "garbage", "pulse": 70}, {"when": "2026-06-10", "pulse": 71}])) == 1


def test_empty_data_is_handled():
    assert trends.summarize(trends.readings_frame([])) == {}
    assert "No confirmed" in trends.doctor_summary_markdown(trends.readings_frame([]))


def test_doctor_summary_has_table_and_no_medical_judgment():
    md = trends.doctor_summary_markdown(frame(ROWS), "Grandpa")
    assert "Grandpa" in md and "| Systolic (mmHg) |" in md and "Last 30 days" in md
    import re
    assert not re.search(r"\b(normal|high|low|dangerous|healthy|elevated)\b", md.lower())


def test_sparse_series_keep_only_real_days_so_the_line_connects():
    df = frame([
        {"when": "2026-06-10T08:00", "systolic": 120, "diastolic": 80},
        {"when": "2026-06-11T08:00", "weight": 146.0, "weight_unit": "lb"},
        {"when": "2026-06-12T08:00", "systolic": 118, "diastolic": 78},
        {"when": "2026-06-13T08:00", "weight": 145.6, "weight_unit": "lb"},
    ])
    long = trends.series_long(trends.daily(df), {"systolic": "Systolic", "diastolic": "Diastolic",
                                                 "pulse": "Pulse", "weight_lb": "Weight"})
    by = long.groupby("series")["value"].apply(list).to_dict()
    assert by == {"Systolic": [120.0, 118.0], "Diastolic": [80.0, 78.0], "Weight": [146.0, 145.6]}
    assert not long["value"].isna().any()          # no NaN rows left to break the line
    assert "Pulse" not in by                       # a measure with no data is left out entirely


def test_series_long_with_nothing_is_empty_not_an_error():
    assert trends.series_long(trends.daily(frame([{"when": "2026-06-10", "pulse": 70}])), {"glucose_mgdl": "Glucose"}).empty
