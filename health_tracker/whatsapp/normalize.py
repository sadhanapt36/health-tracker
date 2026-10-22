"""Turn the loosely structured dicts scraped from the page into RawMessage.

Pure functions, no browser needed, so they are easy to unit test.
"""
import re
from datetime import datetime
from typing import Optional

from ..models import RawMessage

# WhatsApp puts e.g. "[3:45 PM, 10/21/2026] Grandpa: " in a data-pre-plain-text
# attribute. The format depends on the account's locale; add yours if needed.
TIME_FORMATS = ("%I:%M %p, %m/%d/%Y", "%H:%M, %m/%d/%Y")

_PRE_PLAIN = re.compile(r"^\[(?P<ts>[^\]]+)\]\s*(?P<sender>.*?):\s*$")
_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(?:\s?[AaPp][Mm])?$")
_NOISE = re.compile(r"^(forwarded|forwarded many times|\+\d+)$", re.I)


def _clean(s: str) -> str:
    # Newer WhatsApp builds use a narrow no-break space before AM/PM.
    return s.replace("\u202f", " ").replace("\u00a0", " ").strip()


def parse_pre_plain_text(value: Optional[str]):
    """Return (sent_at_raw, sent_at_iso, sender). Unknown parts are None."""
    if not value:
        return None, None, None
    m = _PRE_PLAIN.match(_clean(value))
    if not m:
        return None, None, None
    raw, sender = m["ts"], (m["sender"] or None)
    for fmt in TIME_FORMATS:
        try:
            iso = datetime.strptime(raw, fmt).isoformat(timespec="minutes")
            return raw, iso, sender
        except ValueError:
            continue
    return raw, None, sender  # keep the raw text; don't guess a date


def parse_row_text(row_text: Optional[str]):
    """Fallback when the metadata attribute is missing.

    Takes the visible text of a whole message row and returns
    (text, time_label). Drops "Forwarded" labels and "+2" album counters.
    The time label is time-of-day only, so the date stays unknown.
    """
    if not row_text:
        return "", None
    time_label, body = None, []
    for line in (_clean(l) for l in row_text.splitlines()):
        if not line:
            continue
        if _TIME_ONLY.match(line):
            time_label = line
        elif not _NOISE.match(line):
            body.append(line)
    return "\n".join(body), time_label


def to_raw_message(d: dict) -> RawMessage:
    raw, iso, sender = parse_pre_plain_text(d.get("prePlain"))
    text = (d.get("text") or "").strip()
    if not text or raw is None:
        fb_text, fb_time = parse_row_text(d.get("rowText"))
        text = text or fb_text
        raw = raw or fb_time  # iso stays None: no date, so we don't invent one
    return RawMessage(
        source_message_id=d.get("id") or None,
        sender=sender,
        sent_at_raw=raw,
        sent_at=iso,
        text=text,
        has_image=bool(d.get("hasImage")),
    )


def merge_best_rows(best: dict, rows: list[dict]) -> list[str]:
    """Fold one page read into `best` (id -> row) and return the ids in page order.

    WhatsApp Web leaves off-screen rows as empty shells, so the same row can be
    empty in one read and filled in another. A filled row is never replaced by
    an empty one.
    """
    for r in rows:
        filled = bool((r.get("rowText") or "").strip())
        if filled or r["id"] not in best:
            best[r["id"]] = r
    return [r["id"] for r in rows]
