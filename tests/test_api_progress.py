from app.db import Database
from app.main import AddRequest, add_work_async, work_status


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
