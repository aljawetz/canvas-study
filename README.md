# Canvas Study

A Claude Code plugin that lets you ask Claude about your Canvas courses:

- **What's due:** deadlines, submission status, scores and rubrics, read live from Canvas and shown in your time zone.
- **Teacher feedback:** what your professor or TA wrote on your submissions, including on work graded weeks ago.
- **Your grades:** your course total as Canvas computes it, and how much each category (labs, exams) is worth.
- **What the course says:** answers from your slides, readings, syllabus and Canvas pages, each fact linked to the Canvas page it came from.
- **Help studying:** explanations grounded in your own course material. It won't write your graded work for you.

Unofficial. Not made by or affiliated with Instructure, the maker of Canvas.

## Install

In Claude Code (version 2.1.275 or later):

```text
/plugin install canvas-study --marketplace aljawetz/canvas-study
```

On an older Claude Code, add the marketplace first, then install:

```text
/plugin marketplace add aljawetz/canvas-study
/plugin install canvas-study@canvas-study
```

Claude Code asks for your Canvas URL (default `https://canvas.cmu.edu`) and a Canvas access token. To make one:
Canvas > Account > Settings > Approved Integrations > **+ New Access Token**. If that button isn't there, your school
doesn't let students create tokens. Some schools give one on request (ask your IT help desk); without one, Canvas
Study can't connect to your Canvas.

Then run:

```text
/canvas-study:setup
```

Setup downloads what it needs the first time (about 215 MB, and about 500 MB on disk once installed: the search
model and its runtime, the document readers, and Python), lets you pick your courses and indexes them. Then ask away: "What's due this week?", "What does the syllabus say about late work?"

Works on macOS, Linux and Windows. On Windows, install [Git for Windows](https://git-scm.com/downloads/win) first: Claude Code
uses its shell to start the plugin (and for its own Bash tool).

## Privacy

Your token and your course index stay on your computer. What you ask about goes to Claude:

- Your Canvas token is kept in your system keychain by Claude Code.
- Your course index lives in the plugin's data folder (`~/.claude/plugins/data/canvas-study-canvas-study/`).
- When you ask a question, the course text, deadlines, grades and teacher feedback Claude reads to answer it are
  sent to Anthropic as part of your Claude Code conversation, like anything else you show Claude, under your
  account's data settings.
- The plugin itself calls no AI service and sends no telemetry. It talks only to your Canvas (read-only), plus
  one-time downloads of its Python packages (PyPI, at pinned versions), its search model (Hugging Face) and, if your
  computer has no `uv`, `uv` itself and the Python it runs on (GitHub releases, checksum-verified).

The full list of what it runs, sends and fetches is in [plugin/README.md](plugin/README.md).

Uninstalling the plugin deletes its data folder.

## How it works

| Part | What it does |
| --- | --- |
| `plugin/skills/setup` | `/canvas-study:setup`: gets the server and model ready, checks the token, syncs the courses you pick |
| `plugin/skills/answering-course-questions` | Routes course questions to the right tool, cites sources as Canvas links, refuses graded work |
| `plugin/server` | The MCP server: `list_courses`, `search_course_material`, `list_course_items`, `list_assignments`, `get_grades`, `sync_course`, `sync_status` |
| `plugin/scripts/server` | Starts the server with `uv` (installed into the plugin's data folder if missing) |
| `plugin/hooks` | Points you to setup until a course is indexed |

An indexed course keeps itself current: when Claude searches a course whose last sync is over 6 hours old, the
server starts an incremental sync in the background (only new or changed items are re-read) and answers from the
index meanwhile. Deadlines and grades are always read live.

Search is hybrid (vector + keyword, rank fusion) over a local SQLite index (FTS5 for keywords, numpy for vectors), with the
[bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) embedding model running on your machine.

## Development

```bash
uv sync                  # dev environment
uv run pytest            # tests (the first run downloads the model into ./models)
./scripts/export-constraints   # after changing dependencies and `uv lock`: re-pins the students' server
./plugin/dev.sh          # Claude Code with this checkout's plugin and nothing else
claude plugin eval plugin/ --judge-model sonnet   # tool and skill evals (costs a few dollars)
```

To release: bump the version in `plugin/.claude-plugin/plugin.json` and `plugin/server/version.py` (a test keeps
them equal), add a `CHANGELOG.md` entry, merge, then tag it with `claude plugin tag plugin --push`. Installed copies
only update when that version changes.

To try the real install flow from a checkout, add it as a local marketplace:

```text
/plugin marketplace add /path/to/canvas-study
/plugin install canvas-study@canvas-study
```

## License

MIT
