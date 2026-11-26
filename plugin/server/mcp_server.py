"""MCP server for the Canvas Study Claude Code plugin.

Claude Code is the LLM. This server only reads Canvas and searches: no tool
here calls a model. Its tools:
- list_courses: the student's Canvas courses, each marked indexed or not.
- search_course_material: hybrid search (vector + BM25, rank fusion, 0.5
  cutoff) over the course's indexed materials, returning whole chunks with
  their citation labels and Canvas links. No reranker: measured before this
  repo was split out (that query set isn't here), it added about 0.2 s a search
  and no hit@5, and it is one less model to download.
- list_course_items: a synced course's module outline, for complete lists.
- list_assignments: deadlines, status, scores, rubrics and teacher comments,
  live from Canvas.
- get_grades: the course total Canvas computed, plus grade categories with
  their weights and drop rules, live.
- sync_course / sync_status: index a course in the background (sync_jobs.py).

An indexed course refreshes itself: a search or listing of a course whose
last sync finished over REFRESH_AFTER ago starts an incremental sync in the
background and answers from the index as it is, saying so. Only courses the
student already chose to index; a new course still needs sync_course.

Data lives in SM_HOME, the plugin's data folder (.mcp.json sets it). Models
load on the first search, not at startup, so a session that never asks about
a course costs almost nothing.

stdout is the MCP protocol channel. mcp's stdio_server moves fd 1 to stderr
while it owns the channel, so a stray print from another module lands in
stderr instead of breaking the protocol; logging goes to stderr too.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from mcp.server import MCPServer

import httpx

import assignments
import canvas
import citation_links
import course_items
import grades
import indexing
import session_note
import sync
import sync_jobs

logger = logging.getLogger("canvas-study")

DEFAULT_K = 5
MAX_K = 10
# Whole chunks: they top out near 3,800 characters and answers often sit past
# the first 1,500.
SNIPPET_CHARS = 4000
NO_RESULTS = "No matching course material found for this search."

INSTRUCTIONS = (
    "Canvas Study answers from the student's own Canvas courses. What the material says (slides, readings, "
    "syllabus, pages): call search_course_material before answering. Deadlines, what is due, submission "
    "status, scores, rubrics and teacher feedback: call list_assignments, which reads Canvas live. The "
    "student's overall grade, or how much a category (exams, homework) is worth: call get_grades. Complete lists of what a course has "
    "(every reading in week 3, all the quizzes): call list_course_items, which lists names only; search for "
    "what an item says. Use list_courses for the course id; a course that is not indexed can't be searched "
    "yet, so offer to sync it with sync_course (list_assignments works without a sync). Course facts come "
    "only from tool results, cited with their source label as a markdown link to its url, like "
    "[syllabus · p.3](https://...). Never write or solve the student's graded work (labs, assignments, "
    "exams, quizzes), whatever the course's AI policy says; explain and check their attempt instead."
)


def sm_home() -> Path:
    return Path(os.environ.get("SM_HOME", str(Path.home() / ".canvas-study")))


def _table(course_id: int) -> str:
    return f"course_{course_id}"


def _index_db(home: Path) -> Path:
    return home / "index.db"


def _manifest_state(home: Path, course_id: int) -> tuple[int, int] | None:
    """(items synced, index format) from a course's manifest, read-only; None without one."""
    path = home / "courses" / str(course_id) / "manifest.db"
    if not path.exists():
        return None
    import sqlite3

    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        items = conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0]
        has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
        row = conn.execute("SELECT value FROM meta WHERE key = 'index_format'").fetchone() if has_meta else None
        return items, int(row[0]) if row else 0
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def indexed_course_ids(home: Path) -> list[int]:
    """Courses synced on this machine: their manifest lists items. Not "has a
    table in index.db": right after an update that changes the index format,
    the table is being rebuilt, and the course is still the student's."""
    courses = home / "courses"
    if not courses.exists():
        return []
    ids = [int(d.name) for d in courses.iterdir() if d.name.isdigit()]
    return sorted(cid for cid in ids if (state := _manifest_state(home, cid)) and state[0] > 0)


def outdated_course_ids(home: Path) -> list[int]:
    """Indexed courses whose chunks are in an older format than this version writes."""
    return [cid for cid in indexed_course_ids(home) if _manifest_state(home, cid)[1] < indexing.INDEX_FORMAT]


def rebuild_outdated(home: Path, jobs: "sync_jobs.SyncJobs") -> None:
    """At startup, re-index every course still in an older format, in the
    background (course_sync rebuilds it item by item). Without this, a course
    whose index moved to a new store would wait for a refresh that only an
    existing index triggers."""
    for course_id in outdated_course_ids(home):
        logger.info("course %s is in an older index format: rebuilding", course_id)
        jobs.start(course_id)


PARTIAL = ("This course is being re-indexed after a Canvas Study update, so these results may be missing items "
           "that aren't back in the index yet.")
REBUILDING = ("Course {cid} is being re-indexed after a Canvas Study update{progress}, so it can't be searched for a "
              "few minutes. Its deadlines, grades and feedback (list_assignments, get_grades) work meanwhile.")


# How old an index may get before using it starts a background refresh. A
# refresh with no changes only lists Canvas: nothing is downloaded or embedded.
REFRESH_AFTER = timedelta(hours=6)
# After a failed or interrupted sync (Canvas down, token expired), wait this
# long before trying again, so every search doesn't hit a failing Canvas.
RETRY_AFTER = timedelta(minutes=30)


def refresh_if_stale(home: Path, jobs: "sync_jobs.SyncJobs", course_id: int, token_check=None,
                     now: datetime | None = None) -> str | None:
    """Starts a background sync of an indexed course whose last sync is old,
    and returns a note for the answer; None when the index is fresh enough
    (or isn't there: syncing a new course is the student's call)."""
    if course_id not in indexed_course_ids(home):
        return None
    now = now or datetime.now(timezone.utc)
    status = sync_jobs.read_status(home, course_id) or {}
    state = status.get("state")
    if state == "running":
        return "This course is being refreshed from Canvas right now; results come from the last sync."
    finished = canvas.parse_time(status.get("finished_at"))
    wait = REFRESH_AFTER if state == "done" else RETRY_AFTER
    if finished and now - finished < wait:
        return None
    try:
        (token_check or canvas._token)()
    except canvas.CanvasError:
        return None
    started = jobs.start(course_id)
    if not started.get("started") and started.get("state") != "running":
        return None
    since = f" of {status['finished_at']}" if finished else ""
    return (f"Refreshing this course from Canvas in the background; these results come from the last sync{since}. "
            "Only new or changed items are re-read.")


def last_synced_at(home: Path, course_id: int) -> str | None:
    """When the last sync finished. The manifest's own times only move when
    an item changes, so a sync that found nothing new is read from its status."""
    status = sync_jobs.read_status(home, course_id) or {}
    if status.get("state") == "done" and status.get("finished_at"):
        return status["finished_at"]
    manifest = home / "courses" / str(course_id) / "manifest.db"
    if not manifest.exists():
        return None
    conn = sync.open_manifest(manifest)
    try:
        row = conn.execute("SELECT MAX(last_synced_at) FROM manifest").fetchone()
    finally:
        conn.close()
    return row[0] if row else None




def _course_row(home: Path, course_id: int, name: str | None, indexed: bool) -> dict:
    row = {"course_id": course_id, "name": name, "indexed": indexed, "last_synced_at": last_synced_at(home, course_id)}
    status = sync_jobs.read_status(home, course_id)
    if status:
        row["sync_state"] = status["state"]
    return row


def list_courses_result(home: Path, courses_for: Callable[[], list[dict]] = canvas.list_courses) -> dict:
    """Every course the student can open in Canvas, each marked indexed or
    not, so Claude can offer to sync one. Courses Canvas locks by date are
    left out. Without Canvas, the courses already indexed here."""
    ids = indexed_course_ids(home)
    try:
        raw = courses_for()
    except canvas.ERRORS as e:
        logger.warning("list_courses without Canvas: %s", e)
        return {
            "courses": [_course_row(home, cid, None, True) for cid in ids],
            "note": f"{_canvas_failure(e)}, so only the courses already indexed on this machine are listed.",
        }
    rows = [
        _course_row(home, c["id"], c["name"], c["id"] in ids)
        for c in raw
        if c.get("name") and not c.get("access_restricted_by_date")
    ]
    listed = {r["course_id"] for r in rows}
    rows += [_course_row(home, cid, None, True) for cid in ids if cid not in listed]
    session_note.remember_names(home, {r["course_id"]: r["name"] for r in rows if r["name"]})
    session_note.write_note(home, ids)
    return {"courses": rows}


def make_searcher(index, embed):
    """The searcher for one course's index (store.CourseIndex). It returns MAX_K, not its default 5, so
    search's [:k] can reach MAX_K; fusion orders before it cuts, so the first 5 of 10 are the same top 5."""
    import retrieval

    return retrieval.HybridSearcher(index, embed, top_k=MAX_K)


_embed_query = None


def _query_embedder():
    """One embedding model for every course, loaded on the first search (the slow part)."""
    global _embed_query
    if _embed_query is None:
        import embeddings
        import model_download

        model_download.ensure_embedding_model()
        _embed_query = embeddings.OnnxBgeEmbedding().embed
    return _embed_query


def _searcher_factory(home: Path):
    def make(course_id: int):
        import store

        return make_searcher(store.CourseIndex(_index_db(home), _table(course_id)), _query_embedder())

    return make


def _log_query(course_id: int, query: str, results: list[dict], seconds: float) -> None:
    """With SM_QUERY_LOG set, append each search as one JSON line: what
    Claude asked and what came back, for scoring retrieval and latency. It
    holds course text, so it is never committed."""
    path = os.environ.get("SM_QUERY_LOG")
    if not path:
        return
    entry = {
        "course_id": course_id,
        "query": query,
        "sources": [r["source"] for r in results],
        "texts": [r["text"] for r in results],
        "seconds": round(seconds, 3),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Searcher:
    """One searcher per course, built on its first search."""

    def __init__(self, home: Path, make_searcher=None):
        self._home = home
        self._make = make_searcher or _searcher_factory(home)
        self._searchers: dict[int, object] = {}

    def forget(self, course_id: int) -> None:
        """Drop a course's searcher after a sync, so the next search opens
        the table as the sync left it."""
        self._searchers.pop(course_id, None)

    def search(self, course_id: int, query: str, k: int = DEFAULT_K) -> dict:
        import retrieval

        k = max(1, min(int(k), MAX_K))
        # Checked only until the course's searcher exists: a missing course
        # is not cached, so it is found once a sync creates its table.
        if course_id not in self._searchers and not indexing.index_exists(_index_db(self._home), _table(course_id)):
            # Logged too, so the eval sees the tool call even though nothing came back.
            _log_query(course_id, query, [], 0.0)
            if course_id in indexed_course_ids(self._home):
                status = sync_jobs.read_status(self._home, course_id) or {}
                done = status.get("items_done")
                progress = f" ({done} items done so far)" if status.get("state") == "running" and done else ""
                return {"course_id": course_id, "results": [], "note": REBUILDING.format(cid=course_id, progress=progress)}
            return {
                "course_id": course_id,
                "results": [],
                "note": (
                    f"Course {course_id} is not indexed on this machine yet, so nothing can be searched. "
                    "This is not the same as the material not covering it. Offer to sync it with sync_course."
                ),
            }
        start = time.perf_counter()
        if course_id not in self._searchers:
            self._searchers[course_id] = self._make(course_id)
        hits = self._searchers[course_id].search(query)[:k]
        citations = [retrieval.citation(hit) for hit in hits]
        origin = canvas.origin()
        results = [
            {
                "n": n,
                "source": citation["label"],
                "url": citation_links.canvas_url(origin, course_id, citation["source_type"], citation["item_id"]),
                "text": hit.text[:SNIPPET_CHARS],
            }
            for n, (citation, hit) in enumerate(zip(citations, hits), start=1)
        ]
        seconds = time.perf_counter() - start
        logger.info("search course=%s query=%r results=%d seconds=%.2f", course_id, query, len(results), seconds)
        _log_query(course_id, query, results, seconds)
        out = {"course_id": course_id, "query": query, "results": results}
        if not results:
            out["note"] = NO_RESULTS
        if course_id in outdated_course_ids(self._home):
            out["rebuilding"] = PARTIAL
        return out


def _index_rows(home: Path, course_id: int) -> list[dict]:
    """Every indexed chunk's id, text and metadata, without the vectors."""
    if not _index_db(home).exists():
        return []
    import store

    conn = store.connect(_index_db(home))
    try:
        return store.rows(conn, _table(course_id))
    finally:
        conn.close()


def _canvas_failure(error: Exception) -> str:
    """Why Canvas gave nothing, in words the student can act on."""
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 401:
        return ("Your Canvas token doesn't work (make a new one in Canvas and paste it in /plugin > Installed > "
                "canvas-study > Configure options, then run /reload-plugins)")
    if isinstance(error, canvas.CanvasError):
        return "No Canvas token is set up (run /canvas-study:setup)"
    return "Canvas could not be reached"


def canvas_zone():
    """The student's time zone, from their Canvas profile (the zone Canvas
    shows their deadlines in), else this machine's. Asked once per server."""
    cached: list = []

    def zone():
        if not cached:
            try:
                name = canvas.get_profile().get("time_zone")
            except canvas.ERRORS as e:
                logger.warning("no time zone from Canvas, using this machine's: %s", e)
                name = None
            cached.append(assignments.zone_from_name(name))
        return cached[0]

    return zone


def list_assignments_result(home: Path, course_id: int, fetch, days: int, only_unsubmitted: bool,
                            assignment_id: int | None, zone=assignments.machine_zone,
                            description_offset: int = 0, with_feedback: bool = False) -> dict:
    try:
        raw = fetch(course_id)
    except canvas.ERRORS as e:
        logger.warning("list_assignments falling back to the index: %s", e)
        return {"course_id": course_id, **assignments.from_index_rows(_index_rows(home, course_id), _canvas_failure(e))}
    if assignment_id is not None:
        match = next((a for a in raw if a.get("id") == assignment_id), None)
        if match is None:
            return {"course_id": course_id, "note": f"No assignment {assignment_id} in this course on Canvas."}
        return {"course_id": course_id, **assignments.detail(match, tz=zone(), description_offset=description_offset)}
    if with_feedback:
        return {"course_id": course_id, **assignments.with_feedback(raw, tz=zone())}
    return {"course_id": course_id, **assignments.upcoming(raw, days, only_unsubmitted, tz=zone())}


def get_grades_result(course_id: int, course_for=canvas.get_course_grades,
                      groups_for=canvas.list_assignment_groups) -> dict:
    try:
        course, groups = course_for(course_id), groups_for(course_id)
    except canvas.ERRORS as e:
        logger.warning("get_grades without Canvas: %s", e)
        return {"course_id": course_id, "note": f"{_canvas_failure(e)}, so grades can't be read right now."}
    url = f"{canvas.origin()}/courses/{course_id}/grades"
    return {"course_id": course_id, **grades.summary(course, groups, url)}


def sync_course_result(jobs: "sync_jobs.SyncJobs", course_id: int, token_check=canvas._token) -> dict:
    try:
        token_check()
    except canvas.CanvasError as e:
        return {"course_id": course_id, "started": False, "error": _canvas_failure(e)}
    return jobs.start(course_id)


def sync_status_result(home: Path, course_id: int | None) -> dict:
    ids = [course_id] if course_id is not None else [
        int(d.name) for d in sorted((home / "courses").glob("*")) if d.name.isdigit()
    ]
    syncs = [s for cid in ids if (s := sync_jobs.read_status(home, cid))]
    if course_id is not None and not syncs:
        return {"syncs": [], "note": f"Course {course_id} has never been synced on this machine."}
    return {"syncs": syncs}


def build_server(home: Path | None = None, searcher: Searcher | None = None, courses_for=canvas.list_courses,
                 assignments_for=canvas.list_assignments, zone_for=None, jobs=None,
                 course_grades_for=canvas.get_course_grades, groups_for=canvas.list_assignment_groups,
                 rebuild_on_start: bool = False) -> MCPServer:
    home = home or sm_home()
    searcher = searcher or Searcher(home)
    def synced(course_id: int) -> None:
        searcher.forget(course_id)
        session_note.write_note(home, indexed_course_ids(home))

    jobs = jobs or sync_jobs.SyncJobs(home, on_done=synced)
    if rebuild_on_start:
        rebuild_outdated(home, jobs)
    zone_for = zone_for or canvas_zone()
    server = MCPServer("canvas-study", instructions=INSTRUCTIONS)

    # Tools return dicts, not JSON strings: a str result reaches Claude
    # wrapped as {"result": "<escaped JSON>"}, seen in the first live run.
    @server.tool()
    def list_courses() -> dict:
        """List the student's Canvas courses: course_id, name, whether it is indexed (searchable) on this
        machine, and its last sync time."""
        return list_courses_result(home, courses_for)

    @server.tool()
    def search_course_material(course_id: int, query: str, k: int = DEFAULT_K) -> dict:
        """Search one course's materials. Returns up to k chunks, each with a source label to cite and,
        when the item is in Canvas, the url of its Canvas page."""
        out = searcher.search(course_id, query, k)
        refresh = refresh_if_stale(home, jobs, course_id)
        if refresh:
            out["refresh"] = refresh
        return out

    @server.tool()
    def list_course_items(course_id: int, topic: str = "", types: list[str] | None = None) -> dict:
        """Every item in a synced course's Canvas modules (files, pages, assignments, quizzes, links, ...),
        grouped by module and section: for "what are all the readings for week 3" or "list the quizzes",
        which search can't answer completely. topic narrows it to a module, section or title ("week 3");
        types to item kinds (file, page, assignment, quiz, discussion, link, tool, announcement). Names
        only: search_course_material for what an item says. Each item is a markdown link to it on Canvas: keep
        the link when you list the item."""
        out = {"listing": course_items.list_for_course(home, course_id, topic, types),
               "url": f"{canvas.origin()}/courses/{course_id}/modules"}
        refresh = refresh_if_stale(home, jobs, course_id)
        if refresh:
            out["refresh"] = refresh
        return out

    @server.tool()
    def list_assignments(course_id: int, days: int = assignments.DEFAULT_DAYS, only_unsubmitted: bool = False,
                         assignment_id: int | None = None, description_offset: int = 0,
                         with_feedback: bool = False) -> dict:
        """Live from Canvas: every assignment due in the next `days` days and the last week, submitted or not,
        each with its submission status and score. Times come in the student's zone: show due_at_display as
        given. Pass only_unsubmitted when the student asks only for what is left to hand in. With
        assignment_id: that one assignment's description, rubric, submission, score and the teacher's comments
        on the submission. A very long description comes with description_cut; pass its description_offset to
        read the rest. with_feedback: every assignment the teacher commented on, any date, with the comments:
        for what a professor or TA said about the student's work. Each assignment comes with link, a markdown
        link to it on Canvas: write the assignment as that link when you list it."""
        return list_assignments_result(home, course_id, assignments_for, days, only_unsubmitted, assignment_id,
                                       zone_for, description_offset, with_feedback)

    @server.tool()
    def get_grades(course_id: int) -> dict:
        """Live from Canvas: the student's course total as Canvas computes it (current and final score and
        letter grade), whether the grade is weighted by category, and each category (Homework, Exams, ...)
        with its weight, drop rules and the points graded so far. Report Canvas's course_total as the grade;
        the per-category sums are before drop rules."""
        return get_grades_result(course_id, course_grades_for, groups_for)

    @server.tool()
    def sync_course(course_id: int) -> dict:
        """Download one course's materials from Canvas and index them so search_course_material can find them.
        Runs in the background (a few minutes the first time, faster after) and returns at once; check it
        with sync_status. Ask the student before syncing a course."""
        return sync_course_result(jobs, course_id)

    @server.tool()
    def sync_status(course_id: int | None = None) -> dict:
        """How a course sync is going (running, done, failed, interrupted) with item counts. Without
        course_id: every course synced on this machine."""
        return sync_status_result(home, course_id)

    return server


def main() -> None:
    if "--version" in sys.argv[1:]:
        # Used to warm uvx's cache before Claude Code's first start:
        # installs everything, imports nothing heavy.
        from importlib.metadata import PackageNotFoundError, version

        try:
            print(version("canvas-study-mcp"))
        except PackageNotFoundError:
            print("dev")
        return
    if "--download-model" in sys.argv[1:]:
        import model_download

        model_download.main()
        return
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx logs each request URL at INFO, and Canvas file downloads carry
    # signed tokens in theirs; Claude Code keeps the server's stderr in its logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_server(sm_home(), rebuild_on_start=True).run("stdio")


if __name__ == "__main__":
    main()
