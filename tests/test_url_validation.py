import pytest

from unittest.mock import patch

from app.downloader import normalize_ao3_series_url, normalize_ao3_url, resolve_series_work_urls


def test_normalizes_work_url():
    canonical, work_id = normalize_ao3_url("https://archiveofourown.org/works/123456?view_full_work=true#main")
    assert canonical == "https://archiveofourown.org/works/123456"
    assert work_id == "123456"


def test_allows_www_host():
    canonical, work_id = normalize_ao3_url("https://www.archiveofourown.org/works/42/chapters/99")
    assert canonical == "https://archiveofourown.org/works/42"
    assert work_id == "42"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/works/123",
        "https://evilarchiveofourown.org/works/123",
        "ftp://archiveofourown.org/works/123",
        "https://archiveofourown.org/users/name",
    ],
)
def test_rejects_invalid_urls(url):
    with pytest.raises(ValueError):
        normalize_ao3_url(url)


def test_normalizes_series_url():
    canonical, series_id = normalize_ao3_series_url("https://archiveofourown.org/series/3073359?show_comments=true#main")
    assert canonical == "https://archiveofourown.org/series/3073359"
    assert series_id == "3073359"


def test_resolves_series_work_urls_from_ao3_html():
    pages = {
        "https://archiveofourown.org/series/3073359": """
            <html><body>
              <a href="/works/111/chapters/1">First</a>
              <a href="https://archiveofourown.org/works/222?view_full_work=true">Second</a>
              <a href="/series/3073359?page=2" rel="next">Next</a>
            </body></html>
        """,
        "https://archiveofourown.org/series/3073359?page=2": """
            <html><body>
              <a href="/works/222">Duplicate</a>
              <a href="/works/333">Third</a>
            </body></html>
        """,
    }

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self.body.encode("utf-8")

    def fake_urlopen(request, timeout):
        assert timeout == 30
        return FakeResponse(pages[request.full_url])

    with patch("app.downloader.urlopen", side_effect=fake_urlopen):
        assert resolve_series_work_urls("https://archiveofourown.org/series/3073359") == [
            ("https://archiveofourown.org/works/111", "111"),
            ("https://archiveofourown.org/works/222", "222"),
            ("https://archiveofourown.org/works/333", "333"),
        ]
