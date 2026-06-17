from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


class DownloadError(RuntimeError):
    def __init__(self, message: str, *, auth_failed: bool = False):
        super().__init__(message)
        self.auth_failed = auth_failed


@dataclass
class DownloadResult:
    epub_path: Path
    title: str | None = None
    author: str | None = None


AO3_HOSTS = {"archiveofourown.org", "www.archiveofourown.org"}
WORK_PATH_RE = re.compile(r"^/works/(\d+)")
SERIES_PATH_RE = re.compile(r"^/series/(\d+)")


def normalize_ao3_url(raw_url: str) -> tuple[str, str]:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL must use http or https")
    if parsed.hostname not in AO3_HOSTS:
        raise ValueError("URL must be from archiveofourown.org")
    match = WORK_PATH_RE.match(parsed.path)
    if not match:
        raise ValueError("URL must be an AO3 work URL")
    work_id = match.group(1)
    return f"https://archiveofourown.org/works/{work_id}", work_id


def normalize_ao3_series_url(raw_url: str) -> tuple[str, str]:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL must use http or https")
    if parsed.hostname not in AO3_HOSTS:
        raise ValueError("URL must be from archiveofourown.org")
    match = SERIES_PATH_RE.match(parsed.path)
    if not match:
        raise ValueError("URL must be an AO3 series URL")
    series_id = match.group(1)
    return f"https://archiveofourown.org/series/{series_id}", series_id


def resolve_series_work_urls(series_url: str) -> list[tuple[str, str]]:
    canonical_url, _series_id = normalize_ao3_series_url(series_url)
    urls_by_id: dict[str, str] = {}
    next_url: str | None = canonical_url
    seen_pages: set[str] = set()

    while next_url:
        if next_url in seen_pages:
            raise DownloadError("AO3 series pagination loop detected")
        if len(seen_pages) >= 100:
            raise DownloadError("AO3 series has too many pages to process safely")
        seen_pages.add(next_url)

        parser = _SeriesPageParser(next_url)
        parser.feed(_fetch_ao3_page(next_url))
        for work_url, work_id in parser.work_urls:
            urls_by_id.setdefault(work_id, work_url)
        next_url = parser.next_url

    if not urls_by_id:
        raise DownloadError("AO3 series page did not contain any works")
    return [(work_url, work_id) for work_id, work_url in urls_by_id.items()]


def _fetch_ao3_page(url: str) -> str:
    request = Request(url, headers={"User-Agent": "fanfic-db/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise DownloadError("Failed to fetch AO3 series page") from exc


class _SeriesPageParser(HTMLParser):
    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.work_urls: list[tuple[str, str]] = []
        self.next_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = {name: value or "" for name, value in attrs}
        href = attr_map.get("href", "")
        if not href:
            return

        absolute_url = urljoin(self.page_url, href)
        parsed = urlparse(absolute_url)
        if parsed.hostname not in AO3_HOSTS:
            return

        work_match = WORK_PATH_RE.match(parsed.path)
        if work_match:
            work_id = work_match.group(1)
            self.work_urls.append((f"https://archiveofourown.org/works/{work_id}", work_id))
            return

        series_match = SERIES_PATH_RE.match(parsed.path)
        rel_values = {value.strip().lower() for value in attr_map.get("rel", "").split()}
        if series_match and "next" in rel_values:
            self.next_url = absolute_url


def classify_fanficfare_error(stderr: str) -> DownloadError:
    lowered = stderr.lower()
    auth_markers = [
        "login",
        "logged in",
        "restricted",
        "not authorized",
        "session",
        "password",
        "adult",
    ]
    if any(marker in lowered for marker in auth_markers):
        return DownloadError("AO3 login required or session expired. Refresh the cookie file if configured.", auth_failed=True)
    return DownloadError("FanFicFare failed to download the work")


class FanFicFareDownloader:
    def __init__(
        self,
        download_dir: str | Path,
        *,
        auth_mode: str = "none",
        cookie_file: str | Path | None = None,
        username_file: str | Path | None = None,
        password_file: str | Path | None = None,
        delay_seconds: float = 2.0,
    ):
        self.download_dir = Path(download_dir)
        self.auth_mode = auth_mode
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self.username_file = Path(username_file) if username_file else None
        self.password_file = Path(password_file) if password_file else None
        self.delay_seconds = delay_seconds
        self.download_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "FanFicFareDownloader":
        return cls(
            os.getenv("DOWNLOAD_DIR", "/downloads"),
            auth_mode=os.getenv("AO3_AUTH_MODE", "none"),
            cookie_file=os.getenv("AO3_COOKIE_FILE", "/config/cookies.txt"),
            username_file=os.getenv("AO3_USERNAME_FILE"),
            password_file=os.getenv("AO3_PASSWORD_FILE"),
            delay_seconds=float(os.getenv("DOWNLOAD_DELAY_SECONDS", "2")),
        )

    def build_command(self, url: str, epub_path: Path) -> list[str]:
        cmd = [
            "fanficfare",
            "--non-interactive",
            "--format=epub",
            "--option",
            f"output_filename={epub_path.name}",
        ]

        if self.auth_mode == "cookies":
            if not self.cookie_file or not self.cookie_file.exists():
                raise DownloadError("AO3 login required or session expired. Refresh the cookie file if configured.", auth_failed=True)
            cmd.extend(["--mozilla-cookies", str(self.cookie_file)])
        elif self.auth_mode == "credentials":
            username = _read_secret_file(self.username_file, "AO3 username")
            password = _read_secret_file(self.password_file, "AO3 password")
            cmd.extend(["--username", username, "--password", password])
        elif self.auth_mode != "none":
            raise DownloadError("Invalid AO3_AUTH_MODE; use none, cookies, or credentials")

        cmd.append(url)
        return cmd

    def download(self, canonical_url: str, work_id: str, *, update: bool = False) -> DownloadResult:
        epub_path = self.download_dir / f"ao3-{work_id}.epub"
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        cmd = self.build_command(canonical_url, epub_path)
        if update and epub_path.exists():
            cmd.insert(1, "--update-epub")

        result = subprocess.run(cmd, cwd=self.download_dir, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise classify_fanficfare_error(result.stderr)
        if not epub_path.exists():
            raise DownloadError("FanFicFare did not create an EPUB")
        return DownloadResult(epub_path=epub_path)


def _read_secret_file(path: Path | None, label: str) -> str:
    if not path or not path.exists():
        raise DownloadError(f"{label} secret file is missing")
    return path.read_text(encoding="utf-8").strip()
