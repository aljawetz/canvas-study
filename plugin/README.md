# Canvas Study

Ask Claude about your Canvas courses: what's due, what your slides, readings and syllabus say, and help studying
the material. Every course fact links back to the Canvas page it came from. It won't write your graded work.

Unofficial. Not made by or affiliated with Instructure, the maker of Canvas.

## Setup

In Claude Code (version 2.1.275 or later; on an older one, run `/plugin marketplace add aljawetz/canvas-study` and
then `/plugin install canvas-study@canvas-study`):

```text
/plugin install canvas-study --marketplace aljawetz/canvas-study
```

Claude Code then asks for your Canvas URL and a Canvas access token (Canvas > Account >
Settings > Approved Integrations > **+ New Access Token**). Then run `/canvas-study:setup` and pick your courses.

Works on macOS, Linux and Windows (on Windows it needs [Git for Windows](https://git-scm.com/downloads/win), which Claude Code also uses).

## What it runs, sends and fetches

**Runs on your computer**

- A local MCP server (`scripts/server`, Python source in `server/`), started by Claude Code.
- `uv`, to install and run that server. If `uv` isn't installed, the plugin downloads a pinned version (0.9.28)
  from uv's GitHub releases, checks it against a SHA-256 written into `scripts/server`, and puts it in its own data
  folder. It doesn't touch your `PATH` or shell profile.
- A `SessionStart` hook (`scripts/session-start`) that points you to setup until a course is indexed, and after
  that tells Claude the names of your indexed courses (read from a file in the data folder; it calls nothing).
- Tesseract, only if you already have it installed, to read text in image-only PDF pages. Without it those pages
  are indexed with whatever text they have.

**Talks to**

- **Your Canvas** (the URL you set): reads your courses, modules, pages, files, assignments, quizzes (settings and
  instructions, never the questions), announcements, discussions and grades with your token. It never writes to Canvas.
- **PyPI**, once, to download the server's Python packages at the exact versions in `server/constraints.txt`.
- **GitHub**, once and only if needed: `uv` itself when it isn't installed (checksum-verified), and the Python 3.13
  that `uv` runs the server on, from uv's own Python builds.
- **Hugging Face**, once, to download the search model
  ([BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5), pinned to one revision).

**Never**

- It calls no AI service itself. What Claude reads to answer you (course text, deadlines, grades, teacher
  feedback) goes to Anthropic as part of your Claude Code conversation, under your own account's data settings.
- It sends no analytics or telemetry.

## Your data

- The Canvas token is stored in your system keychain by Claude Code, not in a file.
- Downloaded course material, the search index, the search model, and the Python and packages the server runs on
  all live in the plugin's data folder, `~/.claude/plugins/data/canvas-study-canvas-study/`. Uninstalling the
  plugin deletes it.

## License

MIT. Source: <https://github.com/aljawetz/canvas-study>
