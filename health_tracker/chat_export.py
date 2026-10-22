"""Read a WhatsApp "Export Chat" file (iPhone layout) into RawMessages.

Pure Python, no browser. Handles a .txt file, a .zip, or an unzipped folder.

Layout handled (one message per entry, text may continue on later lines):
    [11/9/23, 10:16:01 PM] Sender: message text
    [11/9/23, 10:16:09 PM] Sender: <attached: 00000010-PHOTO-2023-11-19-18-07-52.jpg>
Invisible characters WhatsApp inserts around these lines are stripped first.
"""
import hashlib
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import RawMessage

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

# \u200e/\u200f are invisible direction marks; \u202f is a narrow space before AM/PM.
_CLEAN = str.maketrans({"\u200e": "", "\u200f": "", "\u202f": " ", "\u00a0": " "})
_DATE = r"(?P<d>\d{1,2}[/.]\d{1,2}[/.]\d{2,4})"
_TIME = r"(?P<t>\d{1,2}:\d{2}(?::\d{2})?)\s?(?P<ap>[AaPp][Mm])?"
_START = re.compile(                                  # iPhone: [11/9/23, 10:16:01 PM] Sender: text
    rf"^\[{_DATE},?\s+{_TIME}\]\s+(?P<sender>[^:]+):\s?(?P<body>.*)$")
_START_ANDROID = re.compile(                          # Android: 11/9/23, 10:16 PM - Sender: text
    rf"^{_DATE},?\s+{_TIME}\s+-\s+(?P<sender>[^:]+?):\s?(?P<body>.*)$")
_START_ANDROID_SYSTEM = re.compile(                   # Android notice with no sender
    rf"^{_DATE},?\s+{_TIME}\s+-\s+(?P<body>.+)$")
_ATTACHED = re.compile(r"<attached:\s*(?P<n>[^>]+)>")
_ATTACHED_ANDROID = re.compile(r"(?P<n>[^\s<>]+\.\w{2,5})\s*\(file attached\)", re.I)  # Android: IMG-x.jpg (file attached)
_OMITTED = re.compile(
    r"\b(?P<kind>image|video|audio|document|sticker|gif|contact card) omitted\b", re.I
)
_CALL = re.compile(r"^(missed )?(voice|video) call\b", re.I)


class ChatExportError(Exception):
    """A problem the person can fix (wrong path, missing text file, ...)."""


@dataclass
class ParseResult:
    messages: list[RawMessage]
    ignored: dict[str, int]        # what we deliberately left out, with counts
    date_order: str                # "MDY" or "DMY"
    date_order_certain: bool       # False if every date was ambiguous (e.g. 3/4/26)


# ---------- finding and reading the file ----------

def _decode(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def _is_chat_txt(name: str) -> bool:
    base = Path(name).name
    return name.lower().endswith(".txt") and "__MACOSX" not in name and not base.startswith("._")


def read_chat_text(path) -> str:
    path = Path(path).expanduser()
    if not path.exists():
        raise ChatExportError(f"Not found: {path}")

    no_txt = ("No chat text file found. An export made with Attach Media should "
              "contain a file named _chat.txt. Find it, or export again "
              "(Without Media gives just the text file).")

    if path.is_dir():
        names = sorted((p for p in path.rglob("*.txt") if _is_chat_txt(str(p))),
                       key=lambda p: (p.name != "_chat.txt", str(p)))
        if not names:
            raise ChatExportError(no_txt)
        return _decode(names[0].read_bytes())

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = sorted((n for n in z.namelist() if _is_chat_txt(n)),
                           key=lambda n: (Path(n).name != "_chat.txt", n))
            if not names:
                raise ChatExportError(no_txt)
            return _decode(z.read(names[0]))

    return _decode(path.read_bytes())


# ---------- parsing ----------

def detect_date_order(date_strings: list[str]) -> tuple[str, bool]:
    """Work out month-first vs day-first from the data itself.

    A value above 12 can only be a day. If every date is ambiguous we default
    to month-first and say we guessed.
    """
    first_big = second_big = False
    for d in date_strings:
        a, b = (int(x) for x in re.split(r"[/.]", d)[:2])
        first_big |= a > 12
        second_big |= b > 12
    if first_big and second_big:
        raise ChatExportError("Dates in this file are inconsistent; cannot tell the date order.")
    if first_big:
        return "DMY", True
    if second_big:
        return "MDY", True
    return "MDY", False


def _to_iso(d: str, t: str, ap: Optional[str], order: str) -> Optional[str]:
    try:
        a, b, y = re.split(r"[/.]", d)
        month, day = (a, b) if order == "MDY" else (b, a)
        year = int(y) + (2000 if int(y) < 100 else 0)
        parts = [int(x) for x in t.split(":")]
        hour, minute, second = parts[0], parts[1], (parts[2] if len(parts) > 2 else 0)
        if ap:
            hour = hour % 12 + (12 if ap.upper() == "PM" else 0)
        return datetime(year, int(month), int(day), hour, minute, second).isoformat()
    except ValueError:
        return None  # leave blank rather than guess


def parse_chat(text: str) -> ParseResult:
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Pass 1: group lines into entries. Lines that don't start a new message
    # continue the previous one.
    entries: list[dict] = []
    for line in text.split("\n"):
        line = line.translate(_CLEAN)
        m = _START.match(line) or _START_ANDROID.match(line)
        sysm = None if m else _START_ANDROID_SYSTEM.match(line)
        if m:
            entries.append({**m.groupdict(), "lines": [m["body"]]})
        elif sysm:
            entries.append({**sysm.groupdict(), "sender": "", "lines": [sysm["body"]], "system": True})
        if (m or sysm):
            entries[-1]["d"] = entries[-1]["d"].replace(".", "/")
        elif entries and line.strip():
            entries[-1]["lines"].append(line)

    if not entries:
        raise ChatExportError(
            "No chat messages were found in that file. Expected lines like "
            "'[11/9/23, 10:16:01 PM] Name: text' (iPhone) or '11/9/23, 10:16 PM - Name: text' "
            "(Android). Check that you picked the WhatsApp export (_chat.txt), not another file.")

    order, certain = detect_date_order([e["d"] for e in entries])

    # Pass 2: classify. Calls and system notices are noise; media other than
    # images is ignored unless it carries a typed caption.
    ignored: dict[str, int] = {}

    def skip(reason: str):
        ignored[reason] = ignored.get(reason, 0) + 1

    seen: dict[str, int] = {}
    messages: list[RawMessage] = []
    for e in entries:
        body = "\n".join(e["lines"]).strip()
        if e.get("system") or "end-to-end encrypted" in body.lower():
            skip("system notices"); continue
        if _CALL.match(body):
            skip("call notices"); continue

        attachment, has_image = None, False
        att = _ATTACHED.search(body)
        android = None if att else _ATTACHED_ANDROID.search(body)
        omitted = _OMITTED.search(body)
        if android:
            attachment = android["n"].strip()
            body = _ATTACHED_ANDROID.sub("", body).strip()
            has_image = Path(attachment).suffix.lower() in IMAGE_EXTS
            att = android
        elif "<media omitted>" in body.lower():           # Android, media not included in export
            skip("media (not in export)"); continue
        elif att:
            attachment = att["n"].strip()
            body = _ATTACHED.sub("", body).strip()
            has_image = Path(attachment).suffix.lower() in IMAGE_EXTS
        elif omitted:  # exports made "Without Media" only say that media existed
            body = _OMITTED.sub("", body).strip()
            has_image = omitted["kind"].lower() == "image"
            if not has_image and not body:
                skip("videos/audio/documents"); continue
        if att and not has_image and not body:
            skip("videos/audio/documents"); continue
        if not body and not has_image:
            skip("empty messages"); continue

        raw_time = f"{e['d']}, {e['t']}" + (f" {e['ap'].upper()}" if e["ap"] else "")
        digest = hashlib.sha1(
            f"{raw_time}|{e['sender']}|{body}|{attachment}".encode("utf-8")
        ).hexdigest()[:12]
        n = seen.get(digest, 0)       # identical lines in the same second get 0, 1, 2...
        seen[digest] = n + 1
        messages.append(RawMessage(
            source_message_id=f"export-{digest}-{n}",  # derived from content: exports have no WhatsApp id
            sender=e["sender"].strip(),
            sent_at_raw=raw_time,
            sent_at=_to_iso(e["d"], e["t"], e["ap"], order),
            text=body,
            has_image=has_image,
            attachment=attachment,
        ))
    return ParseResult(messages, ignored, order, certain)
