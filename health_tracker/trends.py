"""Summaries for a doctor visit. Descriptive only: averages, ranges, change.

Nothing here says a value is good or bad. Those judgments are the doctor's.
"""
from typing import Optional

import pandas as pd

KG_PER_LB = 0.45359237


def readings_frame(rows) -> pd.DataFrame:
    """Confirmed readings (db.list_readings rows) -> DataFrame with a real datetime 'when'."""
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["when"] = pd.to_datetime(df["when"], errors="coerce", format="ISO8601")
    df = df.dropna(subset=["when"]).sort_values("when").reset_index(drop=True)
    return df


def weights_in(df: pd.DataFrame, unit: Optional[str] = None) -> tuple[pd.Series, Optional[str]]:
    """Weights as one series in a single unit (default: the most common stated unit).
    Readings with no stated unit are left out rather than guessed."""
    w = df.dropna(subset=["weight", "weight_unit"])
    if w.empty:
        return pd.Series(dtype=float), None
    unit = unit or w["weight_unit"].mode().iloc[0]
    vals = w["weight"].where(w["weight_unit"] == unit,
                             w["weight"] * (KG_PER_LB if unit == "kg" else 1 / KG_PER_LB))
    return pd.Series(vals.values, index=w["when"].values, name=f"weight ({unit})"), unit


def glucose_in(df: pd.DataFrame) -> tuple[pd.Series, Optional[str]]:
    """Glucose in mg/dL only (mmol/L readings are left out, not converted silently)."""
    g = df[(df["glucose_unit"] == "mg/dL") & df["glucose"].notna()]
    return pd.Series(g["glucose"].values, index=g["when"].values, name="glucose (mg/dL)"), "mg/dL"


def _stats(s: pd.Series) -> dict:
    s = s.dropna()
    if s.empty:
        return {}
    return {"n": int(len(s)), "first": float(s.iloc[0]), "last": float(s.iloc[-1]),
            "min": float(s.min()), "max": float(s.max()), "mean": float(s.mean())}


def summarize(df: pd.DataFrame, days: Optional[int] = None) -> dict:
    """Per-measure stats over the last `days` days (or all time), by the newest reading's date."""
    if df.empty:
        return {}
    if days:
        df = df[df["when"] >= df["when"].max() - pd.Timedelta(days=days)]
    out = {}
    if df["systolic"].notna().any():
        out["Systolic (mmHg)"] = _stats(df.set_index("when")["systolic"])
        out["Diastolic (mmHg)"] = _stats(df.set_index("when")["diastolic"])
    if df["pulse"].notna().any():
        out["Pulse (/min)"] = _stats(df.set_index("when")["pulse"])
    w, wu = weights_in(df)
    if len(w):
        out[f"Weight ({wu})"] = _stats(w)
    g, _ = glucose_in(df)
    if len(g):
        out["Glucose (mg/dL)"] = _stats(g)
    return out


def daily(df: pd.DataFrame) -> pd.DataFrame:
    """One row per calendar day: mean of each measure that day (weight in one unit)."""
    if df.empty:
        return df
    d = df.assign(day=df["when"].dt.normalize())
    cols = {"systolic": "systolic", "diastolic": "diastolic", "pulse": "pulse"}
    out = d.groupby("day")[list(cols)].mean()
    w, wu = weights_in(df)
    if len(w):
        out[f"weight_{wu}"] = w.groupby(w.index.normalize()).mean()
    g, _ = glucose_in(df)
    if len(g):
        out["glucose_mgdl"] = g.groupby(g.index.normalize()).mean()
    return out.round(1)


def doctor_summary_markdown(df: pd.DataFrame, person: str = "") -> str:
    """A one-page text summary to print or paste into an email."""
    if df.empty:
        return "No confirmed readings yet."
    head = f"# Health readings{f' for {person}' if person else ''}\n\n"
    head += (f"{df['when'].min():%b %d, %Y} to {df['when'].max():%b %d, %Y} "
             f"({len(df)} readings). Values were read from photos and messages and "
             "confirmed by a family member.\n")
    parts = [head]
    for title, days in (("Last 30 days", 30), ("Last 90 days", 90), ("All time", None)):
        stats = summarize(df, days)
        if not stats:
            continue
        lines = [f"\n## {title}\n", "| Measure | Readings | Average | Lowest | Highest | Latest |",
                 "|---|---|---|---|---|---|"]
        for name, s in stats.items():
            lines.append(f"| {name} | {s['n']} | {s['mean']:.1f} | {s['min']:g} | {s['max']:g} | {s['last']:g} |")
        parts.append("\n".join(lines) + "\n")
    parts.append("\nThis summary shows measurements only and makes no medical judgment.\n")
    return "".join(parts)


def series_long(day: pd.DataFrame, labels: dict) -> pd.DataFrame:
    """Daily frame -> long rows (day, series, value) with only the days each series really has.

    A line chart over a wide table with gaps draws nothing between isolated points, so a
    measure taken every few days looked empty. Long form with the gaps removed connects them.
    `labels` maps column name -> display name; columns that are absent or empty are skipped.
    """
    parts = []
    for col, name in labels.items():
        if col in day.columns and day[col].notna().any():
            s = day[col].dropna()
            parts.append(pd.DataFrame({"day": s.index, "series": name, "value": s.values}))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["day", "series", "value"])
