---
expect:
  course_id: number
---

{
  "course_id": {{input.course_id}},
  "weighted": true,
  "weighting": "The total is weighted by group: weight_percent is each group's share of the grade.",
  "course_total": {"current_score": 88.4, "current_grade": "B+", "final_score": 51.2, "final_grade": "F"},
  "groups": [
    {"name": "Labs", "weight_percent": 40, "assignments": 6, "graded": 2, "graded_points": 92.0, "graded_possible": 100.0, "graded_percent": 92.0},
    {"name": "Reading responses", "weight_percent": 10, "assignments": 12, "graded": 8, "graded_points": 74.0, "graded_possible": 80.0, "graded_percent": 92.5, "rules": {"drop_lowest": 2}},
    {"name": "Midterm", "weight_percent": 20, "assignments": 1, "graded": 1, "graded_points": 41.0, "graded_possible": 50.0, "graded_percent": 82.0},
    {"name": "Final exam", "weight_percent": 30, "assignments": 1, "graded": 0, "graded_points": 0.0, "graded_possible": 0.0}
  ],
  "notes": [
    "current_score counts only graded work; final_score counts every ungraded assignment as 0.",
    "Each group's graded_points / graded_possible is the plain sum of graded scores in it, before drop rules and excused work; Canvas's course total already applies them."
  ],
  "url": "https://canvas.example.edu/courses/{{input.course_id}}/grades"
}
