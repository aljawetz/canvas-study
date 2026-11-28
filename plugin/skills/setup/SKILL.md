---
name: setup
description: Set up Canvas Study on this computer, from installing what it needs to connecting Canvas and indexing the student's courses. Use when the student runs /canvas-study:setup, asks to set up, connect or sync Canvas Study or Canvas, wants to add a course, or when a Canvas Study tool says no Canvas token is set up or a course is not indexed.
---

# Set up Canvas Study

The student is not a developer. Talk in plain words, one short message per step, and say what is happening while
something slow runs. Never ask for the Canvas token in the chat: it goes in the plugin's settings, which keep it
in the system keychain.

## 1. Get the course server ready

Run this with Bash (timeout 600000):

```
CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/server" --download-model
```

The first time, it downloads the server's packages and Python (about 85 MB) and the search model (about 130 MB), and
`uv` if the computer has none, so tell the student it can take a minute or two on a good connection, longer on a
slow one, and uses about 700 MB of disk. Later runs finish in seconds. If it
fails, show the last few lines of its output and stop; a network error usually means trying again later. On
Windows, if Bash itself isn't available, the student needs Git for Windows (https://git-scm.com/downloads/win):
Claude Code uses its shell to run the server. Ask them to install it, restart Claude Code and run setup again.

## 2. Make sure the tools are connected

If the Canvas Study tools (`list_courses`, `sync_course`, `sync_status`) are not available in this session, the
server could not start before step 1 finished. Ask the student to run `/mcp reconnect all` (or open `/mcp`, pick
canvas-study and choose Reconnect) and then `/canvas-study:setup` again, and stop here. Not a restart: after a
failed start, new sessions skip the server for about 15 minutes, while reconnecting works right away.

## 3. Check Canvas

Call `list_courses`. If its note says there is no Canvas token, or that the token doesn't work, walk the
student through it:

1. In Canvas: Account > Settings > Approved Integrations > **+ New Access Token**. Purpose: "Canvas Study".
   Copy the token. If the button isn't there, their school doesn't let students create tokens: say so plainly,
   suggest asking the school's IT help desk for one (some schools give them on request), and stop. Without a
   token Canvas Study can't connect.
2. In Claude Code: `/plugin` > Installed > canvas-study > **Configure options**, paste it as the Canvas access
   token (and change the Canvas URL if their school isn't canvas.cmu.edu).
3. Run `/reload-plugins`, then `/canvas-study:setup` again.

Stop here until they come back.

## 4. Pick the courses

Show the courses where `indexed` is false as a short numbered list, by name. Ask which ones to set up; suggest
the courses they are taking this term. Mention the courses already indexed in one line, and offer to refresh them
too (a re-sync only downloads what changed in Canvas).

## 5. Sync

Call `sync_course` for each chosen course. Syncs run in the background: about 30 seconds for a small course,
several minutes for a large one.

Then follow progress: run `sleep 20` with Bash, call `sync_status`, and give a one-line update
("Requirements: 40 items so far"). Repeat until every sync is done or failed, for at most about 5 minutes. If some
are still running after that, tell the student they keep going in the background and they can ask "is my sync
done?" any time.

For a failed sync, say which course and the error in plain words. An item or two failing (`items_failed`) is
normal: some files in Canvas can't be read, and the rest of the course still works. `failures` names the
last few that failed and why; mention them by name if the student asks what's missing.

## 6. Done

In two or three lines: which courses are ready, and a few things to try, for example:

- "What's due this week?"
- "What does the syllabus say about late work?"
- "Explain the main idea of this week's reading."

To add a course later, they run `/canvas-study:setup` again. Running it again also refreshes the courses already
set up (only what changed in Canvas is downloaded).
