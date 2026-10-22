"""Fictional demo data, in the same layout as a real chat export."""
from pathlib import Path

from .chat_export import ParseResult, parse_chat, read_chat_text

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "sample_data" / "fictional_chat.txt"


def load_sample(path: Path = SAMPLE_PATH) -> ParseResult:
    return parse_chat(read_chat_text(path))
