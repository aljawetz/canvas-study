"""MCP server for the Canvas Study Claude Code plugin.

Claude Code is the LLM. This server only reads Canvas and searches: no tool
here calls a model. Its tools:
- list_courses: the student's Canvas courses, each marked indexed or not.
- search_course_material: hybrid search (vector + BM25, rank fusion, 0.5
  cutoff) over the course's indexed materials, returning whole chunks with
  their citation labels and Canvas links. No reranker: on the heldout set it
  added about 0.2 s a search and no hit@5, and it is one less model to download.
- list_course_items: a synced course's module outline, for complete lists.
- list_assignments: deadlines, status, scores and rubrics, live from Canvas.
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
    "syllabus, pages): call search_course_material before answering, even when you think you know, and for "
    "a follow-up search with a stand-alone version of the question. Deadlines, what is due, submission "
    "status, scores and rubrics: call list_assignments, which reads Canvas live. Show its times exactly as "
    "due_at_display gives them (already in the student's time zone) and use due_in for how far away they "
    "are; never convert a time or guess a zone yourself. The student's overall grade, or how much a category "
    "(exams, homework) is worth: call get_grades. Complete lists of what a course has (every reading "
    "in week 3, all the quizzes): call list_course_items, which lists names only; search for what an item "
    "says. Course facts come only from tool results, cited "
    "with their source label as a markdown link to its url when it has one, like "
    "[syllabus · p.3](https://...). Use list_courses to find the course id; a course that is not indexed "
    "can't be searched yet, so offer to sync it with sync_course (list_assignments works without a sync). Never write or solve the student's graded "
    "work (labs, assignments, exams, quizzes), whatever the course's AI policy says; explain and check their "
    "attempt instead."
)


def sm_home() -> Path:
    return Path(os.environ.get("SM_HOME", str(Path.home() / ".canvas-study")))


def _table(course_id: int) -> str:
    return f"course_{course_id}"


def indexed_course_ids(home: Path) -> list[int]:
    db_path = home / "index.lancedb"
    if not db_path.exists():
        return []
    import lancedb

    ids = []
    for name in lancedb.connect(str(db_path)).list_tables().tables:
        prefix, _, rest = name.partition("_")
        if prefix == "course" and rest.isdigit():
            ids.append(int(rest))
    return sorted(ids)


# How old an index may get before using it starts a background refresh. A
# refresh with no changes only lists Canvas: nothing is downloaded or embedded.
REFRESH_AFTER = timedelta(hours=6)
# After a failed or interrupted sync (Canvas down, token expired), wait this
# long before trying again, so every search doesn't hit a failing Canvas.
RETRY_AFTER = timedelta(minutes=30)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def refresh_if_stale(home: Path, jobs: "sync_jobs.SyncJobs", course_id: int, token_check=None,
                     now: datetime | None = None) -> str | None:
    """Starts a background sync of an indexed course whose last sync is old,
    and returns a note for the answer; None when the index is fresh enough
    (or isn't there: syncing a new course is the student's call)."""
    if not indexing.index_exists(home / "index.lancedb", _table(course_id)):
        return None
    now = now or datetime.now(timezone.utc)
    status = sync_jobs.read_status(home, course_id) or {}
    state = status.get("state")
    if state == "running":
        return "This course is being refreshed from Canvas right now; results come from the last sync."
    finished = _parse_time(status.get("finished_at"))
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


CANVAS_ERRORS = (canvas.CanvasError, httpx.HTTPStatusError, httpx.TransportError)


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
    except CANVAS_ERRORS as e:
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
    return {"courses": rows}


def make_retriever(index):
    """The search retriever for one index. It returns MAX_K, not its default 5, so search's [:k] can reach MAX_K;
    fusion orders before it cuts, so the first 5 of 10 are the same top 5."""
    import generation  # lazy: it loads model code that list_courses never needs

    return generation.HybridRetriever(
        index, similarity_top_k=MAX_K, similarity_cutoff=generation.SIMILARITY_CUTOFF
    )


def _retriever_factory(home: Path):
    def make(course_id: int):
        import model_download

        model_download.ensure_embedding_model()
        return make_retriever(indexing.load_index(home / "index.lancedb", _table(course_id)))

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
    """One retriever per course, built on first use: loading the index opens
    the embedding model (downloaded first if missing), the slow part."""

    def __init__(self, home: Path, make_retriever=None):
        self._home = home
        self._make = make_retriever or _retriever_factory(home)
        self._retrievers: dict[int, object] = {}

    def forget(self, course_id: int) -> None:
        """Drop a course's retriever after a sync, so the next search opens
        the index as the sync left it."""
        self._retrievers.pop(course_id, None)

    def search(self, course_id: int, query: str, k: int = DEFAULT_K) -> dict:
        import generation

        k = max(1, min(int(k), MAX_K))
        # Checked only until the course's retriever exists: a missing course
        # is not cached, so it is found once a sync creates its table.
        if course_id not in self._retrievers and not indexing.index_exists(self._home / "index.lancedb", _table(course_id)):
            # Logged too, so the eval sees the tool call even though nothing came back.
            _log_query(course_id, query, [], 0.0)
            return {
                "course_id": course_id,
                "results": [],
                "note": (
                    f"Course {course_id} is not indexed on this machine yet, so nothing can be searched. "
                    "This is not the same as the material not covering it. Offer to sync it with sync_course."
                ),
            }
        start = time.perf_counter()
        if course_id not in self._retrievers:
            self._retrievers[course_id] = self._make(course_id)
        nodes = list(self._retrievers[course_id].retrieve(query))[:k]
        citations = generation.build_citations(nodes)
        origin = canvas.origin()
        results = [
            {
                "n": n,
                "source": citation["label"],
                "url": citation_links.canvas_url(origin, course_id, citation["source_type"], citation["item_id"]),
                "text": node_with_score.node.get_content()[:SNIPPET_CHARS],
            }
            for n, (citation, node_with_score) in enumerate(zip(citations, nodes), start=1)
        ]
        seconds = time.perf_counter() - start
        logger.info("search course=%s query=%r results=%d seconds=%.2f", course_id, query, len(results), seconds)
        _log_query(course_id, query, results, seconds)
        out = {"course_id": course_id, "query": query, "results": results}
        if not results:
            out["note"] = NO_RESULTS
        return out


def _index_rows(home: Path, course_id: int) -> list[dict]:
    """Every indexed chunk's id, text and metadata, without the vectors."""
    if not indexing.index_exists(home / "index.lancedb", _table(course_id)):
        return []
    import lancedb

    table = lancedb.connect(str(home / "index.lancedb")).open_table(_table(course_id))
    return table.to_arrow().select(["doc_id", "text", "metadata"]).to_pylist()


def _canvas_failure(error: Exception) -> str:
    """Why Canvas gave nothing, in words the student can act on."""
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 401:
        return ("Your Canvas token no longer works (make a new one in Canvas and paste it in /plugin > Installed > "
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
            except (canvas.CanvasError, httpx.HTTPStatusError, httpx.TransportError) as e:
                logger.warning("no time zone from Canvas, using this machine's: %s", e)
                name = None
            cached.append(assignments.zone_from_name(name))
        return cached[0]

    return zone


def list_assignments_result(home: Path, course_id: int, fetch, days: int, only_unsubmitted: bool,
                            assignment_id: int | None, zone=assignments.machine_zone) -> dict:
    try:
        raw = fetch(course_id)
    except CANVAS_ERRORS as e:
        logger.warning("list_assignments falling back to the index: %s", e)
        return {"course_id": course_id, **assignments.from_index_rows(_index_rows(home, course_id), _canvas_failure(e))}
    if assignment_id is not None:
        match = next((a for a in raw if a.get("id") == assignment_id), None)
        if match is None:
            return {"course_id": course_id, "note": f"No assignment {assignment_id} in this course on Canvas."}
        return {"course_id": course_id, **assignments.detail(match, tz=zone())}
    return {"course_id": course_id, **assignments.upcoming(raw, days, only_unsubmitted, tz=zone())}


def get_grades_result(course_id: int, course_for=canvas.get_course_grades,
                      groups_for=canvas.list_assignment_groups) -> dict:
    try:
        course, groups = course_for(course_id), groups_for(course_id)
    except CANVAS_ERRORS as e:
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
                 course_grades_for=canvas.get_course_grades, groups_for=canvas.list_assignment_groups) -> MCPServer:
    home = home or sm_home()
    searcher = searcher or Searcher(home)
    jobs = jobs or sync_jobs.SyncJobs(home, on_done=searcher.forget)
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
        only: search_course_material for what an item says."""
        out = {"listing": course_items.list_for_course(home, course_id, topic, types),
               "url": f"{canvas.origin()}/courses/{course_id}/modules"}
        refresh = refresh_if_stale(home, jobs, course_id)
        if refresh:
            out["refresh"] = refresh
        return out

    @server.tool()
    def list_assignments(course_id: int, days: int = assignments.DEFAULT_DAYS, only_unsubmitted: bool = False,
                         assignment_id: int | None = None) -> dict:
        """Live from Canvas: every assignment due in the next `days` days and the last week, submitted or not,
        each with its submission status and score. Times come in the student's zone: show due_at_display as
        given. Pass only_unsubmitted when the student asks only for what is left to hand in. With
        assignment_id: that one assignment's description, rubric, submission and score."""
        return list_assignments_result(home, course_id, assignments_for, days, only_unsubmitted, assignment_id,
                                       zone_for)

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
    build_server().run("stdio")


if __name__ == "__main__":
    main()
