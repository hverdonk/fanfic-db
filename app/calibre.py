from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path


class CalibreError(RuntimeError):
    pass


@dataclass
class CalibreImportResult:
    book_id: int
    title: str | None
    author: str | None


class CalibreLibrary:
    def __init__(self, library_path: str | Path, tags: list[str] | None = None):
        self.library_path = Path(library_path)
        self.tags = tags or ["ao3", "fanficfare", "unread"]

    @classmethod
    def from_env(cls) -> "CalibreLibrary":
        tags = [tag.strip() for tag in os.getenv("CALIBRE_TAGS", "ao3,fanficfare,unread").split(",") if tag.strip()]
        return cls(os.getenv("CALIBRE_LIBRARY_PATH", "/library"), tags)

    def ensure_ready(self) -> None:
        if not self.library_path.exists():
            raise CalibreError("Calibre library path does not exist")
        if not os.access(self.library_path, os.W_OK):
            raise CalibreError("Calibre library path is not writable")

    def add_or_update(
        self,
        epub_path: Path,
        existing_book_id: int | None = None,
        source_url: str | None = None,
    ) -> CalibreImportResult:
        self.ensure_ready()
        book_id = existing_book_id or (self._find_book_by_url(source_url) if source_url else None)
        if book_id:
            self._add_or_replace_format(book_id, epub_path)
            self._set_tags(book_id)
            meta = self._metadata(book_id)
            return CalibreImportResult(book_id, meta.get("title"), _first_author(meta))

        result = self._run(["calibredb", "add", str(epub_path), "--with-library", str(self.library_path), "--duplicates"])
        book_id = _parse_added_book_id(result.stdout)
        if book_id is None:
            raise CalibreError("Could not determine Calibre book ID after import")
        self._set_tags(book_id)
        meta = self._metadata(book_id)
        return CalibreImportResult(book_id, meta.get("title"), _first_author(meta))

    def _set_tags(self, book_id: int) -> None:
        if self.tags:
            self._run(["calibredb", "set_metadata", str(book_id), "--with-library", str(self.library_path), "--field", f"tags:{','.join(self.tags)}"])

    def _add_or_replace_format(self, book_id: int, epub_path: Path) -> None:
        self._run(["calibredb", "add_format", str(book_id), str(epub_path), "--with-library", str(self.library_path), "--dont-replace"], allow_replace=True)

    def _metadata(self, book_id: int) -> dict:
        result = self._run(["calibredb", "show_metadata", str(book_id), "--with-library", str(self.library_path), "--as-opf"])
        return _metadata_from_opf(result.stdout)

    def _find_book_by_url(self, source_url: str | None) -> int | None:
        if not source_url:
            return None
        metadata_path = self.library_path / "metadata.db"
        if not metadata_path.exists():
            raise CalibreError("Calibre metadata database does not exist")
        try:
            with sqlite3.connect(metadata_path) as conn:
                row = conn.execute(
                    """
                    SELECT book
                    FROM identifiers
                    WHERE type = 'url' AND val = ?
                    ORDER BY book DESC
                    LIMIT 1
                    """,
                    (source_url,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise CalibreError(f"Could not search Calibre identifiers: {exc}") from exc
        return int(row[0]) if row else None

    @staticmethod
    def _run(cmd: list[str], *, allow_replace: bool = False) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            if allow_replace and "already has format" in result.stderr.lower():
                replace_cmd = cmd[:-1]
                replace_cmd.append("--replace")
                result = subprocess.run(replace_cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise CalibreError(_safe_error("Calibre command failed", result.stderr))
        return result


def _parse_added_book_id(stdout: str) -> int | None:
    matches = re.findall(r"Added book ids?:\s*([0-9, ]+)", stdout, flags=re.IGNORECASE)
    if not matches:
        return None
    return int(matches[-1].split(",")[0].strip())


def _metadata_from_opf(opf: str) -> dict:
    # calibredb has no stable JSON output for show_metadata on all packaged versions.
    title = _tag_text(opf, "dc:title")
    creator = _tag_text(opf, "dc:creator")
    return {"title": title, "authors": [creator] if creator else []}


def _tag_text(xml: str, tag: str) -> str | None:
    match = re.search(rf"<{re.escape(tag)}(?:\s[^>]*)?>(.*?)</{re.escape(tag)}>", xml, flags=re.DOTALL)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _first_author(meta: dict) -> str | None:
    authors = meta.get("authors")
    if isinstance(authors, list) and authors:
        return str(authors[0])
    return None


def _safe_error(prefix: str, stderr: str) -> str:
    details = stderr.strip().splitlines()[-1] if stderr.strip() else "no details"
    return f"{prefix}: {details}"
