from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.downloader import DownloadError, FanFicFareDownloader


def test_public_download_without_auth(tmp_path):
    downloader = FanFicFareDownloader(tmp_path, auth_mode="none", delay_seconds=0)
    epub = tmp_path / "ao3-123.epub"

    def fake_run(cmd, **kwargs):
        epub.write_text("epub", encoding="utf-8")
        return Mock(returncode=0, stderr="", stdout="")

    with patch("app.downloader.subprocess.run", side_effect=fake_run) as run:
        result = downloader.download("https://archiveofourown.org/works/123", "123")

    assert result.epub_path == epub
    assert "--cookiefile" not in run.call_args.args[0]


def test_restricted_download_with_cookies(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File", encoding="utf-8")
    downloader = FanFicFareDownloader(tmp_path, auth_mode="cookies", cookie_file=cookie_file, delay_seconds=0)

    cmd = downloader.build_command("https://archiveofourown.org/works/123", tmp_path / "ao3-123.epub")

    assert "--cookiefile" in cmd
    assert str(cookie_file) in cmd


def test_restricted_download_failure_without_auth(tmp_path):
    downloader = FanFicFareDownloader(tmp_path, auth_mode="cookies", cookie_file=tmp_path / "missing.txt", delay_seconds=0)

    with pytest.raises(DownloadError, match="AO3 login required"):
        downloader.download("https://archiveofourown.org/works/123", "123")


def test_expired_auth_handling(tmp_path):
    downloader = FanFicFareDownloader(tmp_path, auth_mode="none", delay_seconds=0)

    with patch("app.downloader.subprocess.run", return_value=Mock(returncode=1, stderr="You must be logged in", stdout="")):
        with pytest.raises(DownloadError) as exc:
            downloader.download("https://archiveofourown.org/works/123", "123")

    assert exc.value.auth_failed is True
