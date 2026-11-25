"""Course sync orchestrator: one course from Canvas into the local index.

Wires canvas.py + ingestion.py + indexing.py together via sync.py's
manifest-diff mechanism into a real, incremental sync pipeline. sync.py
itself stays Canvas-agnostic (its own docstring: "the caller ... is
responsible for the actual Canvas calls") — this module is that caller.
"""

import os
import re
import sys
import tempfile
from datetime import datetime, tzinfo
from functools import partial
from pathlib import Path

import canvas
import citation_links
import course_items
import ingestion
import indexing
import sync

# File types read as sections (headings and their text), like Canvas pages.
SECTION_EXTRACTORS = {
    ".docx": "extract_docx",
    ".xlsx": "extract_xlsx",
    ".txt": "extract_text_file",
    ".md": "extract_text_file",
    ".csv": "extract_text_file",
}
SUPPORTED_FILE_SUFFIXES = {".pdf", ".pptx", *SECTION_EXTRACTORS}


def _course_paths(sm_home: Path, course_id: int) -> tuple[Path, Path]:
    course_dir = sm_home / "courses" / str(course_id)
    course_dir.mkdir(parents=True, exist_ok=True)
    return course_dir / "manifest.db", sm_home / "index.db"


def _index_item(conn, db_path, table_name, item_type, prefixed_id, is_changed, build):
    """Index one item: drop its old chunks if it changed, build it, add its
    chunks and record it in the manifest. build() returns (title, version,
    chunks, text) and runs after the delete, so a changed page fetches its
    body only once its old chunks are gone. Returns the title."""
    if is_changed:
        indexing.delete_ref_doc_nodes(db_path, table_name, prefixed_id)
    title, version, nodes, text = build()
    indexing.add_nodes(nodes, db_path, table_name)
    sync.mark_synced(conn, prefixed_id, item_type, title, version, sync.content_hash(text))
    return title


def _sections_doc(prefixed_id, item_type, title, version, sections, node_title=None):
    """(title, version, chunks, text) for Canvas HTML read as sections. Each
    type keeps its own item_type: retrieval.py reads it back as the source
    type, and citation_links.py builds the Canvas URL from it."""
    nodes = indexing.sections_to_nodes(sections, title, prefixed_id, item_type=item_type, title=node_title)
    return title, version, nodes, ingestion.sections_text(sections)


def _file_doc(prefixed_id, file_meta):
    display_name = file_meta.get("display_name", "")
    suffix = Path(display_name).suffix.lower()
    raw = canvas.download(file_meta["url"])
    fd, tmp_name = tempfile.mkstemp(suffix=suffix)
    tmp_path = Path(tmp_name)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    try:
        if suffix == ".pdf":
            pages = ingestion.extract_pdf(tmp_path)
            for p in pages:
                if p["needs_fallback"]:
                    p["text"] = ingestion.merge_ocr_text(p["text"], ingestion.ocr_pdf_page(tmp_path, p["page"]))
            nodes = indexing.pages_to_nodes(pages, display_name, prefixed_id)
            full_text = "".join(p["text"] for p in pages)
        elif suffix == ".pptx":
            slides = ingestion.extract_pptx(tmp_path)
            nodes = indexing.slides_to_nodes(slides, display_name, prefixed_id)
            full_text = "".join(s["text"] for s in slides)
        else:  # Word, Excel, plain text
            sections = getattr(ingestion, SECTION_EXTRACTORS[suffix])(tmp_path)
            nodes = indexing.sections_to_nodes(sections, display_name, prefixed_id, "file")
            full_text = ingestion.sections_text(sections)
    finally:
        tmp_path.unlink()
    nodes = indexing.mark_syllabus_file(nodes, display_name)
    return display_name, file_meta.get("updated_at", ""), nodes, full_text


def _page_doc(course_id, prefixed_id, page):
    # Pages found via Modules or the front page were already fetched in full
    # (body included) to learn their updated_at; only list_pages summaries
    # whose body couldn't be read earlier need fetching here.
    full = page if "body" in page else canvas.get_page(course_id, page["url"])
    title = (full or {}).get("title") or page.get("title") or page["url"]
    sections = ingestion.extract_html_sections((full or {}).get("body") or "")
    # A Canvas wiki page isn't paginated like a PDF: item_type "page", and no
    # fabricated page number.
    return _sections_doc(prefixed_id, "page", title, page.get("updated_at", ""), sections)


def _syllabus_doc(prefixed_id, syllabus_html):
    # Own item_type so citation_links.py can open Canvas's syllabus URL
    # (/assignments/syllabus), which isn't under /pages/. It has no
    # updated_at: its content hash stands in as the version.
    sections = ingestion.extract_html_sections(syllabus_html)
    return _sections_doc(prefixed_id, "syllabus", "Syllabus", sync.content_hash(syllabus_html), sections,
                         node_title=indexing.SYLLABUS_TITLE)


def schedule_line(assignment: dict, tz: tzinfo | None = None) -> str:
    """"Due Tue, Sep 15, 2026 at 10:00 AM EDT. Worth 100 points." for an
    assignment, or "" when Canvas gives neither. A due date lives on the
    assignment record, not in its description, so without this line chat
    can't answer "when is X due?". tz defaults to the student's own (the
    backend runs on their machine), so the time reads the way Canvas shows it
    to them."""
    parts = []
    due_at = assignment.get("due_at")
    if due_at:
        try:
            due = datetime.fromisoformat(due_at).astimezone(tz)
            parts.append(f"Due {due.strftime('%a, %b')} {due.day}, {due.year} at {due.strftime('%I:%M %p %Z').lstrip('0')}.")
        except ValueError:
            pass
    points = assignment.get("points_possible")
    if isinstance(points, (int, float)) and points > 0:
        parts.append(f"Worth {points:g} points.")
    return " ".join(parts)


def _assignment_doc(prefixed_id, assignment):
    name = assignment.get("name") or f"Assignment {assignment['id']}"
    sections = ingestion.extract_html_sections(assignment.get("description") or "")
    schedule = schedule_line(assignment)
    if schedule:
        sections.insert(0, {"heading": "Due date and points", "text": schedule})
    # citation_links.py opens /assignments/<id>.
    return _sections_doc(prefixed_id, "assignment", name, assignment.get("updated_at", ""), sections)


def _deletable(remote_items: list[dict], deleted_ids: list[str], unresolved_ids) -> list[str]:
    """Which of diff()'s "deleted" ids are safe to actually destroy.

    canvas.py's HTTP layer degrades a 403/404 to None / an empty list
    (its own docstring: "not available to this token"), which is
    indistinguishable from "this course genuinely has zero items" or
    "this item is genuinely gone". Absence from a listing is therefore
    *not* trustworthy evidence of deletion, and acting on it can silently
    destroy a course's whole indexed corpus, which is slow to rebuild. So
    two guards:

      - an entirely empty remote listing deletes nothing (highest-risk,
        least distinguishable case — a hidden/locked modules tab looks
        exactly like an empty course);
      - an item whose own metadata fetch failed or 403'd this round is
        excluded, so a transient per-item failure can't both report
        "failed" and wipe that item's real index entry in the same run.

    The tradeoff is deliberate: stale manifest/index rows linger until a
    listing succeeds again, which is strictly better than destroying
    real content on ambiguous evidence.
    """
    if not remote_items:
        return []
    return [i for i in deleted_ids if i not in unresolved_ids]


def _linked_file_ids(html: str, course_id: int) -> list[str]:
    """Ids of this course's files linked from a page, syllabus, assignment,
    announcement or discussion: "/courses/<id>/files/<n>" (Canvas's own
    links, also under /api/v1) or a bare "/files/<n>". Another course's or a
    user's files are left alone."""
    found = []
    for link_course, course_file, bare_file in _FILE_LINK_RE.findall(html or ""):
        if course_file and link_course == str(course_id):
            found.append(course_file)
        elif bare_file:
            found.append(bare_file)
    return list(dict.fromkeys(found))


_FILE_LINK_RE = re.compile(r"/courses/(\d+)/files/(\d+)|(?<![\d/])/files/(\d+)")


def _topic_text(topic: dict) -> str:
    message = (topic.get("message") or "").strip()
    return ingestion.extract_html_page(message).strip() if message else ""


def _topic_version(topic: dict) -> str:
    """Discussion topics carry no updated_at (always null on canvas.cmu.edu),
    so their text stands in, as for the syllabus."""
    return sync.content_hash(f"{topic.get('title') or ''}\n{topic.get('message') or ''}")


def _posted_line(topic: dict, tz: tzinfo | None = None) -> str:
    when = topic.get("posted_at") or topic.get("created_at")
    if not when:
        return ""
    try:
        posted = datetime.fromisoformat(when).astimezone(tz)
    except ValueError:
        return ""
    return f"Posted {posted.strftime('%a, %b')} {posted.day}, {posted.year}."


def _topic_doc(kind, prefixed_id, topic):
    """An announcement or a discussion's opening post (not the replies)."""
    title = (topic.get("title") or "").strip() or f"{kind.title()} {topic['id']}"
    sections = ingestion.extract_html_sections(topic.get("message") or "")
    posted = _posted_line(topic) if kind == "announcement" else ""
    if posted:
        sections.insert(0, {"heading": "Posted", "text": posted})
    # citation_links.py opens /discussion_topics/<id> for both kinds.
    return _sections_doc(prefixed_id, kind, title, _topic_version(topic), sections)


QUIZ_TYPES = {
    "assignment": "Graded quiz",
    "practice_quiz": "Practice quiz (not graded)",
    "graded_survey": "Graded survey",
    "survey": "Survey (not graded)",
}


def quiz_details(quiz: dict, tz: tzinfo | None = None) -> str:
    """"Graded quiz. Due Fri, Nov 20, 2026 at 11:59 PM PST. Worth 20 points.
    10 questions. Time limit: 30 minutes. 2 attempts allowed." Facts Canvas
    keeps on the quiz record, not in its instructions."""
    parts = [f"{QUIZ_TYPES.get(quiz.get('quiz_type'), 'Quiz')}."]
    schedule = schedule_line(quiz, tz)
    if schedule:
        parts.append(schedule)
    count = quiz.get("question_count")
    if isinstance(count, int) and count > 0:
        parts.append(f"{count} question{'' if count == 1 else 's'}.")
    limit = quiz.get("time_limit")
    parts.append(f"Time limit: {limit} minutes." if limit else "No time limit.")
    attempts = quiz.get("allowed_attempts")
    if attempts == -1:
        parts.append("Unlimited attempts.")
    elif isinstance(attempts, int) and attempts > 0:
        parts.append(f"{attempts} attempt{'' if attempts == 1 else 's'} allowed.")
    return " ".join(parts)


def _quiz_sections(quiz: dict) -> list[dict]:
    sections = ingestion.extract_html_sections(quiz.get("description") or "")
    sections.insert(0, {"heading": "Quiz details", "text": quiz_details(quiz)})
    return sections


def _quiz_version(quiz: dict) -> str:
    """Quizzes carry no updated_at, so what gets indexed stands in, as for
    the syllabus and topics: a new due date or time limit reads as changed."""
    return sync.content_hash(f"{quiz.get('title') or ''}\n{ingestion.sections_text(_quiz_sections(quiz))}")


def _quiz_doc(prefixed_id, quiz):
    """A quiz's instructions and settings. Never its questions."""
    title = (quiz.get("title") or "").strip() or f"Quiz {quiz['id']}"
    return _sections_doc(prefixed_id, "quiz", title, _quiz_version(quiz), _quiz_sections(quiz))


def sync_course(course_id: int, sm_home: Path):
    """Generator. Yields {"item", "status", "error"?} per processed item, then
    exactly one final {"done": True, "new", "changed", "removed", "failed"}
    summary, or {"done": True, "error": str} if the course couldn't be
    synced at all. Exactly one terminal ("done") event always fires, even
    if something unexpected blows up mid-stream, so sync_jobs always
    records how the sync ended.

    Everything a student reaches from the course menu, not only Modules:
    Home, Syllabus, Pages, Assignments, Quizzes, Announcements, Discussions and Files.
    Files are found three ways: module items, the Files tab when the course
    shows it, and links in the text of all the others. 18-658 lists no files
    in its Modules; all 46 of its readings are links in its pages and
    syllabus."""
    manifest_path, db_path = _course_paths(sm_home, course_id)
    table_name = f"course_{course_id}"

    try:
        structure = canvas.get_course_structure(course_id)
        pages_list = canvas.list_pages(course_id)
        front_page = canvas.get_front_page(course_id)
        syllabus_html = canvas.get_syllabus(course_id)
        assignments = canvas.list_assignments(course_id)
        files_tab = canvas.list_course_files(course_id)
        announcements = canvas.list_announcements(course_id)
        discussions = canvas.list_discussions(course_id)
        quizzes = canvas.list_quizzes(course_id)
    except canvas.ERRORS as e:
        yield {"done": True, "error": str(e)}
        return

    conn = sync.open_manifest(manifest_path)
    try:
        yield from _sync_items(
            conn, course_id, db_path, table_name, structure, pages_list, front_page, syllabus_html,
            assignments, files_tab, announcements, discussions, quizzes,
        )
    finally:
        conn.close()


def _sync_items(conn, course_id, db_path, table_name, structure, pages_list, front_page, syllabus_html,
                assignments, files_tab, announcements, discussions, quizzes):
    """The rest of sync_course, once Canvas has answered: diff against the
    manifest, then index, update and delete item by item. Split out so
    sync_course can close the manifest whichever way this ends."""
    module_items = [item for module in structure for item in module.get("items", [])]

    new_count = changed_count = removed_count = failed_count = 0

    # Coarse safety net over the fine-grained per-item try/excepts below
    # (which still isolate one item's failure from the rest). Its only job
    # is the "exactly one terminal event, always" guarantee above:
    # sync.diff() and the deletions' index/manifest writes are otherwise
    # unguarded, and an exception from either would escape uncaught.
    try:
        # Pages come from three places, merged by url slug. list_pages is
        # the Pages-tab listing, which 404s for students in most courses
        # (8 of 10 surveyed) — yet every Page item in those courses' Modules
        # opened fine individually (211 of 211), and five courses had no
        # other content at all. Module items carry no updated_at, so each
        # one is fetched in full here; the body is kept for _page_doc. A
        # fetch that fails or 404s is unresolved, not deleted — same guard
        # as files below.
        pages_by_url = {p["url"]: p for p in pages_list}
        if front_page and front_page.get("url"):
            pages_by_url.setdefault(front_page["url"], front_page)
        unresolved_page_ids: set[str] = set()
        module_page_urls = [item["page_url"] for item in module_items if item.get("type") == "Page" and item.get("page_url")]
        for url in dict.fromkeys(module_page_urls):
            if url in pages_by_url:
                continue
            try:
                full = canvas.get_page(course_id, url)
            except Exception as e:
                failed_count += 1
                unresolved_page_ids.add(f"page:{url}")
                yield {"item": url, "status": "failed", "error": str(e)}
                continue
            if full is None:
                unresolved_page_ids.add(f"page:{url}")
                continue
            pages_by_url[url] = full
        # Pages-tab summaries carry no body, and a page's links are needed
        # every sync (a file linked only from an unchanged page is still
        # there). A page whose body can't be read leaves its links unknown,
        # so no file is taken as deleted this round.
        links_complete = True
        for url, page in list(pages_by_url.items()):
            if "body" in page:
                continue
            try:
                full = canvas.get_page(course_id, url)
            except Exception as e:
                full = None
                yield {"item": url, "status": "failed", "error": str(e)}
                failed_count += 1
            if full is None:
                links_complete = False
                continue
            pages_by_url[url] = {**page, **full, "updated_at": page.get("updated_at", full.get("updated_at", ""))}
        page_remote = [{"id": f"page:{url}", "updated_at": p.get("updated_at", "")} for url, p in pages_by_url.items()]

        # The syllabus has no updated_at of its own: its content hash stands
        # in as the version, so an edit reads as "changed".
        syllabus_remote = []
        if syllabus_html and ingestion.extract_html_page(syllabus_html).strip():
            syllabus_remote = [{"id": f"syllabus:{course_id}", "updated_at": sync.content_hash(syllabus_html)}]

        # Classic Quizzes, with their time limit, attempts and question
        # count. A graded quiz is also an assignment ("online_quiz") with the
        # same text: it is indexed once, as the quiz, so the two don't take
        # two of search's few result slots.
        quizzes_by_id = {str(q["id"]): q for q in quizzes if q.get("id") is not None and q.get("published", True)}
        quiz_assignment_ids = {str(q["assignment_id"]) for q in quizzes_by_id.values() if q.get("assignment_id")}
        quiz_remote = [
            {"id": f"quiz:{qid}", "updated_at": _quiz_version(q)} for qid, q in quizzes_by_id.items()
        ]

        # Assignment instructions, so search can answer "what does Task 2 ask
        # for?" across assignments and "when is it due?". Ones with neither text nor a due date or points are left out;
        # an assignment whose description was cleared then reads as deleted,
        # which matches what the student can see.
        assignments_by_id = {
            str(a["id"]): a
            for a in assignments
            if ((a.get("description") and ingestion.extract_html_page(a["description"]).strip()) or schedule_line(a))
            and str(a["id"]) not in quiz_assignment_ids
        }
        assignment_remote = [
            {"id": f"assignment:{aid}", "updated_at": a.get("updated_at", "")} for aid, a in assignments_by_id.items()
        ]

        # Announcements and discussions: the opening post only. Empty ones
        # (or a locked topic Canvas sends without its text) are left out.
        topics = {
            kind: {str(t["id"]): t for t in listing if t.get("id") is not None and _topic_text(t)}
            for kind, listing in (("announcement", announcements), ("discussion", discussions))
        }
        topic_remote = {
            kind: [{"id": f"{kind}:{tid}", "updated_at": _topic_version(t)} for tid, t in by_id.items()]
            for kind, by_id in topics.items()
        }

        # Files: module items, then the Files tab and attachments (which
        # come with their details), then links in every text above.
        page_links = {url: _linked_file_ids(p.get("body") or "", course_id) for url, p in pages_by_url.items()}
        other_html = [syllabus_html or ""] + [a.get("description") or "" for a in assignments]
        other_html += [t.get("message") or "" for t in [*announcements, *discussions]]
        other_html += [q.get("description") or "" for q in quizzes_by_id.values()]
        known_metas = {str(f["id"]): f for f in files_tab if f.get("id") is not None}
        for topic in [*announcements, *discussions]:
            for attachment in topic.get("attachments") or []:
                if attachment.get("id") is not None:
                    known_metas.setdefault(str(attachment["id"]), attachment)
        wanted_ids = [str(item["content_id"]) for item in module_items if item.get("type") == "File" and item.get("content_id") is not None]
        wanted_ids += list(known_metas)
        wanted_ids += [fid for links in page_links.values() for fid in links]
        wanted_ids += [fid for html in other_html for fid in _linked_file_ids(html, course_id)]
        # A module File item with no content_id can't be looked up: counted
        # as failed.
        for item in module_items:
            if item.get("type") == "File" and item.get("content_id") is None:
                failed_count += 1
                yield {"item": "?", "status": "failed", "error": "module file item has no content_id"}

        # Metadata collection itself can fail per-item (get_file hitting a
        # real Canvas/network error) — isolated here the same as the
        # extraction/indexing failures below, so one bad file doesn't abort
        # the whole course sync.
        file_metas = {}
        all_file_names: dict[str, str] = {}
        unresolved_file_ids: set[str] = set()
        for fid in dict.fromkeys(wanted_ids):
            prefixed_id = f"file:{fid}"
            meta = known_metas.get(fid)
            if meta is None:
                try:
                    meta = canvas.get_file(int(fid))
                except Exception as e:
                    failed_count += 1
                    unresolved_file_ids.add(prefixed_id)
                    yield {"item": fid, "status": "failed", "error": str(e)}
                    continue
            if meta is None:
                # get_file degraded a 403/404 to None: not indexed, and
                # recorded as unresolved so its absence from file_remote
                # isn't mistaken for "deleted from Canvas".
                unresolved_file_ids.add(prefixed_id)
                continue
            display_name = meta.get("display_name", "")
            if Path(display_name).suffix.lower() not in SUPPORTED_FILE_SUFFIXES:
                print(f"[course_sync] skipping unsupported file: {display_name}", file=sys.stderr)
                continue
            file_metas[str(meta["id"])] = meta
            all_file_names[str(meta["id"])] = display_name

        file_remote = [
            {"id": f"file:{fid}", "updated_at": meta.get("updated_at", "")} for fid, meta in file_metas.items()
        ]

        # The course outline, for list_course_items (course_items.py).
        # Like the deletion guards below, an empty listing never wipes one
        # already saved: a hidden modules tab looks the same as no modules.
        origin = canvas.origin()
        file_url = lambda fid: citation_links.canvas_url(origin, course_id, "file", str(fid))
        outline = course_items.outline_rows(structure, page_links=page_links, file_names=all_file_names,
                                            file_url=file_url)
        # By id, not name: a module item's title often isn't its file's name.
        in_modules = {str(item.get("content_id")) for item in module_items if item.get("type") == "File"}
        in_modules |= {fid for url in module_page_urls for fid in page_links.get(url, [])}
        module_topics = {str(item.get("content_id")) for item in module_items if item.get("type") == "Discussion"}
        module_quizzes = {str(item.get("content_id")) for item in module_items if item.get("type") == "Quiz"}
        outline += course_items.group_rows(
            "Other course files",
            sorted((n, file_url(fid)) for fid, n in all_file_names.items() if fid not in in_modules),
            "file",
        )
        outline += course_items.group_rows(
            "Announcements", [(t.get("title") or "", t.get("html_url")) for t in announcements], "announcement"
        )
        outline += course_items.group_rows(
            "Discussions",
            [(t.get("title") or "", t.get("html_url")) for t in discussions if str(t.get("id")) not in module_topics],
            "discussion",
        )
        outline += course_items.group_rows(
            "Quizzes",
            [(q.get("title") or "", q.get("html_url")) for qid, q in quizzes_by_id.items() if qid not in module_quizzes],
            "quiz",
        )
        if outline:
            course_items.save_outline(conn, outline)

        # One row per item type: (type, remote listing, items by unprefixed
        # id, build(prefixed_id, item), label(item, key, prefixed_id) for a
        # failure). Deleted, then indexed, in this order.
        kinds = [
            ("file", file_remote, file_metas, _file_doc, lambda m, key, _: m.get("display_name", key)),
            ("page", page_remote, pages_by_url, partial(_page_doc, course_id), lambda p, key, _: p.get("title", key)),
            ("syllabus", syllabus_remote, {str(course_id): syllabus_html}, _syllabus_doc, lambda *_: "Syllabus"),
            ("assignment", assignment_remote, assignments_by_id, _assignment_doc,
             lambda a, _, pid: a.get("name", pid)),
            *((kind, topic_remote[kind], topics[kind], partial(_topic_doc, kind),
               lambda t, _, pid: t.get("title") or pid) for kind in topics),
            ("quiz", quiz_remote, quizzes_by_id, _quiz_doc, lambda q, _, pid: q.get("title") or pid),
        ]
        # Items whose own fetch failed or 404'd this round: never deleted.
        unresolved = {"file": unresolved_file_ids, "page": unresolved_page_ids}

        # A course indexed in an older chunk format is re-indexed item by
        # item (delete, then add), so it stays searchable meanwhile.
        rebuild = sync.index_format(conn) < indexing.INDEX_FORMAT
        diffs = {kind: sync.diff(conn, kind, remote, rebuild) for kind, remote, *_ in kinds}

        for kind, remote, *_ in kinds:
            # Like the other empty-listing guards: a syllabus that comes back
            # empty or 403 is never taken as "removed". A file is only taken
            # as removed when every page's links could be read.
            deleted = _deletable(remote, diffs[kind]["deleted"], unresolved.get(kind, ()))
            if kind == "file" and not links_complete:
                deleted = []
            for prefixed_id in deleted:
                indexing.delete_ref_doc_nodes(db_path, table_name, prefixed_id)
                sync.forget(conn, prefixed_id, kind)
                removed_count += 1
                yield {"item": prefixed_id, "status": "done"}

        for kind, _, by_id, build, label in kinds:
            for ids, is_changed in ((diffs[kind]["new"], False), (diffs[kind]["changed"], True)):
                for prefixed_id in ids:
                    key = prefixed_id.removeprefix(f"{kind}:")
                    item = by_id[key]
                    try:
                        name = _index_item(conn, db_path, table_name, kind, prefixed_id, is_changed,
                                           partial(build, prefixed_id, item))
                        yield {"item": name, "status": "done"}
                        changed_count += is_changed
                        new_count += not is_changed
                    except Exception as e:
                        failed_count += 1
                        yield {"item": label(item, key, prefixed_id), "status": "failed", "error": str(e)}
        # Stamped even when single items failed: they are retried like any
        # failed item, and a course with one broken file must not rebuild
        # on every sync. A sync that stops early (above) leaves the stamp,
        # so the next one finishes the rebuild.
        sync.set_index_format(conn, indexing.INDEX_FORMAT)
    except Exception as e:
        yield {"done": True, "error": str(e)}
        return

    yield {"done": True, "new": new_count, "changed": changed_count, "removed": removed_count, "failed": failed_count}
