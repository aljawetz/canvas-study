"""Grades for the plugin's get_grades tool, read live from Canvas.

Two sources: the course total Canvas itself computed for the student
(canvas.get_course_grades, include[]=total_scores), and the grade
categories with their weights and drop rules (canvas.list_assignment_groups).

The course total is Canvas's own number and is passed through untouched.
The per-category sums are this module's: points earned over points possible
for the graded work in each category, before drop rules. They are labeled
as such, because Canvas applies drop rules and excused work when it builds
its total, and a model handed two numbers that disagree will pick one.
"""

CURRENT_NOTE = "current_score counts only graded work; final_score counts every ungraded assignment as 0."
GROUP_NOTE = (
    "Each group's graded_points / graded_possible is the plain sum of graded scores in it, before drop rules "
    "and excused work; Canvas's course total already applies them."
)


def _enrollment(course: dict) -> dict:
    for e in course.get("enrollments") or []:
        if e.get("type") in ("student", "StudentEnrollment"):
            return e
    return {}


def course_total(course: dict) -> dict:
    """Canvas's own numbers for the student, or why there are none."""
    if course.get("hide_final_grades"):
        return {"hidden": True, "note": "This course hides its total grade from students."}
    e = _enrollment(course)
    total = {
        "current_score": e.get("computed_current_score"),
        "current_grade": e.get("computed_current_grade"),
        "final_score": e.get("computed_final_score"),
        "final_grade": e.get("computed_final_grade"),
    }
    if all(v is None for v in total.values()):
        return {"hidden": True, "note": "Canvas gave no total grade for this course."}
    return total


def _rules(group: dict) -> dict:
    rules = group.get("rules") or {}
    out = {k: rules[k] for k in ("drop_lowest", "drop_highest") if rules.get(k)}
    if rules.get("never_drop"):
        out["never_drop_count"] = len(rules["never_drop"])
    return out


def _counts(assignment: dict) -> bool:
    return not assignment.get("omit_from_final_grade") and assignment.get("published", True)


def group_row(group: dict, weighted: bool) -> dict:
    assignments = [a for a in group.get("assignments") or [] if _counts(a)]
    earned = possible = 0.0
    graded = 0
    for a in assignments:
        sub = a.get("submission") or {}
        if sub.get("excused") or sub.get("score") is None or sub.get("workflow_state") != "graded":
            continue
        graded += 1
        earned += float(sub["score"])
        possible += float(a.get("points_possible") or 0)
    row = {"name": group.get("name") or f"Group {group.get('id')}"}
    if weighted:
        row["weight_percent"] = group.get("group_weight")
    row.update({
        "assignments": len(assignments),
        "graded": graded,
        "graded_points": round(earned, 2),
        "graded_possible": round(possible, 2),
    })
    if possible:
        row["graded_percent"] = round(100 * earned / possible, 2)
    rules = _rules(group)
    if rules:
        row["rules"] = rules
    return row


def _says_something(row: dict) -> bool:
    """Courses keep empty, weightless groups around (seven empty "Assignments"
    groups in one real course): noise. An empty group with a weight stays:
    "Final Exam, 20%, no assignments yet" is what "how much is the final
    worth?" needs."""
    return bool(row["assignments"] or row.get("weight_percent"))


def summary(course: dict, groups: list[dict], grades_url: str) -> dict:
    weighted = bool(course.get("apply_assignment_group_weights"))
    ordered = sorted(groups, key=lambda g: g.get("position") or 0)
    return {
        "weighted": weighted,
        "weighting": (
            "The total is weighted by group: weight_percent is each group's share of the grade."
            if weighted
            else "The total is not weighted by group: every point counts the same."
        ),
        "course_total": course_total(course),
        "groups": [row for row in (group_row(g, weighted) for g in ordered) if _says_something(row)],
        "notes": [CURRENT_NOTE, GROUP_NOTE],
        "url": grades_url,
    }
