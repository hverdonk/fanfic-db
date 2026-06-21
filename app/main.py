from __future__ import annotations

import os

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .calibre import CalibreError, CalibreLibrary
from .db import Database
from .downloader import (
    DownloadError,
    FanFicFareDownloader,
    normalize_ao3_series_url,
    normalize_ao3_url,
    resolve_series_work_urls,
)


class AddRequest(BaseModel):
    url: str


class AddResponse(BaseModel):
    ok: bool
    action: str
    work_id: str
    title: str | None
    author: str | None
    calibre_book_id: int | None
    source_url: str


class AddSeriesResponse(BaseModel):
    ok: bool
    action: str
    series_id: str
    source_url: str
    work_count: int
    added: int
    updated: int
    works: list[AddResponse]


class QueueResponse(BaseModel):
    ok: bool
    action: str
    work_id: str
    source_url: str
    status_url: str
    status: str
    message: str | None
    progress: int


class WorkStatusResponse(BaseModel):
    ok: bool
    work_id: str
    title: str | None
    author: str | None
    calibre_book_id: int | None
    source_url: str
    status: str
    message: str | None
    progress: int
    done: bool


def get_db() -> Database:
    return Database(os.getenv("DATABASE_PATH", "/config/fanfic-db.sqlite3"))


def require_token(authorization: str | None = Header(default=None)) -> None:
    token = os.getenv("API_TOKEN")
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid API token")


app = FastAPI(title="Send to Library")


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Database = Depends(get_db)) -> str:
    token_enabled = bool(os.getenv("API_TOKEN"))
    rows = "\n".join(
        f"<tr><td>{r.work_id}</td><td>{_esc(r.title or '')}</td><td>{_esc(r.author or '')}</td><td>{r.status}</td><td>{_esc(r.message or '')}</td></tr>"
        for r in db.recent()
    )
    return f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Send to Library</title>
      <style>
        body {{ font-family: system-ui, sans-serif; max-width: 860px; margin: 0 auto; padding: 1rem; }}
        form {{ display: grid; gap: .75rem; margin: 1rem 0 2rem; }}
        input, button {{ font: inherit; padding: .8rem; }}
        button {{ cursor: pointer; }}
        table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
        th, td {{ border-bottom: 1px solid #ddd; padding: .5rem; text-align: left; vertical-align: top; }}
        .note {{ color: #555; font-size: .9rem; }}
      </style>
    </head>
    <body>
      <h1>Send to Library</h1>
      <form method="post" action="/add-form">
        <input name="url" type="url" placeholder="https://archiveofourown.org/works/123456" required>
        <button type="submit">Add AO3 work</button>
      </form>
      <p class="note">API token is {"enabled" if token_enabled else "disabled"}. Keep this service on a private network such as Tailscale.</p>
      <h2>Recent downloads</h2>
      <table>
        <thead><tr><th>AO3 ID</th><th>Title</th><th>Author</th><th>Status</th><th>Message</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </body>
    </html>
    """


@app.post("/add", response_model=AddResponse | AddSeriesResponse, dependencies=[Depends(require_token)])
def add_work(payload: AddRequest, db: Database = Depends(get_db)) -> AddResponse | AddSeriesResponse:
    return _add_url(payload.url, db)


@app.post(
    "/add-async",
    response_model=QueueResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_token)],
)
def add_work_async(
    payload: AddRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Database = Depends(get_db),
) -> QueueResponse:
    canonical_url, work_id = _normalize_or_400(payload.url)
    existing = db.get_work(work_id)
    action = "updated" if existing and existing.calibre_book_id else "added"
    record = db.upsert_work(work_id=work_id, source_url=canonical_url, status="pending", message="Download queued")

    background_tasks.add_task(
        _run_add_job,
        canonical_url,
        work_id,
        existing.calibre_book_id if existing else None,
        action,
    )

    return QueueResponse(
        ok=True,
        action=action,
        work_id=work_id,
        source_url=record.source_url,
        status_url=str(request.url_for("work_status", work_id=work_id)),
        status=record.status,
        message=record.message,
        progress=_progress_for(record.status),
    )


@app.get("/status/{work_id}", response_model=WorkStatusResponse, dependencies=[Depends(require_token)])
def work_status(work_id: str, db: Database = Depends(get_db)) -> WorkStatusResponse:
    record = db.get_work(work_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown AO3 work ID")
    return _status_response(record)


@app.post("/add-form", response_class=HTMLResponse)
async def add_form(request: Request, db: Database = Depends(get_db)) -> HTMLResponse:
    form = await request.form()
    try:
        _add_url(str(form.get("url", "")), db)
        return HTMLResponse('<meta http-equiv="refresh" content="0; url=/" />', status_code=303)
    except HTTPException as exc:
        return HTMLResponse(f"<p>{_esc(str(exc.detail))}</p><p><a href='/'>Back</a></p>", status_code=exc.status_code)


def _add_url(raw_url: str, db: Database) -> AddResponse | AddSeriesResponse:
    try:
        canonical_url, work_id = normalize_ao3_url(raw_url)
    except ValueError:
        return _add_series_url(raw_url, db)

    return _add_work_url(db, canonical_url, work_id)


def _add_series_url(raw_url: str, db: Database) -> AddSeriesResponse:
    try:
        canonical_url, series_id = normalize_ao3_series_url(raw_url)
        work_urls = resolve_series_work_urls(canonical_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="URL must be an AO3 work or series URL") from exc
    except DownloadError as exc:
        status_code = 401 if exc.auth_failed else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    works = [_add_work_url(db, work_url, work_id) for work_url, work_id in work_urls]
    return AddSeriesResponse(
        ok=True,
        action="series_added",
        series_id=series_id,
        source_url=canonical_url,
        work_count=len(works),
        added=sum(1 for work in works if work.action == "added"),
        updated=sum(1 for work in works if work.action == "updated"),
        works=works,
    )


def _add_work_url(db: Database, canonical_url: str, work_id: str) -> AddResponse:
    existing = db.get_work(work_id)
    action = "updated" if existing and existing.calibre_book_id else "added"
    db.upsert_work(work_id=work_id, source_url=canonical_url, status="pending", message="Download queued")
    return _download_and_import(db, canonical_url, work_id, existing.calibre_book_id if existing else None, action)


def _normalize_or_400(raw_url: str) -> tuple[str, str]:
    try:
        return normalize_ao3_url(raw_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _download_and_import(
    db: Database,
    canonical_url: str,
    work_id: str,
    existing_book_id: int | None,
    action: str,
) -> AddResponse:
    try:
        db.upsert_work(work_id=work_id, source_url=canonical_url, status="downloading", message="Downloading EPUB")
        downloader = FanFicFareDownloader.from_env()
        downloaded = downloader.download(canonical_url, work_id, update=bool(existing_book_id))
        db.upsert_work(work_id=work_id, source_url=canonical_url, status="importing", message="Importing into Calibre")
        calibre = CalibreLibrary.from_env()
        imported = calibre.add_or_update(downloaded.epub_path, existing_book_id, canonical_url)
    except DownloadError as exc:
        db.upsert_work(work_id=work_id, source_url=canonical_url, status="error", message=str(exc))
        status_code = 401 if exc.auth_failed else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except CalibreError as exc:
        db.upsert_work(work_id=work_id, source_url=canonical_url, status="error", message=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except PermissionError as exc:
        message = "Permission error on mounted volume"
        db.upsert_work(work_id=work_id, source_url=canonical_url, status="error", message=message)
        raise HTTPException(status_code=500, detail=message) from exc

    record = db.upsert_work(
        work_id=work_id,
        source_url=canonical_url,
        status="ok",
        calibre_book_id=imported.book_id,
        title=imported.title,
        author=imported.author,
        message=action,
    )
    return AddResponse(
        ok=True,
        action=action,
        work_id=work_id,
        title=record.title,
        author=record.author,
        calibre_book_id=record.calibre_book_id,
        source_url=record.source_url,
    )


def _run_add_job(
    canonical_url: str,
    work_id: str,
    existing_book_id: int | None,
    action: str,
) -> None:
    db = get_db()
    try:
        _download_and_import(db, canonical_url, work_id, existing_book_id, action)
    except HTTPException:
        pass


def _status_response(record) -> WorkStatusResponse:
    return WorkStatusResponse(
        ok=record.status == "ok",
        work_id=record.work_id,
        title=record.title,
        author=record.author,
        calibre_book_id=record.calibre_book_id,
        source_url=record.source_url,
        status=record.status,
        message=record.message,
        progress=_progress_for(record.status),
        done=record.status in {"ok", "error"},
    )


def _progress_for(status_value: str) -> int:
    return {
        "pending": 5,
        "downloading": 35,
        "importing": 80,
        "ok": 100,
        "error": 100,
    }.get(status_value, 0)


def _esc(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
