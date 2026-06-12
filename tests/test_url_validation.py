import pytest

from app.downloader import normalize_ao3_url


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
