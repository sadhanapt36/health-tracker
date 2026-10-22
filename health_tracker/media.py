"""Find the photo files that belong to a chat export.

An export made with "Attach Media" is a .zip (or folder) holding _chat.txt plus
the photos, named exactly as the chat refers to them (<attached: NAME.jpg>).
Pure Python, no Streamlit.
"""
import zipfile
from pathlib import Path
from typing import Optional

from .chat_export import IMAGE_EXTS


class MediaStore:
    """Look up photo bytes by file name in a zip, a folder, or next to a .txt."""

    def __init__(self, path):
        self.path = Path(path).expanduser()
        self._zip: Optional[zipfile.ZipFile] = None
        self._index: dict[str, str] = {}     # lower-case base name -> zip member / file path
        if not self.path.exists():
            return
        if self.path.is_dir():
            root = self.path
        elif self.path.suffix.lower() == ".zip":
            self._zip = zipfile.ZipFile(self.path)
            for n in self._zip.namelist():
                if self._usable(n):
                    self._index[Path(n).name.lower()] = n
            return
        else:
            root = self.path.parent            # a loose _chat.txt: photos sit beside it
        for p in root.rglob("*"):
            if p.is_file() and self._usable(str(p)):
                self._index[p.name.lower()] = str(p)

    @staticmethod
    def _usable(name: str) -> bool:
        base = Path(name).name
        return (Path(name).suffix.lower() in IMAGE_EXTS
                and "__MACOSX" not in name and not base.startswith("._"))

    def __len__(self) -> int:
        return len(self._index)

    def has(self, name: Optional[str]) -> bool:
        return bool(name) and Path(name).name.lower() in self._index

    def read(self, name: Optional[str]) -> Optional[bytes]:
        if not self.has(name):
            return None
        member = self._index[Path(name).name.lower()]
        return self._zip.read(member) if self._zip else Path(member).read_bytes()


def media_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
