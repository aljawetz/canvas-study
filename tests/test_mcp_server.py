"""MCP server for the Claude Code plugin (mcp_server.py).

In-process tests go through mcp's Client against the real server object, so
they check MCP responses, not just the Python functions. One test runs the
server as a subprocess over stdio, the way Claude Code starts it, and fails
on anything that is not protocol on stdout.
"""

import datetime
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import anyio
import numpy as np
import pytest
from mcp import Client, StdioServerParameters

import indexing
import mcp_server
import sync
from embeddings import EMBEDDING_DIM

SERVER = Path(__file__).resolve().parent.parent / "plugin" / "server"


@pytest.fixture(autouse=True)
def no_canvas_token(monkeypatch):
    """A search refreshes a stale course from Canvas when a token is set; a
    dev's own token must never send these tests to live Canvas."""
    monkeypatch.delenv("SM_CANVAS_TOKEN", raising=False)


COURSE = 55016


class _HashEmbedding:
    """Bag of words hashed into the real width: offline and deterministic.
    Scores stay under the similarity cutoff, so the keyword (BM25) hit is what
    comes back, through the real HybridSearcher."""

    def embed(self, text: str) -> list[float]:
        vec = np.zeros(EMBEDDING_DIM, dtype=np.float64)
        for token in re.findall(r"[a-z0-9]+", text.lower()) or ["empty"]:
            slot = 0
            for char in token:
                slot = (slot * 33 + ord(char)) % EMBEDDING_DIM
            vec[slot] += 1.0
        return (vec / np.linalg.norm(vec)).tolist()


_PAGES = [
    ("The final exam is worth 20 percent of the grade.", "syllabus", 2),
    ("Project teams may include at most five students.", "01 CourseInfoF26.pdf", 28),
    ("Late work loses ten percent per day.", "syllabus", 9),
]


def _add(home: Path, chunks, course_id: int = COURSE):
    indexing.add_nodes(chunks, home / "index.lancedb", f"course_{course_id}", embed=_HashEmbedding().embed)
    return indexing.open_table(home / "index.lancedb", f"course_{course_id}")


def _build_index(home: Path, course_id: int = COURSE, pages=_PAGES):
    chunks = [indexing.Chunk(text, {"source": source, "item_type": "file", "page": page, "slide": None}, f"file:{i}")
              for i, (text, source, page) in enumerate(pages)]
    return _add(home, chunks, course_id)


def _searcher(home: Path, table):
    calls = []

    def make(course_id):
        calls.append(course_id)
        return mcp_server.make_searcher(table, _HashEmbedding().embed)

    return mcp_server.Searcher(home, make_searcher=make), calls


def _call(server, name: str, arguments: dict | None = None) -> dict:
    async def go():
        async with Client(server) as client:
            result = await client.call_tool(name, arguments or {})
            assert not result.is_error, result
            return json.loads(result.content[0].text)

    return anyio.run(go)


def test_tools_and_instructions_are_listed():
    async def go():
        async with Client(mcp_server.build_server(Path("/nonexistent"), courses_for=list)) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            return tools, client.instructions

    tools, instructions = anyio.run(go)
    assert tools == {"list_courses", "search_course_material", "list_course_items", "list_assignments",
                     "get_grades", "sync_course", "sync_status"}
    assert "search_course_material before answering" in instructions
    assert "list_course_items" in instructions


def test_list_course_items_lists_the_saved_outline_with_a_modules_link(tmp_path, monkeypatch):
    import course_items

    monkeypatch.setenv("SM_CANVAS_URL", "https://canvas.example.edu")
    (tmp_path / "courses" / "7").mkdir(parents=True)
    conn = sync.open_manifest(tmp_path / "courses" / "7" / "manifest.db")
    course_items.save_outline(conn, [
        {"module": "Week 3: Elicitation", "section": "Readings", "title": "Interviews.pdf", "item_type": "file", "url": ""},
        {"module": "Week 4: Modeling", "section": "", "title": "Quiz 4", "item_type": "quiz", "url": ""},
    ])
    conn.close()
    server = mcp_server.build_server(tmp_path, courses_for=list)

    out = _call(server, "list_course_items", {"course_id": 7, "topic": "week 3"})

    assert "Interviews.pdf" in out["listing"] and "Quiz 4" not in out["listing"]
    assert out["url"] == "https://canvas.example.edu/courses/7/modules"
    quizzes = _call(server, "list_course_items", {"course_id": 7, "types": ["quiz"]})
    assert "Quiz 4" in quizzes["listing"] and "Interviews.pdf" not in quizzes["listing"]


def test_list_course_items_says_when_a_course_is_not_synced(tmp_path):
    out = _call(mcp_server.build_server(tmp_path, courses_for=list), "list_course_items", {"course_id": 8})

    assert "hasn't been synced" in out["listing"]


def test_search_returns_chunks_with_citation_labels(tmp_path):
    index = _build_index(tmp_path)
    searcher, _ = _searcher(tmp_path, index)
    server = mcp_server.build_server(tmp_path, searcher=searcher, courses_for=list)

    out = _call(server, "search_course_material", {"course_id": COURSE, "query": "late work penalty"})

    assert out["course_id"] == COURSE
    top = out["results"][0]
    assert {k: top[k] for k in ("n", "source", "text")} == {"n": 1, "source": "syllabus · p.9", "text": "Late work loses ten percent per day."}
    assert [r["n"] for r in out["results"]] == list(range(1, len(out["results"]) + 1))


def test_search_caps_k_and_builds_the_retriever_once(tmp_path):
    index = _build_index(tmp_path)
    searcher, calls = _searcher(tmp_path, index)

    first = searcher.search(COURSE, "percent", k=1)
    searcher.search(COURSE, "percent", k=50)

    assert len(first["results"]) == 1
    assert calls == [COURSE]


def test_search_returns_more_than_five_when_k_asks(tmp_path):
    # Eight chunks share "rubric", so FTS alone finds them all.
    pages = [(f"The rubric for lab {i} counts style and tests.", "rubric.pdf", i) for i in range(1, 9)]
    index = _build_index(tmp_path, pages=pages)
    searcher, _ = _searcher(tmp_path, index)

    assert len(searcher.search(COURSE, "rubric", k=7)["results"]) == 7
    assert len(searcher.search(COURSE, "rubric")["results"]) == mcp_server.DEFAULT_K


def test_search_returns_the_query_exactly_as_sent(tmp_path):
    # Relative dates ("tomorrow") are Claude's to resolve (skills/ask); the
    # server searches and reports the query unchanged.
    index = _build_index(tmp_path)
    searcher, _ = _searcher(tmp_path, index)

    out = searcher.search(COURSE, "what is due tomorrow")

    assert out["query"] == "what is due tomorrow"


def test_query_log_records_each_search_when_set(tmp_path, monkeypatch):
    index = _build_index(tmp_path)
    searcher, _ = _searcher(tmp_path, index)
    log = tmp_path / "queries.jsonl"

    searcher.search(COURSE, "late work")  # not logged: SM_QUERY_LOG unset
    monkeypatch.setenv("SM_QUERY_LOG", str(log))
    searcher.search(COURSE, "late work penalty")
    searcher.search(COURSE, "team size")

    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["query"] for e in entries] == ["late work penalty", "team size"]
    assert entries[0]["sources"][0] == "syllabus · p.9"
    assert entries[0]["texts"][0] == "Late work loses ten percent per day."
    assert all(e["course_id"] == COURSE and e["seconds"] >= 0 for e in entries)


def test_search_on_a_course_with_no_index_says_so(tmp_path):
    searcher = mcp_server.Searcher(tmp_path, make_searcher=lambda cid: pytest.fail("must not open an index"))

    out = searcher.search(999, "anything")

    assert out["results"] == []
    assert "not indexed on this machine" in out["note"]


def test_query_log_records_a_search_on_a_course_with_no_index(tmp_path, monkeypatch):
    log = tmp_path / "queries.jsonl"
    monkeypatch.setenv("SM_QUERY_LOG", str(log))
    searcher = mcp_server.Searcher(tmp_path, make_searcher=lambda cid: pytest.fail("must not open an index"))

    searcher.search(999, "anything")

    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert entries == [{"course_id": 999, "query": "anything", "sources": [], "texts": [], "seconds": 0.0}]


def test_search_with_no_match_returns_the_no_results_note(tmp_path):
    index = _build_index(tmp_path)
    searcher, _ = _searcher(tmp_path, index)

    out = searcher.search(COURSE, "zzzz qqqq")

    assert out["results"] == []
    assert out["note"] == "No matching course material found for this search."


def _canvas_course(cid, name, **extra):
    return {"id": cid, "name": name, **extra}


def test_list_courses_lists_canvas_courses_and_marks_the_indexed_ones(tmp_path):
    _build_index(tmp_path, COURSE)
    (tmp_path / "courses" / str(COURSE)).mkdir(parents=True)
    conn = sync.open_manifest(tmp_path / "courses" / str(COURSE) / "manifest.db")
    sync.mark_synced(conn, "file:1", "file", "a.pdf", "2026-10-01T00:00:00Z", "h1")
    conn.close()
    canvas_courses = [
        _canvas_course(COURSE, "Requirements"),
        _canvas_course(56350, "Advanced AI"),
        _canvas_course(52701, None, access_restricted_by_date=True),
    ]

    out = _call(mcp_server.build_server(tmp_path, courses_for=lambda: canvas_courses), "list_courses")

    assert out["courses"][0]["course_id"] == COURSE
    assert out["courses"][0]["indexed"] is True
    assert out["courses"][0]["last_synced_at"] is not None
    assert out["courses"][1] == {"course_id": 56350, "name": "Advanced AI", "indexed": False, "last_synced_at": None}
    assert len(out["courses"]) == 2  # the date-locked course is left out


def test_list_courses_keeps_an_indexed_course_canvas_no_longer_lists(tmp_path):
    _build_index(tmp_path, 56350)

    out = mcp_server.list_courses_result(tmp_path, courses_for=lambda: [_canvas_course(COURSE, "Requirements")])

    assert [(c["course_id"], c["indexed"]) for c in out["courses"]] == [(COURSE, False), (56350, True)]


def test_without_canvas_list_courses_shows_the_indexed_ones_and_says_why(tmp_path):
    _build_index(tmp_path, COURSE)

    def no_token():
        raise mcp_server.canvas.CanvasError("no token")

    out = mcp_server.list_courses_result(tmp_path, courses_for=no_token)

    assert [c["course_id"] for c in out["courses"]] == [COURSE]
    assert out["note"].startswith("No Canvas token is set up")


def test_list_courses_shows_a_course_sync_state(tmp_path):
    jobs = mcp_server.sync_jobs.SyncJobs(tmp_path, run=lambda cid, home: iter([{"done": True, "error": "boom"}]),
                                         ensure_model=lambda: None)
    jobs.start(56350, wait=True)

    out = mcp_server.list_courses_result(tmp_path, courses_for=lambda: [_canvas_course(56350, "Advanced AI")])

    assert out["courses"][0]["sync_state"] == "failed"


def test_stdio_subprocess_speaks_only_protocol_on_stdout(tmp_path, caplog):
    """Claude Code's view: launch the server, call both tools. mcp's stdio
    client logs every stdout line that is not JSON-RPC ("Failed to parse
    JSONRPC message") and skips it, so a stray print shows up in that log.
    No index here, so no model is loaded and no Canvas call is made."""
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER / "mcp_server.py")],
        # A closed local port: list_courses falls back without reaching a real Canvas.
        env={**os.environ, "SM_HOME": str(tmp_path), "SM_CANVAS_TOKEN": "x", "SM_CANVAS_URL": "http://127.0.0.1:9"},
        cwd=str(SERVER),
    )

    async def go():
        async with Client(params) as client:
            courses = await client.call_tool("list_courses", {})
            missing = await client.call_tool("search_course_material", {"course_id": 1, "query": "x"})
            return json.loads(courses.content[0].text), json.loads(missing.content[0].text)

    with caplog.at_level(logging.ERROR, logger="mcp.client.stdio"):
        courses, missing = anyio.run(go)
    assert [r.getMessage() for r in caplog.records if r.name == "mcp.client.stdio"] == []
    assert courses["courses"] == []
    assert courses["note"].startswith("Canvas could not be reached")
    assert "not indexed" in missing["note"]


# --- list_assignments --------------------------------------------------------

import httpx  # noqa: E402

import canvas  # noqa: E402

FUTURE = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
RAW_ASSIGNMENTS = [
    {"id": 11, "name": "Lab 3", "due_at": FUTURE, "points_possible": 50, "html_url": "https://c/a/11",
     "description": "<p>Write tests.</p>", "submission": {"workflow_state": "unsubmitted"},
     "rubric": [{"description": "Coverage", "points": 50, "ratings": []}], "use_rubric_for_grading": True},
]


from zoneinfo import ZoneInfo  # noqa: E402

LA = ZoneInfo("America/Los_Angeles")


def _assignments_server(tmp_path, fetch, zone=lambda: LA):
    return mcp_server.build_server(tmp_path, courses_for=list, assignments_for=fetch, zone_for=zone)


def test_list_assignments_returns_upcoming_work(tmp_path):
    out = _call(_assignments_server(tmp_path, lambda cid: RAW_ASSIGNMENTS), "list_assignments", {"course_id": COURSE})

    assert [a["name"] for a in out["assignments"]] == ["Lab 3"]
    assert out["days"] == 14


def test_list_assignments_with_an_id_returns_the_rubric(tmp_path):
    out = _call(_assignments_server(tmp_path, lambda cid: RAW_ASSIGNMENTS), "list_assignments",
                {"course_id": COURSE, "assignment_id": 11})

    assert out["name"] == "Lab 3"
    assert out["rubric"]["criteria"][0]["criterion"] == "Coverage"
    assert out["description"] == "Write tests."


def test_list_assignments_with_an_unknown_id_says_so(tmp_path):
    out = _call(_assignments_server(tmp_path, lambda cid: RAW_ASSIGNMENTS), "list_assignments",
                {"course_id": COURSE, "assignment_id": 999})

    assert "No assignment 999" in out["note"]


def _index_with_an_assignment(home):
    _add(home, [indexing.Chunk("Due date and points\nDue Fri, Nov 20, 2026 at 11:59 PM EST. Worth 50 points.",
                               {"source": "Lab 3", "item_type": "assignment", "page": None, "slide": None},
                               "assignment:301")])


@pytest.mark.parametrize(
    "error, reason",
    [
        (canvas.CanvasError("no Canvas token stored"), "No Canvas token is set up"),
        (httpx.ConnectError("offline"), "Canvas could not be reached"),
        (httpx.HTTPStatusError("401", request=httpx.Request("GET", "https://c"), response=httpx.Response(401)),
         "Your Canvas token no longer works"),
    ],
)
def test_list_assignments_falls_back_to_the_index(tmp_path, error, reason):
    _index_with_an_assignment(tmp_path)

    def fail(cid):
        raise error

    out = _call(_assignments_server(tmp_path, fail), "list_assignments", {"course_id": COURSE})

    assert out["stale"] is True
    assert out["note"].startswith(reason)
    assert out["assignments"] == [{"name": "Lab 3", "schedule": "Due Fri, Nov 20, 2026 at 11:59 PM EST. Worth 50 points."}]


def test_list_assignments_shows_times_in_the_students_zone(tmp_path):
    out = _call(_assignments_server(tmp_path, lambda cid: RAW_ASSIGNMENTS), "list_assignments", {"course_id": COURSE})

    assert out["timezone"] == "America/Los_Angeles"
    row = out["assignments"][0]
    assert row["due_at_display"].endswith(("PST", "PDT"))
    assert row["due_in"].startswith("in ")


def test_zone_comes_from_the_canvas_profile_once(monkeypatch):
    calls = []

    def profile():
        calls.append(1)
        return {"time_zone": "America/Denver"}

    monkeypatch.setattr(mcp_server.canvas, "get_profile", profile)
    zone = mcp_server.canvas_zone()

    assert zone() == ZoneInfo("America/Denver")
    assert zone() == ZoneInfo("America/Denver")
    assert len(calls) == 1


def test_zone_falls_back_to_the_machine_when_canvas_fails(monkeypatch):
    def boom():
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(mcp_server.canvas, "get_profile", boom)

    assert mcp_server.canvas_zone()() == mcp_server.assignments.machine_zone()


def test_search_results_link_to_their_canvas_page(tmp_path, monkeypatch):
    monkeypatch.setenv("SM_CANVAS_URL", "https://canvas.example.edu")
    table = _add(tmp_path, [indexing.Chunk("Late work loses ten percent per day.",
                                          {"source": "syllabus", "item_type": "syllabus", "page": None, "slide": None},
                                          f"syllabus:{COURSE}")])
    searcher, _ = _searcher(tmp_path, table)

    out = searcher.search(COURSE, "late work")

    assert out["results"][0]["url"] == f"https://canvas.example.edu/courses/{COURSE}/assignments/syllabus"


def test_get_grades_returns_canvas_total_and_groups(tmp_path):
    course = {"apply_assignment_group_weights": True,
              "enrollments": [{"type": "student", "computed_current_score": 88.0, "computed_current_grade": "B+"}]}
    groups = [{"id": 1, "name": "Exams", "group_weight": 40, "assignments": []}]
    server = mcp_server.build_server(tmp_path, courses_for=list, course_grades_for=lambda cid: course,
                                     groups_for=lambda cid: groups)

    out = _call(server, "get_grades", {"course_id": COURSE})

    assert out["course_total"]["current_score"] == 88.0
    assert out["groups"][0] == {"name": "Exams", "weight_percent": 40, "assignments": 0, "graded": 0,
                                "graded_points": 0.0, "graded_possible": 0.0}
    assert out["url"].endswith(f"/courses/{COURSE}/grades")


def test_get_grades_without_canvas_says_why(tmp_path):
    def fail(cid):
        raise httpx.ConnectError("offline")

    server = mcp_server.build_server(tmp_path, courses_for=list, course_grades_for=fail, groups_for=fail)

    assert _call(server, "get_grades", {"course_id": COURSE})["note"].startswith("Canvas could not be reached")


class _FakeJobs:
    def __init__(self, result=None):
        self.started = []
        self._result = result or {"started": True, "state": "running"}

    def start(self, course_id):
        self.started.append(course_id)
        return {"course_id": course_id, **self._result}


NOW = datetime.datetime(2026, 11, 21, 18, 0, tzinfo=datetime.timezone.utc)


def _status(home, state, finished_at=None, course_id=COURSE):
    d = home / "courses" / str(course_id)
    d.mkdir(parents=True, exist_ok=True)
    status = {"course_id": course_id, "state": state}
    if finished_at:
        status["finished_at"] = finished_at
    (d / "sync_status.json").write_text(json.dumps(status))


def _refresh(home, jobs, token=lambda: "t"):
    return mcp_server.refresh_if_stale(home, jobs, COURSE, token_check=token, now=NOW)


def test_a_fresh_index_is_not_refreshed(tmp_path):
    _build_index(tmp_path)
    _status(tmp_path, "done", "2026-11-21T15:00:00Z")
    jobs = _FakeJobs()
    assert _refresh(tmp_path, jobs) is None
    assert jobs.started == []


@pytest.mark.parametrize("status", [("done", "2026-11-21T08:00:00Z"), None])
def test_a_stale_or_never_finished_index_refreshes_in_the_background(tmp_path, status):
    _build_index(tmp_path)
    if status:
        _status(tmp_path, *status)
    jobs = _FakeJobs()
    note = _refresh(tmp_path, jobs)
    assert jobs.started == [COURSE]
    assert "Refreshing this course" in note and "last sync" in note


def test_a_failed_sync_is_retried_only_after_a_while(tmp_path):
    _build_index(tmp_path)
    _status(tmp_path, "failed", "2026-11-21T17:45:00Z")
    jobs = _FakeJobs()
    assert _refresh(tmp_path, jobs) is None
    _status(tmp_path, "failed", "2026-11-21T17:00:00Z")
    assert _refresh(tmp_path, jobs) is not None and jobs.started == [COURSE]


def test_no_refresh_without_an_index_or_a_token(tmp_path):
    jobs = _FakeJobs()
    assert _refresh(tmp_path, jobs) is None  # not indexed: syncing it is the student's call
    _build_index(tmp_path)

    def no_token():
        raise mcp_server.canvas.CanvasError("no token")

    assert _refresh(tmp_path, jobs, token=no_token) is None
    assert jobs.started == []


def test_search_says_when_the_course_is_refreshing(tmp_path, monkeypatch):
    index = _build_index(tmp_path)
    monkeypatch.setenv("SM_CANVAS_TOKEN", "t")
    searcher, _ = _searcher(tmp_path, index)
    jobs = _FakeJobs()
    server = mcp_server.build_server(tmp_path, searcher=searcher, courses_for=list, jobs=jobs)

    out = _call(server, "search_course_material", {"course_id": COURSE, "query": "late work"})

    assert out["results"] and "Refreshing this course" in out["refresh"]
    assert jobs.started == [COURSE]


def test_last_synced_at_comes_from_the_last_finished_sync(tmp_path):
    _status(tmp_path, "done", "2026-11-21T15:00:00Z")
    assert mcp_server.last_synced_at(tmp_path, COURSE) == "2026-11-21T15:00:00Z"


def test_list_courses_writes_the_session_note_for_indexed_courses(tmp_path):
    _build_index(tmp_path)
    courses = [{"id": COURSE, "name": "Software Requirements"}, {"id": 999, "name": "Not indexed"}]

    mcp_server.list_courses_result(tmp_path, courses_for=lambda: courses)

    text = json.loads((tmp_path / "session_start.json").read_text())["hookSpecificOutput"]["additionalContext"]
    assert "Software Requirements" in text and "Not indexed" not in text


def test_a_finished_sync_rewrites_the_session_note(tmp_path, monkeypatch):
    _build_index(tmp_path)
    mcp_server.session_note.remember_names(tmp_path, {COURSE: "Software Requirements"})
    monkeypatch.setenv("SM_CANVAS_TOKEN", "t")
    monkeypatch.setattr(mcp_server.sync_jobs, "_default_ensure_model", lambda: None)
    monkeypatch.setattr(mcp_server.sync_jobs, "_default_run",
                        lambda cid, home: iter([{"done": True, "new": 0, "changed": 0, "removed": 0, "failed": 0}]))
    searcher, _ = _searcher(tmp_path, None)
    server = mcp_server.build_server(tmp_path, searcher=searcher, courses_for=list)
    assert not (tmp_path / "session_start.json").exists()

    assert _call(server, "sync_course", {"course_id": COURSE})["started"] is True
    for _ in range(100):  # the sync runs on a thread
        if (tmp_path / "session_start.json").exists():
            break
        time.sleep(0.05)

    assert "Software Requirements" in (tmp_path / "session_start.json").read_text()


def test_startup_backfills_missing_course_names_once(tmp_path):
    _build_index(tmp_path)
    calls = []

    def courses():
        calls.append(1)
        return [{"id": COURSE, "name": "Software Requirements"}]

    mcp_server.backfill_course_names(tmp_path, courses)
    mcp_server.backfill_course_names(tmp_path, courses)  # names on disk now: Canvas isn't asked again

    assert len(calls) == 1
    assert "Software Requirements" in (tmp_path / "session_start.json").read_text()


def test_backfill_without_canvas_or_index_does_nothing(tmp_path):
    def offline():
        raise httpx.ConnectError("offline")

    mcp_server.backfill_course_names(tmp_path, offline)  # nothing indexed: Canvas never asked
    _build_index(tmp_path)
    mcp_server.backfill_course_names(tmp_path, offline)  # Canvas down: no crash, no note
    assert not (tmp_path / "session_start.json").exists()
