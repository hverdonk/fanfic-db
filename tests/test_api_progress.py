import pytest
from fastapi import HTTPException

from app.calibre import CalibreError, CalibreImportResult
from app.db import Database
from app.downloader import DownloadResult
from app.main import AddRequest, _download_and_import, add_work_async, work_status


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


class FakeRequest:
    def url_for(self, route_name, **path_params):
        assert route_name == "work_status"
        return f"http://testserver/status/{path_params['work_id']}"


def test_async_add_returns_status_url_without_running_download(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    background_tasks = FakeBackgroundTasks()

    response = add_work_async(
        AddRequest(url="https://archiveofourown.org/works/123"),
        background_tasks,
        FakeRequest(),
        db,
    )

    assert response.ok is True
    assert response.work_id == "123"
    assert response.status == "pending"
    assert response.message == "Download queued"
    assert response.progress == 5
    assert response.status_url == "http://testserver/status/123"
    assert len(background_tasks.tasks) == 1


def test_status_endpoint_reports_progress(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.upsert_work(
        work_id="123",
        source_url="https://archiveofourown.org/works/123",
        status="importing",
        message="Importing into Calibre",
    )

    response = work_status("123", db)

    assert response.ok is False
    assert response.done is False
    assert response.status == "importing"
    assert response.progress == 80


def test_status_endpoint_marks_complete_work_done(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.upsert_work(
        work_id="123",
        source_url="https://archiveofourown.org/works/123",
        status="ok",
        calibre_book_id=7,
        title="Example",
        author="Author",
        message="added",
    )

    response = work_status("123", db)

    assert response.ok is True
    assert response.done is True
    assert response.progress == 100
    assert response.title == "Example"
    assert response.calibre_book_id == 7


def test_add_endpoint_expands_series_urls(tmp_path, monkeypatch):
    from app.main import AddResponse, add_work

    db = Database(tmp_path / "test.sqlite3")
    calls = []

    monkeypatch.setattr(
        "app.main.resolve_series_work_urls",
        lambda _url: [
            ("https://archiveofourown.org/works/111", "111"),
            ("https://archiveofourown.org/works/222", "222"),
        ],
    )

    def fake_download_and_import(db, canonical_url, work_id, existing_book_id, action):
        calls.append((canonical_url, work_id, existing_book_id, action))
        return AddResponse(
            ok=True,
            action=action,
            work_id=work_id,
            title=f"Work {work_id}",
            author="Author",
            calibre_book_id=int(work_id),
            source_url=canonical_url,
        )

    monkeypatch.setattr("app.main._download_and_import", fake_download_and_import)

    response = add_work(AddRequest(url="https://archiveofourown.org/series/3073359"), db)

    assert response.ok is True
    assert response.action == "series_added"
    assert response.series_id == "3073359"
    assert response.source_url == "https://archiveofourown.org/series/3073359"
    assert response.work_count == 2
    assert response.added == 2
    assert response.updated == 0
    assert [work.work_id for work in response.works] == ["111", "222"]
    assert calls == [
        ("https://archiveofourown.org/works/111", "111", None, "added"),
        ("https://archiveofourown.org/works/222", "222", None, "added"),
    ]


def test_downloaded_epub_is_deleted_after_successful_import(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.sqlite3")
    epub = tmp_path / "downloads" / "ao3-123.epub"
    epub.parent.mkdir()
    epub.write_text("epub", encoding="utf-8")

    class FakeDownloader:
        def download(self, canonical_url, work_id, *, update=False):
            assert canonical_url == "https://archiveofourown.org/works/123"
            assert work_id == "123"
            assert update is False
            return DownloadResult(epub_path=epub)

    class FakeCalibre:
        def add_or_update(self, epub_path, existing_book_id=None, source_url=None):
            assert epub_path == epub
            assert epub.exists()
            return CalibreImportResult(book_id=9, title="Example", author="Author")

    monkeypatch.setattr("app.main.FanFicFareDownloader.from_env", staticmethod(lambda: FakeDownloader()))
    monkeypatch.setattr("app.main.CalibreLibrary.from_env", staticmethod(lambda: FakeCalibre()))

    response = _download_and_import(db, "https://archiveofourown.org/works/123", "123", None, "added")

    assert response.ok is True
    assert response.calibre_book_id == 9
    assert not epub.exists()


def test_downloaded_epub_is_kept_when_import_fails(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.sqlite3")
    epub = tmp_path / "downloads" / "ao3-123.epub"
    epub.parent.mkdir()
    epub.write_text("epub", encoding="utf-8")

    class FakeDownloader:
        def download(self, canonical_url, work_id, *, update=False):
            return DownloadResult(epub_path=epub)

    class FakeCalibre:
        def add_or_update(self, epub_path, existing_book_id=None, source_url=None):
            raise CalibreError("import failed")

    monkeypatch.setattr("app.main.FanFicFareDownloader.from_env", staticmethod(lambda: FakeDownloader()))
    monkeypatch.setattr("app.main.CalibreLibrary.from_env", staticmethod(lambda: FakeCalibre()))

    with pytest.raises(HTTPException):
        _download_and_import(db, "https://archiveofourown.org/works/123", "123", None, "added")

    assert epub.exists()


@pytest.mark.parametrize("cleanup_error", [PermissionError("denied"), OSError("filesystem error")])
def test_cleanup_failure_preserves_success(tmp_path, monkeypatch, caplog, cleanup_error):
    from pathlib import Path

    db = Database(tmp_path / "test.sqlite3")
    epub = tmp_path / "ao3-123.epub"
    epub.write_text("epub")
    monkeypatch.setattr(
        "app.main.FanFicFareDownloader.download",
        lambda *args, **kwargs: DownloadResult(epub_path=epub),
    )
    monkeypatch.setattr(
        "app.main.CalibreLibrary.add_or_update",
        lambda *args, **kwargs: CalibreImportResult(9, "Example", "Author"),
    )
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))

    def fail_cleanup(path, **kwargs):
        record = db.get_work("123")
        assert record.status == "ok"
        assert record.calibre_book_id == 9
        raise cleanup_error

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    response = _download_and_import(db, "https://archiveofourown.org/works/123", "123", None, "added")

    assert response.ok is True
    assert response.calibre_book_id == 9
    assert db.get_work("123").status == "ok"
    assert epub.exists()
    assert "cleanup failed" in caplog.text


def test_epub_is_kept_if_success_cannot_be_saved(tmp_path, monkeypatch):
    import sqlite3

    db = Database(tmp_path / "test.sqlite3")
    epub = tmp_path / "ao3-123.epub"
    epub.write_text("epub")
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(
        "app.main.FanFicFareDownloader.download",
        lambda *args, **kwargs: DownloadResult(epub_path=epub),
    )
    monkeypatch.setattr(
        "app.main.CalibreLibrary.add_or_update",
        lambda *args, **kwargs: CalibreImportResult(9, "Example", "Author"),
    )
    original_upsert = db.upsert_work

    def fail_success_write(**kwargs):
        if kwargs["status"] == "ok":
            raise sqlite3.OperationalError("database is locked")
        return original_upsert(**kwargs)

    monkeypatch.setattr(db, "upsert_work", fail_success_write)
    with pytest.raises(sqlite3.OperationalError):
        _download_and_import(db, "https://archiveofourown.org/works/123", "123", None, "added")

    assert epub.exists()
