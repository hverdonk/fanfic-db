import sqlite3
import subprocess

from app.calibre import CalibreLibrary


def test_add_or_update_reuses_existing_calibre_identifier(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    epub = tmp_path / "work.epub"
    epub.write_text("epub")

    with sqlite3.connect(library / "metadata.db") as conn:
        conn.execute("CREATE TABLE identifiers (book INTEGER, type TEXT, val TEXT)")
        conn.execute(
            "INSERT INTO identifiers (book, type, val) VALUES (?, ?, ?)",
            (41, "url", "https://archiveofourown.org/works/123"),
        )
        conn.execute(
            "INSERT INTO identifiers (book, type, val) VALUES (?, ?, ?)",
            (42, "url", "https://archiveofourown.org/works/123"),
        )

    calls = []

    def fake_run(cmd, *, allow_replace=False):
        calls.append((cmd, allow_replace))
        if cmd[:2] == ["calibredb", "show_metadata"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="<dc:title>Updated Work</dc:title><dc:creator>Author</dc:creator>",
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(CalibreLibrary, "_run", staticmethod(fake_run))

    result = CalibreLibrary(library, tags=["ao3"]).add_or_update(
        epub,
        source_url="https://archiveofourown.org/works/123",
    )

    assert result.book_id == 42
    assert result.title == "Updated Work"
    assert result.author == "Author"
    assert not any(cmd[1] == "add" for cmd, _allow_replace in calls)
    assert (
        ["calibredb", "add_format", "42", str(epub), "--with-library", str(library), "--dont-replace"],
        True,
    ) in calls
