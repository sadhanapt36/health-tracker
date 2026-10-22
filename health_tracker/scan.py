"""Scan photo messages into stored drafts. No Streamlit, easy to test."""
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from . import db
from .media import MediaStore, media_type
from .vision import BpReaderUnavailable, ClaudeVisionExtractor, read_photo


@dataclass
class ScanResult:
    scanned: int = 0          # photos read (stored)
    with_values: int = 0      # of those, how many held a health reading
    missing_file: int = 0     # chat mentions the photo but it isn't in the export
    not_read: int = 0         # no reader available for it (e.g. BP photo, cloud off)
    errors: int = 0
    stopped_early: bool = False


def scan_photos(conn, source: str, media: MediaStore, rows, cloud: Optional[ClaudeVisionExtractor] = None,
                progress: Optional[Callable[[int, int], None]] = None,
                max_consecutive_errors: int = 3) -> ScanResult:
    res, streak = ScanResult(), 0
    for i, row in enumerate(rows):
        if progress:
            progress(i, len(rows))
        data = media.read(row["attachment"])
        if data is None:
            res.missing_file += 1
            continue
        try:
            draft = read_photo(data, media_type(row["attachment"]), cloud)
        except BpReaderUnavailable:
            raise                              # a setup problem the person must see, not skip
        except Exception:                      # network, bad key, rate limit...
            res.errors += 1
            streak += 1
            if streak >= max_consecutive_errors:
                res.stopped_early = True
                break
            continue
        streak = 0
        if draft is None:
            res.not_read += 1                  # not stored: can be scanned later with the cloud reader on
            continue
        values = asdict(draft)
        db.save_extraction(conn, row["id"], draft.kind, values, draft.reader)
        res.scanned += 1
        res.with_values += int(draft.has_values())
    if progress:
        progress(len(rows), len(rows))
    return res
