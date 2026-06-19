import pytest

from app.db import Database
from app.main import AddRequest, add_form, add_work_async, queued, work_status


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


class FakeRequest:
    def url_for(self, route_name, **path_params):
        assert route_name == "work_status"
        return f"http://testserver/status/{path_params['work_id']}"


class FakeFormRequest(FakeRequest):
    def __init__(self, url):
        self.url = url

    async def form(self):
        return {"url": self.url}


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


@pytest.mark.anyio
async def test_add_form_queues_work_and_redirects(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.sqlite3")
    background_tasks = FakeBackgroundTasks()

    monkeypatch.setattr("app.main._run_add_job", lambda *args: None)

    response = await add_form(
        FakeFormRequest("https://archiveofourown.org/works/123"),
        background_tasks,
        db,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/queued/123"
    record = db.get_work("123")
    assert record is not None
    assert record.status == "pending"
    assert record.message == "Download queued"
    assert len(background_tasks.tasks) == 1


def test_queued_page_shows_progress(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.upsert_work(
        work_id="123",
        source_url="https://archiveofourown.org/works/123",
        status="downloading",
        message="Downloading EPUB",
    )

    response = queued("123", db)

    assert response.status_code == 200
    assert "Status: downloading" in response.body.decode()
    assert 'value="35"' in response.body.decode()
    assert "Downloading EPUB" in response.body.decode()
