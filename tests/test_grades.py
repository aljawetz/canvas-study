"""grades.py: the course total Canvas computed, and the grade categories."""

import grades

URL = "https://canvas.example.edu/courses/1/grades"


def _course(**extra):
    return {
        "apply_assignment_group_weights": True,
        "enrollments": [{"type": "student", "computed_current_score": 91.5, "computed_current_grade": "A-",
                         "computed_final_score": 60.2, "computed_final_grade": "D"}],
        **extra,
    }


def _assignment(points, score=None, state="graded", **extra):
    submission = {"score": score, "workflow_state": state if score is not None else "unsubmitted"}
    return {"points_possible": points, "submission": submission, **extra}


GROUPS = [
    {"id": 2, "name": "Exams", "position": 2, "group_weight": 50, "rules": {},
     "assignments": [_assignment(100, 80), _assignment(100)]},
    {"id": 1, "name": "Homework", "position": 1, "group_weight": 50, "rules": {"drop_lowest": 1},
     "assignments": [_assignment(10, 9), _assignment(10, 10),
                     _assignment(10, 0, submission=None) | {"submission": {"score": 0, "workflow_state": "graded", "excused": True}},
                     _assignment(10, 10, omit_from_final_grade=True)]},
]


def test_summary_passes_canvas_total_through_and_orders_groups():
    out = grades.summary(_course(), GROUPS, URL)
    assert out["weighted"] is True
    assert out["course_total"] == {"current_score": 91.5, "current_grade": "A-", "final_score": 60.2, "final_grade": "D"}
    assert [g["name"] for g in out["groups"]] == ["Homework", "Exams"]
    assert out["url"] == URL


def test_group_sums_skip_ungraded_excused_and_omitted_work():
    homework, exams = grades.summary(_course(), GROUPS, URL)["groups"]
    assert homework == {"name": "Homework", "weight_percent": 50, "assignments": 3, "graded": 2,
                        "graded_points": 19.0, "graded_possible": 20.0, "graded_percent": 95.0,
                        "rules": {"drop_lowest": 1}}
    assert exams["graded"] == 1 and exams["graded_percent"] == 80.0


def test_unweighted_course_has_no_weights():
    out = grades.summary(_course(apply_assignment_group_weights=False), GROUPS, URL)
    assert out["weighted"] is False
    assert all("weight_percent" not in g for g in out["groups"])


def test_hidden_totals_say_so():
    assert grades.summary(_course(hide_final_grades=True), [], URL)["course_total"]["hidden"] is True
    assert grades.summary({"enrollments": []}, [], URL)["course_total"]["hidden"] is True


def test_empty_weightless_groups_are_left_out_but_an_empty_weighted_one_stays():
    groups = [
        {"id": 1, "name": "Assignments", "position": 1, "group_weight": 0, "assignments": []},
        {"id": 2, "name": "Final Exam", "position": 2, "group_weight": 20, "assignments": []},
        {"id": 3, "name": "Labs", "position": 3, "group_weight": 80, "assignments": [_assignment(10, 9)]},
    ]
    assert [g["name"] for g in grades.summary(_course(), groups, URL)["groups"]] == ["Final Exam", "Labs"]
    unweighted = grades.summary(_course(apply_assignment_group_weights=False), groups, URL)["groups"]
    assert [g["name"] for g in unweighted] == ["Labs"]


def test_a_score_pending_review_is_not_counted_as_graded():
    # A quiz with an essay question: Canvas has a partial score while the
    # teacher hasn't graded it, and the score will still change.
    group = {"id": 1, "name": "Quizzes", "group_weight": 100,
             "assignments": [_assignment(10, 7, state="pending_review"), _assignment(10, 9)]}
    row = grades.summary(_course(), [group], URL)["groups"][0]
    assert (row["assignments"], row["graded"], row["graded_points"], row["graded_possible"]) == (2, 1, 9.0, 10.0)
