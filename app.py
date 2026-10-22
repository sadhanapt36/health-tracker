"""Streamlit UI (display and input only; logic lives in health_tracker/).

Import -> Scan photos -> Review (confirm each draft against its source) -> History/trends.
"""
import json
import subprocess
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from health_tracker import db
from health_tracker.chat_export import ChatExportError, parse_chat, read_chat_text
from health_tracker.models import DraftReading, RawMessage
from health_tracker.parsing import parse_reading
from health_tracker import trends
from health_tracker.media import MediaStore
from health_tracker.review import build_reading, bulk_weight_candidates, fmt
from health_tracker.sample_import import load_sample
from health_tracker.vision import ImageDraft
from health_tracker.scan import scan_photos
from health_tracker.vision import BpReaderUnavailable, bp_reader_available

ROOT = Path(__file__).parent
# The fictional demo gets its own database so it never mixes with real data.
DB_PATHS = {"sample": ROOT / "data" / "demo.db",
            "export": ROOT / "data" / "health.db",
            "whatsapp": ROOT / "data" / "health.db"}
SOURCES = {"Chat export (recommended)": "export",
           "Sample data (fictional)": "sample",
           "WhatsApp Web (experimental)": "whatsapp"}
# One quiet palette, reused for every chart.
CHART_COLORS = {"bp": ["#2F6F6B", "#C9A66B"], "pulse": ["#B5654A"],
                "glucose": ["#5B7DB1"], "weight": ["#7A8F5C"]}
WEIGHT_UNITS = ["", "kg", "lb"]
GLUCOSE_UNITS = ["", "mg/dL", "mmol/L"]

st.set_page_config(page_title="Health Log", layout="wide")
st.markdown("""
<style>
.block-container {max-width: 1080px; padding-top: 2.4rem; padding-bottom: 4rem;}
h1, h2, h3, [data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3 {
    font-family: ui-serif, "New York", Georgia, serif !important; font-weight: 600 !important;
    letter-spacing: -0.01em !important;}
h1 {font-size: 2.4rem !important; margin-bottom: 0;}
h2, h3 {font-size: 1.4rem !important;}
footer, #MainMenu {visibility: hidden;}
[data-testid="stSidebar"] {border-right: 1px solid #E6E0D6;}
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stForm"] {
    border-color: #E4DDD1 !important; border-radius: 14px; background-color: #FFFFFF !important;}
[data-testid="stMetricLabel"] p {font-size: 0.85rem; color: #6B7773; letter-spacing: 0.02em;}
[data-testid="stMetricValue"] {font-family: ui-serif, "New York", Georgia, serif; font-size: 2rem;}
.stTabs [data-baseweb="tab-list"] {gap: 1.8rem; border-bottom: 1px solid #E4DDD1;}
.stTabs [data-baseweb="tab"] {font-size: 1rem; padding: 0.6rem 0;}
.stButton button, .stDownloadButton button {border-radius: 10px; font-weight: 500;}
</style>
""", unsafe_allow_html=True)
st.title("Health Log")
st.caption("Blood pressure, glucose and weight from the family chat. "
           "Everything stays on this computer. For record-keeping, not medical advice.")

with st.sidebar:
    st.markdown("### Source")
    source_key = SOURCES[st.radio("Where the messages come from", list(SOURCES),
                                  label_visibility="collapsed")]
    export_path = chat_name = ""
    limit = 30
    if source_key == "export":
        export_path = st.text_input(
            "Location of the export",
            placeholder="~/Downloads/WhatsApp Chat.zip",
            help="A .zip, a folder, or _chat.txt. On a Mac: right-click the file, "
                 "hold Option, choose 'Copy as Pathname'.")
    elif source_key == "whatsapp":
        chat_name = st.text_input("Conversation name (exact title)")
        limit = st.number_input("Max messages", 5, 200, 30)
    st.caption(f"Saved in data/{DB_PATHS[source_key].name}")

conn = db.connect(DB_PATHS[source_key])


def clean_path(text: str) -> str:
    # Terminal drag-and-drop escapes spaces and may add quotes.
    return text.strip().strip("'\"").replace("\\ ", " ")


def media_store():
    """Photo files for the current export, remembered after a successful import."""
    path = st.session_state.get("media_path")
    return MediaStore(path) if path else None

if source_key == "sample" and st.sidebar.button("Reset demo data"):
    db.clear_messages(conn)
    st.sidebar.success("Sample data cleared.")


# ------------------------------------------------------------------ Import tab
def import_tab():
    st.subheader("Messages")
    if source_key == "export":
        st.caption("Export the chat from WhatsApp with **Attach Media**, paste its location in the "
                   "sidebar, then import. Importing again later only adds what's new.")
    if not st.button("Sync" if source_key == "whatsapp" else "Import chat", type="primary"):
        return
    messages, parsed = None, None
    try:
        if source_key == "sample":
            parsed = load_sample()
        elif source_key == "export":
            if not export_path.strip():
                st.error("Add the location of the export in the sidebar first.")
            else:
                clean = clean_path(export_path)
                parsed = parse_chat(read_chat_text(Path(clean)))
                st.session_state["media_path"] = clean
        elif not chat_name.strip():
            st.error("Enter the exact conversation title first.")
        else:
            st.info("A browser window opened. Log in if needed, then open the conversation.")
            # Subprocess keeps Playwright out of Streamlit's thread/event loop.
            proc = subprocess.run(
                [sys.executable, "-m", "health_tracker.whatsapp.collector",
                 "--chat", chat_name, "--limit", str(limit)],
                capture_output=True, text=True, timeout=300, cwd=ROOT)
            if proc.returncode != 0:
                st.error(proc.stderr.strip() or "Collector failed.")
            else:
                messages = [RawMessage(**d) for d in json.loads(proc.stdout)]
    except ChatExportError as e:
        st.error(str(e))
    except subprocess.TimeoutExpired:
        st.error("Timed out waiting for WhatsApp Web.")

    if parsed is not None:
        messages = parsed.messages
        if not parsed.date_order_certain:
            st.warning("Every date in this file could be read two ways (like 3/4/26), so month first "
                       "was assumed. Check the dates in the table below.")
    if messages is not None:
        r = db.insert_messages(conn, source_key, messages)
        st.success(f"{r.new} new  ·  {r.updated} updated  ·  {r.duplicate} already here")
        if r.skipped:
            st.warning(f"{r.skipped} message(s) hadn't finished loading and were skipped. "
                       "Click Sync again.")
        if parsed is not None and parsed.ignored:
            st.caption("Skipped: " + ", ".join(f"{n:,} {k}" for k, n in parsed.ignored.items()))


def scan_section():
    """Read the numbers off photos. Libre screenshots and blood pressure monitor displays are
    read on this computer; nothing is uploaded anywhere."""
    st.subheader("Photos")
    if not bp_reader_available():
        st.error("Blood pressure photos can't be read yet because OpenCV isn't installed. "
                 "Run `pip install -r requirements.txt` in the terminal, then restart the app. "
                 "Glucose screenshots still work.")
    media = media_store()
    if media is None or len(media) == 0:
        st.caption("No photo files found. Import a chat export made with **Attach Media** "
                   "(a .zip or folder) to read blood pressure and glucose photos.")
        return
    people = db.senders(conn, source_key)
    c1, c2 = st.columns(2)
    chosen = c1.multiselect("Only photos sent by", people, default=[],
                            help="The chat holds other photos too (food, family). Pick who sends the readings.")
    since = c2.text_input("Only photos since (optional)", placeholder="2026-06-01")
    todo = [r for r in db.photos_to_scan(conn, source_key, chosen or None, since.strip() or None)
            if media.has(r["attachment"])]
    st.caption(f"{len(todo):,} photo(s) waiting to be scanned.")

    if st.button("Scan photos", disabled=not todo):
        bar = st.progress(0.0)
        try:
            res = scan_photos(conn, source_key, media, todo, None,
                              progress=lambda i, n: bar.progress(i / max(n, 1)))
        except BpReaderUnavailable as e:
            st.error(str(e))
            return
        bar.empty()
        st.success(f"Read {res.scanned} photo(s); {res.with_values} contained a reading. "
                   "Open **Review** to confirm them.")
        if res.not_read:
            st.info(f"{res.not_read} photo(s) weren't a Libre screenshot or a clear blood pressure "
                    "display (food, family photos, or too blurry or angled to read safely), "
                    "so they were left alone.")
        if res.errors:
            st.warning(f"{res.errors} photo(s) failed and can be retried.")


def messages_table():
    rows = db.list_messages(conn, source_key)
    if not rows:
        st.caption("Nothing imported yet.")
        return
    st.subheader("Imported messages")
    show = st.radio("Show", ["Typed messages", "Photos", "All"], horizontal=True)
    df = pd.DataFrame([dict(r) for r in rows])
    df["has_image"] = df["has_image"].astype(bool)
    if show == "Typed messages":
        df = df[df["text"] != ""]
    elif show == "Photos":
        df = df[df["has_image"]]
    st.caption(f"{len(df):,} messages shown ({len(rows):,} stored)")
    table = df[["sent_at", "sender", "text", "attachment", "review_status"]].rename(columns={
        "sent_at": "Sent", "sender": "From", "text": "Message", "attachment": "Photo",
        "review_status": "Status"})
    st.dataframe(table, width="stretch", hide_index=True)


# ------------------------------------------------------------------ Review tab
def reading_form(key: str, draft: DraftReading, row=None, title: str = ""):
    """One editable form. With a source message it also offers Discard and Skip."""
    with st.form(key, clear_on_submit=row is None):
        if title:
            st.markdown(f"**{title}**")
        c1, c2 = st.columns(2)
        person = c1.text_input("Whose reading (optional)", key=f"{key}-person",
                               value=st.session_state.get("last_person", ""))
        measured_at = c2.text_input(
            "Measured at (optional)", key=f"{key}-at", placeholder="2026-10-21 08:10",
            help="Leave blank if you don't know. The message time is stored separately.")
        a, b, c = st.columns(3)
        systolic = a.text_input("Systolic (mmHg)", fmt(draft.systolic), key=f"{key}-sys")
        diastolic = b.text_input("Diastolic (mmHg)", fmt(draft.diastolic), key=f"{key}-dia")
        pulse = c.text_input("Pulse (/min)", fmt(draft.pulse), key=f"{key}-pulse")
        w1, w2, g1, g2 = st.columns(4)
        weight = w1.text_input("Weight", fmt(draft.weight), key=f"{key}-w")
        weight_unit = w2.selectbox("Weight unit", WEIGHT_UNITS, key=f"{key}-wu",
                                   index=WEIGHT_UNITS.index(draft.weight_unit or ""))
        glucose = g1.text_input("Glucose", fmt(draft.glucose), key=f"{key}-g")
        glucose_unit = g2.selectbox("Glucose unit", GLUCOSE_UNITS, key=f"{key}-gu",
                                    index=GLUCOSE_UNITS.index(draft.glucose_unit or ""))
        note = st.text_input("Note (optional)", key=f"{key}-note")
        b1, b2, b3 = st.columns(3)
        save = b1.form_submit_button("Save reading", type="primary")
        discard = b2.form_submit_button("Not a reading") if row else False
        skip = b3.form_submit_button("Skip for now") if row else False

    if save:
        try:
            reading = build_reading(dict(
                person=person, measured_at=measured_at, systolic=systolic, diastolic=diastolic,
                pulse=pulse, weight=weight, weight_unit=weight_unit, glucose=glucose,
                glucose_unit=glucose_unit, note=note))
        except ValueError as e:
            st.error(str(e))
            return
        if not db.add_reading(conn, row["id"] if row else None, reading):
            st.warning("That message already has a saved reading.")
        st.session_state["last_person"] = person
        st.session_state["flash"] = "Reading saved."
        st.rerun()
    if discard:
        db.set_review_status(conn, row["id"], "discarded")
        st.rerun()
    if skip:
        st.session_state.setdefault("skipped", set()).add(row["id"])
        st.rerun()


def bulk_weight_section():
    """Confirm many clean weight messages at once, after a visible preview."""
    pending = [r for r in db.pending_messages(conn, source_key)]
    safe, look = bulk_weight_candidates(pending, parse_reading)
    if not safe:
        return
    with st.expander(f"Confirm weight messages in bulk  ({len(safe)} look clean, {len(look)} need a closer look)"):
        st.write("Only messages that are **just a weight with a stated unit**, with no warnings and a "
                 "value near the usual one, are listed. Anything odd stays in the one-by-one queue below.")
        preview = pd.DataFrame([{"message time": r["sent_at"] or r["sent_at_raw"], "message": r["text"],
                                 "weight": d.weight, "unit": d.weight_unit} for r, d in safe])
        st.dataframe(preview, width="stretch", height=260)
        person = st.text_input("Whose weight (optional)", key="bulk-person",
                               value=st.session_state.get("last_person", ""))
        checked = st.checkbox(f"I looked through the {len(safe)} rows above and they match the messages",
                              key="bulk-checked")
        if st.button(f"Confirm all {len(safe)}", disabled=not checked, type="primary", key="bulk-go"):
            saved = 0
            for r, d in safe:
                saved += db.add_reading(conn, r["id"], build_reading(dict(
                    person=person, weight=fmt(d.weight), weight_unit=d.weight_unit)))
            st.session_state["last_person"] = person
            st.session_state["flash"] = f"Saved {saved} weight reading(s)."
            st.rerun()


def review_tab():
    if flash := st.session_state.pop("flash", None):
        st.success(flash)
    with st.expander("Add a reading by hand"):
        reading_form("manual", DraftReading())
    bulk_weight_section()

    skipped = st.session_state.setdefault("skipped", set())
    queue, seen = [], set()
    for row in db.pending_messages(conn, source_key):
        draft = parse_reading(row["text"])
        if draft.has_values() and row["id"] not in skipped:
            queue.append((row, draft, False)); seen.add(row["id"])
    for row, ex in db.pending_photo_readings(conn, source_key):
        if row["id"] in skipped or row["id"] in seen:
            continue
        queue.append((row, ImageDraft(**ex).to_draft(), True))
    queue.sort(key=lambda t: (t[0]["sent_at"] or "", t[0]["id"]), reverse=True)
    st.subheader("To review")
    st.caption(f"{len(queue)} waiting" + (f"  ·  {len(skipped)} skipped for now" if skipped else ""))
    if not queue:
        st.info("All caught up. Import messages and scan photos to find more.")
        return

    row, draft, is_photo = queue[0]
    st.caption("These numbers were read automatically. Check them against the source, "
               "fix anything wrong, and clear anything you're unsure of.")
    left, right = st.columns([2, 3], gap="medium")
    with left, st.container(border=True):
        if is_photo:
            st.markdown("**Source photo**")
            media = media_store()
            data = media.read(row["attachment"]) if media else None
            if data:
                st.image(data, width="stretch")
            else:
                st.warning("The photo file isn't available. Re-enter the export path and import again.")
        else:
            st.markdown("**Source message**")
            st.text_area("Message text", row["text"], disabled=True, height=140,
                         key=f"src-{row['id']}", label_visibility="collapsed")
        st.caption(f"From {row['sender']}  ·  sent {row['sent_at_raw']}")
        for n in draft.notes:
            st.warning(n)
    with right:
        reading_form(f"msg-{row['id']}", draft, row, title="Reading")


# ------------------------------------------------------------------ History tab
def _find(stats: dict, prefix: str):
    return next((v for k, v in stats.items() if k.startswith(prefix)), None)


def summary_cards(stats: dict):
    """Latest value per measure with a plain caption. Descriptive only: no colours, arrows or verdicts."""
    cards = []
    sys_, dia = _find(stats, "Systolic"), _find(stats, "Diastolic")
    if sys_ and dia:
        cards.append(("Blood pressure", f"{sys_['last']:.0f}/{dia['last']:.0f}",
                      f"average {sys_['mean']:.0f}/{dia['mean']:.0f}", f"{sys_['n']} readings"))
    if pulse := _find(stats, "Pulse"):
        cards.append(("Pulse", f"{pulse['last']:.0f}", f"average {pulse['mean']:.0f}", f"{pulse['n']} readings"))
    if glu := _find(stats, "Glucose"):
        cards.append(("Glucose (mg/dL)", f"{glu['last']:.0f}", f"average {glu['mean']:.0f}", f"{glu['n']} readings"))
    if wt := _find(stats, "Weight"):
        unit = next(k for k in stats if k.startswith("Weight")).split("(")[1].rstrip(")")
        cards.append((f"Weight ({unit})", f"{wt['last']:g}", f"first {wt['first']:g}", f"{wt['n']} readings"))
    if not cards:
        return
    for col, (label, value, line1, line2) in zip(st.columns(len(cards), gap="small"), cards):
        with col, st.container(border=True):
            st.metric(label, value)
            st.caption(f"{line1}  \n{line2}")


def trend_chart(long: pd.DataFrame, colors: list):
    """Lines through the days a measure was taken, with a dot on each, so sparse data still reads."""
    names = list(dict.fromkeys(long["series"]))
    base = alt.Chart(long).encode(
        x=alt.X("day:T", title=None, axis=alt.Axis(format="%b %d", labelColor="#6B7773", grid=False)),
        y=alt.Y("value:Q", title=None, scale=alt.Scale(zero=False, nice=True),
                axis=alt.Axis(labelColor="#6B7773", gridColor="#EEE8DE")),
        color=alt.Color("series:N", scale=alt.Scale(domain=names, range=colors[:len(names)]),
                        legend=alt.Legend(orient="bottom", title=None, labelColor="#6B7773") if len(names) > 1 else None),
        tooltip=[alt.Tooltip("day:T", title="Day", format="%b %d, %Y"), "series:N",
                 alt.Tooltip("value:Q", title="Value", format=".1f")])
    chart = (base.mark_line(strokeWidth=2) + base.mark_point(filled=True, size=45)).properties(height=210)
    st.altair_chart(chart.configure_view(strokeWidth=0), width="stretch")


def history_tab():
    rows = db.list_readings(conn)
    if not rows:
        st.subheader("History")
        st.caption("No saved readings yet. Confirm some in Review and they will show up here.")
        return
    df = pd.DataFrame([dict(r) for r in rows])
    df["time basis"] = df["measured_at"].map(lambda v: "measured" if pd.notna(v) and v else "message time")
    cols = ["when", "time basis", "person", "systolic", "diastolic", "pulse",
            "weight", "weight_unit", "glucose", "glucose_unit", "note"]

    frame = trends.readings_frame(rows)
    people = sorted({p for p in df["person"].dropna() if p})
    f1, f2 = st.columns([1, 2])
    who = f1.selectbox("Person", ["Everyone"] + people) if people else "Everyone"
    window = f2.radio("Period", ["30 days", "90 days", "1 year", "All"], horizontal=True, index=1)
    if who != "Everyone":
        frame = frame[frame["person"] == who]
        df = df[df["person"] == who]
    days = {"30 days": 30, "90 days": 90, "1 year": 365, "All": None}[window]
    view = frame if days is None else frame[frame["when"] >= frame["when"].max() - pd.Timedelta(days=days)]

    st.subheader("Overview")
    if view.empty:
        st.info("No readings in this period.")
    else:
        stats = trends.summarize(view)
        summary_cards(stats)
        day = trends.daily(view)
        weight_col = next((c for c in day.columns if c.startswith("weight_")), None)
        charts = (("Blood pressure (mmHg)", {"systolic": "Systolic", "diastolic": "Diastolic"}, "bp"),
                  ("Pulse (per minute)", {"pulse": "Pulse"}, "pulse"),
                  ("Glucose (mg/dL)", {"glucose_mgdl": "Glucose"}, "glucose"),
                  ("Weight", {weight_col: "Weight"} if weight_col else {}, "weight"))
        for title, labels, palette in charts:
            long = trends.series_long(day, labels)
            if not long.empty:
                with st.container(border=True):
                    st.markdown(f"**{title}**")
                    trend_chart(long, CHART_COLORS[palette])
        st.caption("Daily averages. Weight uses the most common unit in the data, and readings with "
                   "no stated unit are left out of that chart.")
        with st.expander("Numbers behind the cards"):
            st.dataframe(pd.DataFrame(stats).T.rename(columns={
                "n": "readings", "first": "first", "last": "latest", "min": "lowest",
                "max": "highest", "mean": "average"}).round(1), width="stretch")

    st.subheader("For the doctor")
    summary = trends.doctor_summary_markdown(frame, who if who != "Everyone" else "")
    with st.container(border=True):
        st.caption("A short summary of the readings, plus the full list as a spreadsheet.")
        d1, d2, _ = st.columns([1, 1, 2])
        d1.download_button("Download summary", summary, "health-summary.md", "text/markdown")
        d2.download_button("Download all readings", df[cols].to_csv(index=False),
                           "health-readings.csv", "text/csv")
        with st.expander("Preview summary"):
            st.markdown(summary)

    st.subheader("All readings")
    st.caption(f"{len(df)} saved. When no measurement time was entered, the message time is "
               "shown and labelled as such.")
    st.dataframe(df[cols], width="stretch", hide_index=True)


tab_import, tab_review, tab_history = st.tabs(["Import", "Review", "History"])
with tab_import:
    import_tab()
    scan_section()
    messages_table()
with tab_review:
    review_tab()
with tab_history:
    history_tab()
