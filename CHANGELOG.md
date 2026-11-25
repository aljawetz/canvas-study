# Changelog

Claude Code updates an installed plugin only when the version in `plugin/.claude-plugin/plugin.json` changes, so
every release bumps it (and `plugin/server/version.py`, which a test keeps equal).

## 0.3.5 (2026-11-24)

- **Clearer skill name:** the skill that answers course questions is now `answering-course-questions` (was `ask`).
  It triggers the same way; you don't need to call it by name.

## 0.3.4 (2026-11-24)

- **Clickable course listings:** "What quizzes are there?" and other full lists now link each item to its page on
  Canvas. Existing courses get the links on their next sync, which happens on its own.
- **Fresh results across sessions:** a search now sees a sync another Claude Code window just finished, instead of
  the index as it was when this window first searched.

## 0.3.3 (2026-11-24)

- **Teacher feedback:** ask "What did my professor say about Lab 2?" or "What feedback did I get?" and Claude reads
  the comments your teachers and TAs left on your submissions, live from Canvas, including on work graded weeks ago.

## 0.3.2 (2026-11-24)

- **Better answers to course-level questions:** "What is this course about?", "What will I learn?" and "What topics
  does it cover?" now find the syllabus (83% of the time in the top results, up from 61%), including when the
  syllabus is a PDF linked from Canvas. Courses indexed before this keep the old version until you re-sync them
  from scratch.

## 0.3.1 (2026-11-24)

- **Safer first install:** when your computer has no `uv`, the plugin now downloads it from uv's GitHub releases and
  checks its SHA-256 before using it, instead of running an install script.
- **Clearer messages:** without a token, before any sync, it no longer claims due dates "from the last sync"; a
  mistyped token is told it "doesn't work" rather than "no longer works".
- **Docs:** the install command needs Claude Code 2.1.275 or later (older versions: add the marketplace first), and
  setup takes about 320 MB of download and 800 MB of disk.

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
