# Changelog

Claude Code updates an installed plugin only when the version in `plugin/.claude-plugin/plugin.json` changes, so
every release bumps it (and `plugin/server/version.py`, which a test keeps equal).

## 0.2.0 (2026-11-22)

- **Grades:** `get_grades` returns your course total as Canvas computes it and how much each category is worth.
- **Quizzes:** sync indexes Classic Quiz instructions and settings (time limit, attempts, questions), never the
  questions themselves.
- **Stays current:** searching a course whose last sync is over 6 hours old refreshes it in the background.
- **Sync failures say what failed:** `sync_status` names the last items that couldn't be read and why.
- **Fixed:** announcement and discussion citations linked to a page that didn't exist.
- **Fixed:** after running setup mid-session, Claude could keep saying Canvas Study wasn't set up.

## 0.1.0

First release: deadlines and submission status live from Canvas, search over slides, readings, syllabus and pages
with Canvas links, and no help writing graded work.
