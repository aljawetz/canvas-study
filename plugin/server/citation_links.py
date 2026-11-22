"""Canvas links for citations.

In a terminal "lecture3.pdf · p.12" alone is hard to open, so every search
result carries the URL of the Canvas page it came from.

course_sync.py prefixes every indexed item's id by type ("file:12345",
"page:week-1-overview", "assignment:998240", "syllabus:55016",
"quiz:4410", "announcement:7", "discussion:5"); the prefix
comes off before the id goes into the URL.
"""


def canvas_url(origin: str, course_id: int, item_type: str | None, item_id: str) -> str | None:
    if not item_id:
        return None
    base = f"{origin}/courses/{course_id}"
    if item_type == "syllabus":
        return f"{base}/assignments/syllabus"  # Canvas serves it under assignments, not /pages/
    if item_type == "assignment":
        return f"{base}/assignments/{item_id.removeprefix('assignment:')}"
    if item_type == "page":
        return f"{base}/pages/{item_id.removeprefix('page:')}"
    if item_type == "quiz":
        return f"{base}/quizzes/{item_id.removeprefix('quiz:')}"
    if item_type in ("announcement", "discussion"):
        return f"{base}/discussion_topics/{item_id.removeprefix(item_type + ':')}"
    return f"{base}/files/{item_id.removeprefix('file:')}"
