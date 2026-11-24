"""assignments.py: what list_assignments (the plugin's MCP tool) returns.

Canvas is replaced by plain dicts shaped like GET /courses/:id/assignments
with include[]=submission, so the window, status, rubric and fallback logic
is tested without a network. canvas.py's own HTTP behavior is covered in
test_canvas.py.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import assignments

LA = ZoneInfo("America/Los_Angeles")

NOW = datetime(2026, 11, 14, 17, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _a(aid, name, due=None, submission=None, **extra):
    row = {
        "id": aid,
        "name": name,
        "due_at": _iso(due) if due else None,
        "points_possible": 50,
        "html_url": f"https://canvas.example.edu/courses/1/assignments/{aid}",
        "description": "<p>Write <b>tests</b>.</p>",
    }
    if submission is not None:
        row["submission"] = submission
    row.update(extra)
    return row


UNSUBMITTED = {"workflow_state": "unsubmitted", "late": False, "missing": False, "score": None, "grade": None}
SUBMITTED = {"workflow_state": "submitted", "late": False, "missing": False, "score": None, "grade": None}
GRADED = {"workflow_state": "graded", "late": True, "missing": False, "score": 42.0, "grade": "42", "submitted_at": "2026-11-02T03:00:00Z"}


def test_upcoming_keeps_the_window_sorted_by_due_date():
    raw = [
        _a(1, "Lab 4", NOW + timedelta(days=6), UNSUBMITTED),
        _a(2, "Lab 3", NOW + timedelta(days=2), UNSUBMITTED),
        _a(3, "Final project", NOW + timedelta(days=30), UNSUBMITTED),
        _a(4, "Reading quiz", None, UNSUBMITTED),
    ]

    out = assignments.upcoming(raw, days=7, now=NOW)

    assert [a["name"] for a in out["assignments"]] == ["Lab 3", "Lab 4"]
    assert out["days"] == 7
    assert out["as_of"] == "2026-11-14T17:00:00Z"
    assert out["assignments"][0]["url"].endswith("/assignments/2")
    assert "due_at" not in out["assignments"][0]  # only the explicit utc/local/display fields


def test_upcoming_shows_submitted_and_unsubmitted_work_by_default():
    raw = [_a(1, "Lab 3", NOW + timedelta(days=2), SUBMITTED), _a(2, "Lab 4", NOW + timedelta(days=3), UNSUBMITTED)]

    shown = assignments.upcoming(raw, days=7, now=NOW)["assignments"]
    assert [(a["name"], a["status"]) for a in shown] == [("Lab 3", "submitted"), ("Lab 4", "not submitted")]
    left = assignments.upcoming(raw, days=7, only_unsubmitted=True, now=NOW)["assignments"]
    assert [a["name"] for a in left] == ["Lab 4"]


def test_upcoming_shows_recent_missed_work_as_overdue():
    raw = [
        _a(1, "Missed lab", NOW - timedelta(days=3), {**UNSUBMITTED, "missing": True}),
        _a(2, "Old lab", NOW - timedelta(days=20), {**UNSUBMITTED, "missing": True}),
        _a(3, "Done lab", NOW - timedelta(days=2), SUBMITTED),
    ]

    shown = assignments.upcoming(raw, days=7, now=NOW)["assignments"]

    # The 20-day-old one is outside the window; last week's work shows either way.
    assert [(a["name"], a["status"]) for a in shown] == [("Missed lab", "past due, not submitted"), ("Done lab", "submitted")]


def test_upcoming_clamps_days_and_caps_rows():
    raw = [_a(i, f"Item {i:02d}", NOW + timedelta(hours=i), UNSUBMITTED) for i in range(1, 20)]

    out = assignments.upcoming(raw, days=999, now=NOW)

    assert out["days"] == assignments.MAX_DAYS
    assert len(out["assignments"]) == assignments.MAX_ROWS
    assert out["more"] == 19 - assignments.MAX_ROWS
    assert assignments.upcoming(raw, days=0, now=NOW)["days"] == 1


def test_scores_are_shown_for_graded_work():
    raw = [_a(1, "Lab 2", NOW - timedelta(days=1), GRADED)]

    row = assignments.upcoming(raw, days=7, now=NOW)["assignments"][0]

    assert row["status"] == "graded"
    assert row["score"] == 42.0
    assert row["points_possible"] == 50
    assert row["late"] is True


def test_detail_has_text_rubric_submission_and_dates():
    raw = _a(
        7,
        "Task 4 deliverable",
        NOW + timedelta(days=5),
        GRADED,
        description="<h2>What to submit</h2><p>A <b>prototype</b> and a report.</p><script>x()</script>",
        unlock_at="2026-11-01T04:00:00Z",
        lock_at=None,
        use_rubric_for_grading=True,
        rubric_settings={"points_possible": 50},
        rubric=[
            {"id": "c1", "description": "Prototype", "long_description": "Covers the main flows", "points": 30,
             "ratings": [{"description": "Full", "points": 30}, {"description": "Partial", "points": 15}]},
            {"id": "c2", "description": "Report", "points": 20, "ratings": None},
        ],
    )

    out = assignments.detail(raw)

    assert out["name"] == "Task 4 deliverable"
    assert "A prototype and a report." in out["description"]
    assert "x()" not in out["description"]
    assert out["rubric"]["used_for_grading"] is True
    assert out["rubric"]["points_possible"] == 50
    assert out["rubric"]["criteria"] == [
        {"criterion": "Prototype", "detail": "Covers the main flows", "points": 30,
         "ratings": [{"rating": "Full", "points": 30}, {"rating": "Partial", "points": 15}]},
        {"criterion": "Report", "detail": "", "points": 20, "ratings": []},
    ]
    assert out["submission"] == {"status": "graded", "score": 42.0, "grade": "42", "late": True, "missing": False,
                                 "submitted_at": "2026-11-02T03:00:00Z"}
    assert out["unlock"]["due_at_utc"] == "2026-11-01T04:00:00Z"
    assert out["url"].endswith("/assignments/7")


ME = 151
PROF = 4127


def _comment(author_id, text, created, name="Prof. Lee", attempt=1):
    return {"id": hash(text) % 10_000, "author_id": author_id, "author_name": name, "comment": text,
            "created_at": _iso(created), "edited_at": None, "attempt": attempt}


def _commented(aid, name, due, comments):
    return _a(aid, name, due, {**GRADED, "user_id": ME, "submission_comments": comments})


def test_detail_gives_the_comment_thread_in_order_in_the_students_zone():
    raw = _commented(9, "Lab 2", NOW - timedelta(days=30), [
        _comment(PROF, "Your test plan skips the error paths.\nSee section 3.", NOW - timedelta(days=20)),
        _comment(ME, "Thanks, will fix", NOW - timedelta(days=19), name="Arthur"),
        _comment(PROF, "   ", NOW - timedelta(days=18)),
    ])

    out = assignments.detail(raw, now=NOW, tz=LA)

    assert out["feedback"] == [
        {"from": "Prof. Lee", "at": "Sun, Oct 25, 10:00 AM PDT", "attempt": 1,
         "comment": "Your test plan skips the error paths.\nSee section 3."},
        {"from": "you", "at": "Mon, Oct 26, 10:00 AM PDT", "attempt": 1, "comment": "Thanks, will fix"},
    ]


def test_detail_without_comments_has_empty_feedback():
    assert assignments.detail(_a(8, "Reading response", None, UNSUBMITTED), now=NOW)["feedback"] == []


def test_rows_count_only_the_teachers_comments():
    raw = [_commented(1, "Lab 2", NOW - timedelta(days=1), [
        _comment(PROF, "Nice", NOW), _comment(ME, "thanks", NOW), _comment(PROF, "One more thing", NOW)]),
        _a(2, "Lab 3", NOW + timedelta(days=1), UNSUBMITTED)]

    rows = assignments.upcoming(raw, days=7, now=NOW)["assignments"]

    assert rows[0]["teacher_comments"] == 2
    assert "teacher_comments" not in rows[1]


def test_upcoming_points_to_feedback_outside_its_window():
    raw = [_commented(1, "Lab 1", NOW - timedelta(days=60), [_comment(PROF, "Old note", NOW - timedelta(days=50))]),
           _commented(2, "Lab 2", NOW - timedelta(days=1), [_comment(PROF, "Shown", NOW)]),
           _commented(3, "Lab 3", NOW - timedelta(days=40), [_comment(ME, "my own reply", NOW)])]

    out = assignments.upcoming(raw, days=7, now=NOW)

    assert [a["name"] for a in out["assignments"]] == ["Lab 2"]
    assert out["feedback_elsewhere"].startswith("1 other assignment not listed")
    assert "with_feedback=true" in out["feedback_elsewhere"]
    assert "feedback_elsewhere" not in assignments.upcoming(raw[1:], days=7, now=NOW)


def test_with_feedback_lists_commented_work_of_any_date_latest_comment_first():
    raw = [
        _commented(1, "Lab 1", NOW - timedelta(days=90), [_comment(PROF, "Old note", NOW - timedelta(days=80))]),
        _commented(2, "Lab 2", NOW - timedelta(days=60), [_comment(PROF, "Recent note", NOW - timedelta(days=2))]),
        _commented(3, "Lab 3", NOW - timedelta(days=1), [_comment(ME, "Is this late?", NOW)]),
        _a(4, "Lab 4", NOW - timedelta(days=1), GRADED),
    ]

    out = assignments.with_feedback(raw, now=NOW, tz=LA)

    assert [a["name"] for a in out["assignments"]] == ["Lab 2", "Lab 1"]
    assert out["assignments"][0]["feedback"][0]["comment"] == "Recent note"
    assert out["assignments"][0]["score"] == 42.0
    assert "note" not in out


def test_with_feedback_says_when_there_is_none():
    out = assignments.with_feedback([_a(4, "Lab 4", NOW, GRADED)], now=NOW, tz=LA)

    assert out["assignments"] == []
    assert "No teacher comments" in out["note"]


def test_detail_without_a_rubric_says_so():
    out = assignments.detail(_a(8, "Reading response", None, UNSUBMITTED))

    assert out["rubric"] is None
    assert out["due"] is None


def test_detail_keeps_a_long_lab_spec_whole():
    # L3 - Observability was 18,666 characters and lost 79% of it to the old 4,000 cap.
    spec = "".join(f"<p>Step {i}: run the thing and check it.</p>" for i in range(600))
    out = assignments.detail(_a(9, "Lab", None, UNSUBMITTED, description=spec))

    assert "Step 599" in out["description"]
    assert "description_cut" not in out


def test_detail_says_when_it_cuts_and_how_to_read_the_rest():
    words = " ".join(f"w{i}" for i in range(10_000))  # ~70k characters
    raw = _a(9, "Essay", None, UNSUBMITTED, description=f"<p>{words}</p>")

    first = assignments.detail(raw)
    cut = first["description_cut"]
    assert len(first["description"]) == assignments.DESCRIPTION_CHARS
    assert cut["shown"] == [0, assignments.DESCRIPTION_CHARS]
    assert f"description_offset={assignments.DESCRIPTION_CHARS}" in cut["note"]

    pieces, offset = [first["description"]], cut["shown"][1]
    while offset < cut["total_chars"]:
        page = assignments.detail(raw, description_offset=offset)
        pieces.append(page["description"])
        offset = page["description_cut"]["shown"][1]
    assert "".join(pieces) == assignments._text(raw["description"])
    assert "description_offset=" not in page["description_cut"]["note"]


def test_fallback_reads_due_lines_from_indexed_assignments():
    rows = [
        {"doc_id": "assignment:11", "text": "Due date and points\nDue Fri, Nov 20, 2026 at 11:59 PM EST. Worth 50 points.\nWhat to do...",
         "metadata": {"source": "Lab 3: Branch Coverage", "item_type": "assignment"}},
        {"doc_id": "assignment:11", "text": "More of the description", "metadata": {"source": "Lab 3: Branch Coverage", "item_type": "assignment"}},
        {"doc_id": "file:5", "text": "Due Fri, Nov 20, 2026 at noon.", "metadata": {"source": "syllabus.pdf", "item_type": "file"}},
        {"doc_id": "assignment:12", "text": "No date here", "metadata": {"source": "Participation", "item_type": "assignment"}},
    ]

    out = assignments.from_index_rows(rows, reason="Canvas could not be reached")

    assert out["stale"] is True
    assert "Canvas could not be reached" in out["note"]
    assert out["assignments"] == [
        {"name": "Lab 3: Branch Coverage", "schedule": "Due Fri, Nov 20, 2026 at 11:59 PM EST. Worth 50 points."}
    ]


# --- time zones: the server converts, the model only copies ----------------


def test_due_times_are_given_in_the_students_zone():
    due = datetime(2026, 11, 17, 7, 59, 59, tzinfo=timezone.utc)  # Mon 11:59 PM in Los Angeles
    out = assignments.upcoming([_a(1, "Field project", due, UNSUBMITTED)], days=7, now=NOW, tz=LA)

    row = out["assignments"][0]
    assert out["timezone"] == "America/Los_Angeles"
    assert row["due_at_utc"] == "2026-11-17T07:59:59Z"
    assert row["due_at_local"] == "2026-11-16T23:59:59-08:00"
    assert row["due_at_display"] == "Mon, Nov 16, 11:59 PM PST"
    assert row["due_in"] == "in 2 days, 14 hours"  # 2d 14h 59m, rounded down


def test_display_follows_daylight_saving():
    due = datetime(2026, 10, 1, 6, 59, tzinfo=timezone.utc)
    when = assignments.when(due, LA, now=due - timedelta(minutes=45))

    assert when["due_at_display"] == "Wed, Sep 30, 11:59 PM PDT"
    assert when["due_in"] == "in 45 minutes"


@pytest.mark.parametrize(
    "delta, text",
    [(timedelta(hours=-3), "3 hours ago"), (timedelta(days=-1, hours=-2), "1 day, 2 hours ago"),
     (timedelta(seconds=30), "in less than a minute"), (timedelta(days=9), "in 9 days")],
)
def test_due_in_reads_naturally(delta, text):
    assert assignments.when(NOW + delta, LA, now=NOW)["due_in"] == text


def test_detail_gives_local_times_for_every_date():
    raw = _a(3, "Essay", datetime(2026, 11, 20, 4, 59, tzinfo=timezone.utc), UNSUBMITTED,
             unlock_at="2026-11-01T07:00:00Z", lock_at=None)

    out = assignments.detail(raw, now=NOW, tz=LA)

    assert out["timezone"] == "America/Los_Angeles"
    assert out["due"]["due_at_display"] == "Thu, Nov 19, 8:59 PM PST"
    assert out["unlock"]["due_at_display"] == "Sun, Nov 1, 12:00 AM PDT"
    assert out["lock"] is None


def test_zone_from_name_falls_back_to_the_machine():
    assert assignments.zone_from_name("America/Denver") == ZoneInfo("America/Denver")
    assert assignments.zone_from_name("Not/AZone") == assignments.machine_zone()
    assert assignments.zone_from_name(None) == assignments.machine_zone()
