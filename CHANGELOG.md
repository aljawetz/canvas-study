# Changelog

Claude Code updates an installed plugin only when the version in `plugin/.claude-plugin/plugin.json` changes, so
every release bumps it (and `plugin/server/version.py`, which a test keeps equal).

## 0.3.0 (2026-11-24)

- **Smaller, simpler install:** the search engine no longer runs through llama-index. The server installs 66
  packages instead of 111, and 496 MB instead of 818 MB.
- **Same search quality:** measured on a real student's courses, the right page is in the top 5 results as often
  as before (84.9% on 225 short questions).
- **Breaking:** the course index format changed. After updating, run `/canvas-study:setup` and re-sync your
  courses; the old index can't be read.

## 0.2.2 (2026-11-22)

- **Fixed:** installs set up before 0.2.1 never got the course note if they only asked concept questions. The
  server now fetches the course names once at startup when they're missing.

## 0.2.1 (2026-11-22)

- **Claude knows your courses:** at the start of each session Claude is told which courses you have indexed, so
  a question like "In Software Testing, what's a mock?" is answered from your course material instead of from
  general knowledge. On Sonnet and Haiku this took the plugin from rarely used to used every time in testing.

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
