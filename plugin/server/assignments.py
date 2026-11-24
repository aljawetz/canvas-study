"""Assignments for the plugin's list_assignments tool.

Deadlines, submission status, scores, rubrics and teacher comments are read
live from Canvas (canvas.list_assignments, one call with include[]=submission
and submission_comments; Canvas puts the rubric on every assignment that has
one). The index is a snapshot and
has none of the status, so it is only a fallback when Canvas can't be
reached, and says so.

Window rules: unsubmitted work up to a week past
due still shows, as missed work the student should see; undated work is left
out; at most MAX_ROWS rows.
"""

import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DAYS = 14
MAX_DAYS = 60
OVERDUE_DAYS = 7
MAX_ROWS = 12
# Assignment descriptions are often the whole lab spec pasted into Canvas.
# The longest seen in real courses was 18,666 characters (~4.7k tokens);
# 30,000 keeps a detail result under the 10k-token size Claude Code warns
# about. Past that, the result says it was cut and where to resume.
DESCRIPTION_CHARS = 30_000

# Canvas sends UTC. The model must never convert it: in testing it picked
# the wrong zone (from the canvas.cmu.edu URL) and then did the arithmetic
# wrong too. So every time leaves the server already in the student's zone,
# as a display string to copy, with "in 2 days, 14 hours" next to it.


def machine_zone() -> tzinfo:
    """The zone this machine runs in: TZ, else /etc/localtime's target (macOS
    and most Linux), else the OS's fixed offset."""
    for name in (os.environ.get("TZ"), _localtime_name()):
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                pass
    return datetime.now().astimezone().tzinfo


def _localtime_name() -> str | None:
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return None
    _, sep, name = target.partition("zoneinfo/")
    return name if sep else None


def zone_from_name(name: str | None) -> tzinfo:
    """Canvas's profile time_zone (an IANA name), else the machine's zone."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return machine_zone()


def zone_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or datetime.now(tz).tzname() or "local"


def _span(seconds: float) -> str:
    minutes = int(abs(seconds) // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    plural = lambda n, unit: f"{n} {unit}{'' if n == 1 else 's'}"
    if days:
        parts = [plural(days, "day")] + ([plural(hours, "hour")] if hours else [])
    elif hours:
        parts = [plural(hours, "hour")]
    elif minutes:
        parts = [plural(minutes, "minute")]
    else:
        return "less than a minute"
    return ", ".join(parts)


def when(moment: datetime, tz: tzinfo, now: datetime | None = None) -> dict:
    """One time, three ways: UTC (for reference), local ISO with offset, and
    the string to show the student. Plus how far away it is."""
    now = now or datetime.now(timezone.utc)
    local = moment.astimezone(tz)
    seconds = (moment - now).total_seconds()
    span = _span(seconds)
    return {
        "due_at_utc": moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "due_at_local": local.isoformat(),
        "due_at_display": f"{local:%a, %b} {local.day}, {local.hour % 12 or 12}:{local:%M %p} {local.tzname()}",
        "due_in": f"in {span}" if seconds >= 0 else f"{span} ago",
    }


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_DUE_LINE = re.compile(r"Due [A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2}, \d{4}[^\n]*")


def _due(assignment: dict) -> datetime | None:
    return _parse(assignment.get("due_at"))


def _status(assignment: dict, now: datetime) -> str:
    submission = assignment.get("submission") or {}
    state = submission.get("workflow_state")
    if state == "graded":
        return "graded"
    if state not in (None, "unsubmitted"):
        return "submitted"
    due = _due(assignment)
    return "past due, not submitted" if due and due < now else "not submitted"


def _comments(assignment: dict) -> list[dict]:
    submission = assignment.get("submission") or {}
    return [c for c in submission.get("submission_comments") or [] if (c.get("comment") or "").strip()]


def _from_teacher(assignment: dict, comment: dict) -> bool:
    """A comment someone other than the student wrote. The student's own
    replies stay in the thread but don't count as feedback."""
    return comment.get("author_id") != (assignment.get("submission") or {}).get("user_id")


def teacher_comment_count(assignment: dict) -> int:
    return sum(_from_teacher(assignment, c) for c in _comments(assignment))


def feedback(assignment: dict, tz: tzinfo, now: datetime) -> list[dict]:
    """The comment thread on the student's submission, oldest first: who wrote
    each comment (the student's own show as "you"), when, and the text in
    full. Canvas sends comments as plain text."""
    out = []
    for c in _comments(assignment):
        at = _parse(c.get("created_at"))
        out.append({
            "from": (c.get("author_name") or "teacher") if _from_teacher(assignment, c) else "you",
            **({"at": when(at, tz, now)["due_at_display"]} if at else {}),
            **({"attempt": c["attempt"]} if c.get("attempt") else {}),
            "comment": c["comment"].strip(),
        })
    return out


def _row(assignment: dict, now: datetime, tz: tzinfo) -> dict:
    submission = assignment.get("submission") or {}
    due = _due(assignment)
    comments = teacher_comment_count(assignment)
    return {
        "assignment_id": assignment["id"],
        "name": assignment.get("name") or f"Assignment {assignment['id']}",
        **(when(due, tz, now) if due else {}),
        "points_possible": assignment.get("points_possible"),
        "status": _status(assignment, now),
        "score": submission.get("score"),
        "late": bool(submission.get("late")),
        "missing": bool(submission.get("missing")),
        **({"teacher_comments": comments} if comments else {}),
        "url": assignment.get("html_url"),
    }


def with_feedback(raw: list[dict], now: datetime | None = None, tz: tzinfo | None = None) -> dict:
    """Every assignment the teacher commented on, whatever its due date,
    most recent comment first, each with the whole comment thread: for
    "what feedback did I get?" and "what did my professor say about Lab 2?"
    when Lab 2 is long past the upcoming window."""
    now = now or datetime.now(timezone.utc)
    tz = tz or machine_zone()
    found = []
    for a in raw:
        if not teacher_comment_count(a):
            continue
        latest = max((_parse(c.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) for c in _comments(a))
        found.append((latest, {**_row(a, now, tz), "feedback": feedback(a, tz, now)}))
    found.sort(key=lambda pair: pair[0], reverse=True)
    out = {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timezone": zone_name(tz),
        "assignments": [row for _, row in found[:MAX_ROWS]],
    }
    if not found:
        out["note"] = "No teacher comments on any of the student's submissions in this course."
    if len(found) > MAX_ROWS:
        out["more"] = len(found) - MAX_ROWS
    return out


def upcoming(raw: list[dict], days: int = DEFAULT_DAYS, only_unsubmitted: bool = False, now: datetime | None = None,
             tz: tzinfo | None = None) -> dict:
    """raw: canvas.list_assignments() rows. Every assignment due from
    OVERDUE_DAYS ago to `days` days ahead, submitted or not, each with its
    status, soonest first; the student asks a follow-up to narrow it. With
    only_unsubmitted, just the work still to hand in."""
    now = now or datetime.now(timezone.utc)
    tz = tz or machine_zone()
    days = min(max(int(days), 1), MAX_DAYS)
    start, end = now - timedelta(days=OVERDUE_DAYS), now + timedelta(days=days)
    rows = []
    for a in raw:
        due = _due(a)
        if due is None or not start <= due <= end:
            continue
        row = _row(a, now, tz)
        done = row["status"] in ("submitted", "graded")
        if done and only_unsubmitted:
            continue
        rows.append((due, row))
    rows.sort(key=lambda pair: pair[0])
    shown = [r for _, r in rows[:MAX_ROWS]]
    out = {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timezone": zone_name(tz),
        "days": days,
        "assignments": shown,
    }
    if len(rows) > MAX_ROWS:
        out["more"] = len(rows) - MAX_ROWS
    # Feedback usually lands on work graded weeks ago, outside this window,
    # and in testing Sonnet widened `days` instead of finding with_feedback.
    shown_ids = {r["assignment_id"] for r in shown}
    elsewhere = sum(1 for a in raw if a.get("id") not in shown_ids and teacher_comment_count(a))
    if elsewhere:
        out["feedback_elsewhere"] = (f"{elsewhere} other assignment{'s' if elsewhere != 1 else ''} not listed here "
                                     "have teacher comments: call again with with_feedback=true to read them.")
    return out


def _text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = re.sub(r"[ \t]+", " ", soup.get_text(" "))
    text = re.sub(r" *\n[\n ]*", "\n", text)
    return re.sub(r" ([.,;:!?])", r"\1", text).strip()


def _rubric(assignment: dict) -> dict | None:
    criteria = assignment.get("rubric")
    if not criteria:
        return None
    settings = assignment.get("rubric_settings") or {}
    return {
        "used_for_grading": bool(assignment.get("use_rubric_for_grading")),
        "points_possible": settings.get("points_possible"),
        "criteria": [
            {
                "criterion": c.get("description") or "",
                "detail": c.get("long_description") or "",
                "points": c.get("points"),
                "ratings": [{"rating": r.get("description") or "", "points": r.get("points")} for r in c.get("ratings") or []],
            }
            for c in criteria
        ],
    }


def detail(assignment: dict, now: datetime | None = None, tz: tzinfo | None = None,
           description_offset: int = 0) -> dict:
    """One assignment in full: what to hand in, how it is graded, and where
    the student stands. The score and grade are the student's own.
    description_offset: where to start the description, to read past a cut."""
    now = now or datetime.now(timezone.utc)
    tz = tz or machine_zone()
    submission = assignment.get("submission") or {}

    def local(key: str) -> dict | None:
        moment = _parse(assignment.get(key))
        return when(moment, tz, now) if moment else None

    full = _text(assignment.get("description") or "")
    start = min(max(int(description_offset), 0), len(full))
    description = full[start:start + DESCRIPTION_CHARS]
    end = start + len(description)
    out = {
        "assignment_id": assignment["id"],
        "name": assignment.get("name") or f"Assignment {assignment['id']}",
        "timezone": zone_name(tz),
        "due": local("due_at"),
        "unlock": local("unlock_at"),
        "lock": local("lock_at"),
        "points_possible": assignment.get("points_possible"),
        "description": description,
        "rubric": _rubric(assignment),
        "feedback": feedback(assignment, tz, now),
        "submission": {
            "status": _status(assignment, now),
            "score": submission.get("score"),
            "grade": submission.get("grade"),
            "late": bool(submission.get("late")),
            "missing": bool(submission.get("missing")),
            "submitted_at": submission.get("submitted_at"),
        },
        "url": assignment.get("html_url"),
    }
    if start or end < len(full):
        out["description_cut"] = {
            "shown": [start, end],
            "total_chars": len(full),
            "note": (f"Description is {len(full)} characters; this shows {start} to {end}. "
                     + (f"Call again with description_offset={end} for the rest." if end < len(full) else "")).strip(),
        }
    return out


def from_index_rows(rows: list[dict], reason: str) -> dict:
    """The fallback when Canvas can't be reached: the "Due ... Worth N
    points" line course_sync.schedule_line put in each indexed assignment.
    No status, and as old as the last sync. With no rows the course was
    never synced here, so there is nothing to fall back on: say that rather
    than point at a "last sync" that never happened."""
    if not rows:
        return {
            "stale": True,
            "note": f"{reason}, and this course hasn't been synced on this machine yet, so there are no due dates to "
                    "show. Once Canvas works, they are read live.",
            "assignments": [],
        }
    seen: dict[str, dict] = {}
    for row in rows:
        metadata = row.get("metadata") or {}
        if metadata.get("item_type") != "assignment":
            continue
        doc_id = row.get("doc_id") or ""
        match = _DUE_LINE.search(row.get("text") or "")
        if match and doc_id not in seen:
            seen[doc_id] = {"name": metadata.get("source") or doc_id, "schedule": match.group(0).strip()}
    return {
        "stale": True,
        "note": (
            f"{reason}, so these due dates come from the last sync and may be out of date. "
            "Submission status is not available."
        ),
        "assignments": list(seen.values()),
    }
