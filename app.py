"""Streamlit UI (display and input only; logic lives in health_tracker/).

Import -> Scan photos -> Review (confirm each draft against its source) -> History/trends.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

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
SOURCES = {"Chat export file (recommended)": "export",
           "Sample export (fictional)": "sample",
           "WhatsApp Web (experimental)": "whatsapp"}
WEIGHT_UNITS = ["", "kg", "lb"]
GLUCOSE_UNITS = ["", "mg/dL", "mmol/L"]

st.set_page_config(page_title="Health Log", layout="wide")
st.title("Health readings from WhatsApp")
st.caption("Local-only. Use fictional data for demos. Not medical advice.")

with st.sidebar:
    source_key = SOURCES[st.radio("Source", list(SOURCES))]
    export_path = chat_name = ""
    limit = 30
    if source_key == "export":
        export_path = st.text_input(
            "Path to the export (.zip, folder, or _chat.txt)",
            help="On a Mac: right-click the file, hold Option, choose 'Copy as Pathname'.")
    elif source_key == "whatsapp":
        chat_name = st.text_input("Conversation name (exact title)")
        limit = st.number_input("Max messages", 5, 200, 30)
    st.caption(f"Database: data/{DB_PATHS[source_key].name}")

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
    st.sidebar.success("Demo data cleared (messages and readings).")


# ------------------------------------------------------------------ Import tab
def import_tab():
    if not st.button("Sync" if source_key == "whatsapp" else "Import", type="primary"):
        return
    messages, parsed = None, None
    try:
        if source_key == "sample":
            parsed = load_sample()
        elif source_key == "export":
            if not export_path.strip():
                st.error("Enter the path to your export first.")
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
            st.warning("Every date in this file was ambiguous, so I assumed month/day/year. "
                       "Check the dates in the table.")
    if messages is not None:
        r = db.insert_messages(conn, source_key, messages)
        st.success(f"{r.new} new, {r.updated} completed, {r.duplicate} already imported.")
        if r.skipped:
            st.warning(f"{r.skipped} message(s) hadn't finished loading in WhatsApp Web "
                       "and were skipped. Click Sync again.")
        if parsed is not None and parsed.ignored:
            st.caption("Left out: " + ", ".join(f"{n:,} {k}" for k, n in parsed.ignored.items()))


def scan_section():
    """Read the numbers off photos. Libre screenshots and BP monitor displays are read on this
    computer for free; the Claude reader is an optional paid fallback for photos they decline."""
    st.subheader("Photos")
    if not bp_reader_available():
        st.error("Blood pressure photos can't be read yet: OpenCV isn't installed. In the terminal, run "
                 "`pip install -r requirements.txt`, then restart the app. Glucose screenshots still work.")
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
    st.caption(f"{len(todo):,} photo(s) not scanned yet, found in the export.")

    use_cloud = st.checkbox(
        "Optional: also try photos the local readers can't read with Claude (paid; sends each photo to Anthropic)",
        help="Not needed for FreeStyle Libre screenshots or Omron-style blood pressure monitor photos, "
             "which are read on this computer for free. Needs ANTHROPIC_API_KEY.")
    if use_cloud and not os.environ.get("ANTHROPIC_API_KEY"):
        st.warning("Set the ANTHROPIC_API_KEY environment variable before starting the app.")
        use_cloud = False
    if use_cloud and len(todo) > 50 and not chosen:
        st.warning("That is a lot of photos. Pick the sender and a start date so only readings are sent.")
    if st.button("Scan photos", disabled=not todo or (use_cloud and len(todo) > 50 and not chosen)):
        cloud = None
        if use_cloud:
            from health_tracker.vision import ClaudeVisionExtractor
            cloud = ClaudeVisionExtractor()
        bar = st.progress(0.0)
        try:
            res = scan_photos(conn, source_key, media, todo, cloud,
                              progress=lambda i, n: bar.progress(i / max(n, 1)))
        except BpReaderUnavailable as e:
            st.error(str(e))
            return
        st.success(f"Read {res.scanned} photo(s); {res.with_values} contained a reading. "
                   "Go to **Review** to confirm them.")
        if res.not_read:
            st.info(f"{res.not_read} photo(s) weren't recognised as a Libre screenshot or a blood pressure "
                    "monitor display (food, family photos, or too blurry/angled to read safely). "
                    "They are left alone and can be entered by hand if needed.")
        if res.stopped_early:
            st.error("Stopped after repeated errors (check your API key / connection). Click Scan again.")
        elif res.errors:
            st.warning(f"{res.errors} photo(s) failed and can be retried.")


def messages_table():
    rows = db.list_messages(conn, source_key)
    if not rows:
        st.write("Nothing stored yet. Click **Import**.")
        return
    show = st.radio("Show", ["Typed messages", "Photos", "All"], horizontal=True)
    df = pd.DataFrame([dict(r) for r in rows])
    df["has_image"] = df["has_image"].astype(bool)
    if show == "Typed messages":
        df = df[df["text"] != ""]
    elif show == "Photos":
        df = df[df["has_image"]]
    st.caption(f"{len(df):,} messages shown ({len(rows):,} stored)")
    st.dataframe(df[["sent_at", "sender", "text", "attachment", "has_image", "review_status"]],
                 width="stretch")


# ------------------------------------------------------------------ Review tab
def reading_form(key: str, draft: DraftReading, row=None):
    """One editable form. With a source message it also offers Discard and Skip."""
    with st.form(key, clear_on_submit=row is None):
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
        save = b1.form_submit_button("Confirm and save", type="primary")
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
        st.session_state["flash"] = "Saved."
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
    with st.expander(f"Bulk-confirm weight messages ({len(safe)} look clean, {len(look)} need a look)"):
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
    with st.expander("Manual entry (no source message)"):
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
    st.caption(f"{len(queue)} item(s) with possible readings waiting. "
               f"{len(skipped)} skipped this session.")
    if not queue:
        st.info("Nothing to review. Import messages (and scan photos) first, or everything is done.")
        return

    row, draft, is_photo = queue[0]
    st.info("These values are suggestions read automatically. "
            "Compare them with the source, fix anything wrong, and clear anything you're unsure of.")
    left, right = st.columns([2, 3])
    with left:
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
        st.caption(f"From {row['sender']} · message time {row['sent_at_raw']}")
        for n in draft.notes:
            st.warning(n)
    with right:
        st.markdown("**Reading (edit, then confirm)**")
        reading_form(f"msg-{row['id']}", draft, row)


# ------------------------------------------------------------------ History tab
def history_tab():
    rows = db.list_readings(conn)
    if not rows:
        st.write("No confirmed readings yet.")
        return
    df = pd.DataFrame([dict(r) for r in rows])
    df["time basis"] = df["measured_at"].map(lambda v: "measured" if pd.notna(v) and v else "message time")
    cols = ["when", "time basis", "person", "systolic", "diastolic", "pulse",
            "weight", "weight_unit", "glucose", "glucose_unit", "note"]

    frame = trends.readings_frame(rows)
    people = sorted({p for p in df["person"].dropna() if p})
    who = st.selectbox("Person", ["Everyone"] + people) if people else "Everyone"
    if who != "Everyone":
        frame = frame[frame["person"] == who]
        df = df[df["person"] == who]

    st.subheader("Trends")
    window = st.radio("Period", ["30 days", "90 days", "1 year", "All"], horizontal=True, index=1)
    days = {"30 days": 30, "90 days": 90, "1 year": 365, "All": None}[window]
    view = frame if days is None else frame[frame["when"] >= frame["when"].max() - pd.Timedelta(days=days)]
    if view.empty:
        st.info("No readings in this period.")
    else:
        stats = trends.summarize(view)
        st.dataframe(pd.DataFrame(stats).T.rename(columns={
            "n": "readings", "first": "first", "last": "latest", "min": "lowest",
            "max": "highest", "mean": "average"}).round(1), width="stretch")
        day = trends.daily(view)
        for title, columns in (("Blood pressure (mmHg)", ["systolic", "diastolic"]),
                               ("Pulse (/min)", ["pulse"]),
                               ("Glucose (mg/dL)", ["glucose_mgdl"]),
                               ("Weight", [c for c in day.columns if c.startswith("weight_")])):
            cols_present = [c for c in columns if c in day.columns and day[c].notna().any()]
            if cols_present:
                st.markdown(f"**{title}**")
                st.line_chart(day[cols_present], height=220)
        st.caption("Daily averages. Weight uses the most common unit in the data; readings with "
                   "no stated unit are left out of the weight chart. Shows measurements only, no medical judgment.")

    st.subheader("For the doctor")
    summary = trends.doctor_summary_markdown(frame, who if who != "Everyone" else "")
    with st.expander("Preview summary"):
        st.markdown(summary)
    d1, d2 = st.columns(2)
    d1.download_button("Download summary (.md)", summary, "health-summary.md", "text/markdown")
    d2.download_button("Download all readings (.csv)", df[cols].to_csv(index=False),
                       "health-readings.csv", "text/csv")

    st.subheader("All confirmed readings")
    st.caption(f"{len(df)} confirmed reading(s). When no measurement time was entered, "
               "the message time is shown and labelled as such.")
    st.dataframe(df[cols], width="stretch")


tab_import, tab_review, tab_history = st.tabs(["1. Import", "2. Review", "3. History"])
with tab_import:
    import_tab()
    scan_section()
    messages_table()
with tab_review:
    review_tab()
with tab_history:
    history_tab()
