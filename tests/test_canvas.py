"""Replays real captured Canvas responses (tests/fixtures/canvas/) — no live
Canvas access needed to run these. Identifiers and page text are replaced
with placeholders; the shapes are Canvas's own."""

import json
from pathlib import Path

import httpx
import pytest
import respx

import canvas

FIXTURES = Path(__file__).parent / "fixtures" / "canvas"
BASE = canvas.CANVAS_API_URL


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    # No SM_CANVAS_URL, so _api_base() is CANVAS_API_URL (BASE above).
    monkeypatch.setenv("SM_CANVAS_TOKEN", "fake-token-for-tests")
    monkeypatch.delenv("SM_CANVAS_URL", raising=False)


@respx.mock
def test_list_courses():
    fx = load("courses")
    respx.get(f"{BASE}/courses").mock(return_value=httpx.Response(fx["status"], json=fx["body"]))
    courses = canvas.list_courses()
    assert len(courses) == len(fx["body"])
    assert courses[0]["id"] == fx["body"][0]["id"]


@respx.mock
def test_get_page():
    fx = load("page")
    respx.get(f"{BASE}/courses/51113/pages/academic-integrity-and-credibility").mock(
        return_value=httpx.Response(fx["status"], json=fx["body"])
    )
    page = canvas.get_page(51113, "academic-integrity-and-credibility")
    assert page["title"] == fx["body"]["title"]


@respx.mock
def test_course_structure_success():
    fx = load("course_structure_ok")
    respx.get(f"{BASE}/courses/55710/modules").mock(return_value=httpx.Response(fx["status"], json=fx["body"]))
    modules = canvas.get_course_structure(55710)
    assert len(modules) == len(fx["body"])


@respx.mock
def test_course_structure_403_degrades_to_empty():
    """get_course_structure can 403 for an entire
    course/section — real case, course 55709 (one of the two 18654-SV IDs)."""
    fx = load("course_structure_403")
    respx.get(f"{BASE}/courses/55709/modules").mock(return_value=httpx.Response(fx["status"], json=fx["body"]))
    modules = canvas.get_course_structure(55709)
    assert modules == []


@respx.mock
def test_configured_base_url_is_used_instead_of_default(monkeypatch):
    """A school other than CMU must actually be reachable."""
    monkeypatch.setenv("SM_CANVAS_URL", "https://canvas.instructure.com")
    fx = load("courses")
    respx.get("https://canvas.instructure.com/api/v1/courses").mock(
        return_value=httpx.Response(fx["status"], json=fx["body"])
    )
    result = canvas.list_courses()
    assert len(result) == len(fx["body"])


@respx.mock
def test_list_course_files_403_degrades_to_empty():
    """The bulk Files-tab listing 403s under a
    student token — real case, course 56350."""
    fx = load("list_course_files_403")
    respx.get(f"{BASE}/courses/56350/files").mock(return_value=httpx.Response(fx["status"], json=fx["body"]))
    files = canvas.list_course_files(56350)
    assert files == []


@respx.mock
def test_list_announcements_reads_every_one_not_just_the_last_two_weeks():
    """/announcements returns only the last 14 days by default (56350 showed
    2 of its 13); discussion_topics with only_announcements returns them all."""
    route = respx.get(f"{BASE}/courses/56350/discussion_topics").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "title": "Class This Tuesday", "message": "<p>Room 101</p>"}])
    )
    assert [a["id"] for a in canvas.list_announcements(56350)] == [1]
    assert route.calls[0].request.url.params["only_announcements"] == "true"


@respx.mock
def test_list_discussions_leaves_announcements_out():
    route = respx.get(f"{BASE}/courses/55016/discussion_topics").mock(
        return_value=httpx.Response(200, json=[{"id": 838803, "title": "Updates", "message": "<p>Post weekly</p>"}])
    )
    assert [d["title"] for d in canvas.list_discussions(55016)] == ["Updates"]
    assert "only_announcements" not in route.calls[0].request.url.params


@respx.mock
def test_discussion_listing_403_degrades_to_empty():
    respx.get(f"{BASE}/courses/1/discussion_topics").mock(return_value=httpx.Response(403, json={}))
    assert canvas.list_discussions(1) == []
    assert canvas.list_announcements(1) == []


# --- Retries on transient failures -------------------------------------------
# A real sync lost a file to one "_ssl.c: The handshake operation timed out"
# on get_file; the same request succeeded moments later.


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(canvas.time, "sleep", slept.append)
    return slept


@respx.mock
def test_transport_error_is_retried_then_succeeds(no_sleep):
    route = respx.get(f"{BASE}/files/1").mock(
        side_effect=[httpx.ConnectTimeout("handshake timed out"), httpx.Response(200, json={"id": 1})]
    )
    assert canvas.get_file(1) == {"id": 1}
    assert route.call_count == 2
    assert no_sleep == [1]


@respx.mock
def test_server_error_is_retried_then_succeeds(no_sleep):
    route = respx.get(f"{BASE}/files/1").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={"id": 1})])
    assert canvas.get_file(1) == {"id": 1}
    assert route.call_count == 2


@respx.mock
def test_persistent_transport_error_raises_after_three_attempts(no_sleep):
    route = respx.get(f"{BASE}/files/1").mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(httpx.ReadTimeout):
        canvas.get_file(1)
    assert route.call_count == 3


@respx.mock
def test_persistent_server_error_raises_after_three_attempts(no_sleep):
    route = respx.get(f"{BASE}/files/1").mock(return_value=httpx.Response(502))
    with pytest.raises(httpx.HTTPStatusError):
        canvas.get_file(1)
    assert route.call_count == 3


@respx.mock
def test_403_is_not_retried(no_sleep):
    route = respx.get(f"{BASE}/files/1").mock(return_value=httpx.Response(403))
    assert canvas.get_file(1) is None
    assert route.call_count == 1 and no_sleep == []


@respx.mock
def test_download_follows_redirect_and_retries_transport_errors(no_sleep):
    respx.get("https://canvas.example/files/9/download").mock(
        side_effect=[
            httpx.ConnectError("reset"),
            httpx.Response(302, headers={"location": "https://storage.example/f9.pdf"}),
        ]
    )
    respx.get("https://storage.example/f9.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-1.7"))
    assert canvas.download("https://canvas.example/files/9/download") == b"%PDF-1.7"


@respx.mock
def test_download_raises_on_an_error_status_instead_of_returning_the_error_page(no_sleep):
    # The old httpx.get call never checked the status: a 403 HTML page would
    # have been written to disk and handed to the PDF extractor.
    respx.get("https://canvas.example/files/9/download").mock(return_value=httpx.Response(403, text="<html>denied</html>"))
    with pytest.raises(httpx.HTTPStatusError):
        canvas.download("https://canvas.example/files/9/download")


# --- plugin settings ----------------------------------------------------------


def test_missing_token_is_a_canvas_error(monkeypatch):
    monkeypatch.setenv("SM_CANVAS_TOKEN", "")

    with pytest.raises(canvas.CanvasError):
        canvas._token()


def test_plugin_url_is_normalized(monkeypatch):
    monkeypatch.setenv("SM_CANVAS_URL", "canvas.example.edu/api/v1/")

    assert canvas._api_base() == "https://canvas.example.edu/api/v1"


@pytest.mark.parametrize(
    "raw, origin",
    [
        ("canvas.cmu.edu", "https://canvas.cmu.edu"),
        ("https://canvas.cmu.edu/", "https://canvas.cmu.edu"),
        ("https://canvas.cmu.edu/api/v1", "https://canvas.cmu.edu"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("   ", ""),
    ],
)
def test_normalize_base_url(raw, origin):
    assert canvas.normalize_base_url(raw) == origin


def test_empty_url_setting_means_the_default(monkeypatch):
    monkeypatch.setenv("SM_CANVAS_URL", "")

    assert canvas.origin() == canvas.DEFAULT_ORIGIN
