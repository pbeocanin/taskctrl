# ⬢ TASKCTRL

A single-file, zero-dependency task board with a sci-fi mission-console UI — built to be
maintained by AI agents as much as by you.

One Python file. Python stdlib only. One JSON file as the database. No build step, no
npm, no docker, nothing to configure.

```bash
python3 server.py
# → http://localhost:8100
```

## Why it exists

Most task tools assume a human clicking a UI. TASKCTRL assumes your tasks are updated by
**both** you *and* AI coding agents (Claude Code, etc.) working on your machine: the
storage is a human-readable JSON file, every write is available through a tiny REST API
that agents can `curl`, and the repo ships a ready-made Claude Code skill that teaches
agents the full workflow — create a task when work starts, keep checklists current,
write call-ready notes when it ships.

## Features

- **Single file** — server, REST API, and the whole frontend live in `server.py`
- **JSON storage** — `tasks.json` next to the server; re-read on every request, so
  direct edits show up without a restart. The page polls every 15s.
- **Task anatomy** — title, reasoning ("why does this exist"), **subtasks** (first-class
  items with their own optional reasoning and timestamps — add/edit/remove/check them
  in the task view), markdown **notes**, status (`todo`/`doing`/`done`), category,
  type (`feature`/`bug`/`chore`), priority (`high`/`normal`/`low`), image attachments
  (paste/drag-drop/upload)
- **Completion timestamps** — the server stamps `completed_at` on tasks and subtasks
  the moment they're closed (and clears it if reopened), so "when did this actually
  finish" is real data, not a guess from `updated_at`
- **Today view** — one click shows everything closed today: tasks completed today plus
  tasks where subtasks were checked off today
- **Archive** — retire a task from the board without deleting it; archived tasks live
  only in the Archived view and keep their full history
- **Stable task numbers** — every task gets a permanent `T01`-style number, assigned
  server-side under a lock so concurrent writers can't mint duplicates. Numbers are
  never reused or reshuffled.
- **Priority-first sorting** — open tasks sort high → normal → low, active before
  queued, with badges only on non-normal priorities
- **Pinned · Call Agenda** — pin tasks into an agenda section at the top for your next
  update call; pinning moves the row there and ignores filters
- **The HUD** — on wide screens the filters dock as a 3D game-menu side panel:
  mouse-tracking tilt, staggered fly-in, holo sweep, breathing active glow, and a
  chromatic glitch on click. Flat filter rows below 1280px. Exclusive filters for
  project / type / priority / view (Today, Archived), persisted in localStorage.
- **REST API** — everything an agent needs, no auth, meant for localhost/LAN use

## Quickstart

```bash
git clone https://github.com/pbeocanin/taskctrl && cd taskctrl
python3 server.py
```

The server binds `0.0.0.0:8100`, so the board is reachable from other machines on your
network as well as `http://localhost:8100`. `tasks.json` is created on first run.

## REST API

| Method & path                        | What it does                                          |
| ------------------------------------ | ----------------------------------------------------- |
| `GET /api/tasks`                     | full board as JSON                                    |
| `POST /api/tasks`                    | create — server assigns `id`, `num`, timestamps       |
| `PUT /api/tasks/<id>`                | partial update — send only the fields you're changing |
| `DELETE /api/tasks/<id>`             | delete task (and its image files)                     |
| `POST /api/tasks/<id>/images`        | attach an image — raw bytes, `Content-Type: image/*`  |
| `DELETE /api/tasks/<id>/images/<f>`  | remove an attachment                                  |

```bash
curl -X POST localhost:8100/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title": "Fix the login redirect", "type": "bug", "priority": "high",
       "subtasks": "- [ ] reproduce\n- [ ] fix\n- [ ] verify on staging"}'
```

Task shape:

```json
{
  "id": "a1b2c3d4e5f6",
  "num": 7,
  "title": "Fix the login redirect",
  "reasoning": "why this task exists",
  "subtasks": [
    {"id": "9f3a2b", "text": "reproduce", "reasoning": "", "done": true,
     "created_at": "2026-07-31T12:00:00", "completed_at": "2026-07-31T12:20:00"},
    {"id": "c41d07", "text": "fix", "reasoning": "optional — why this step",
     "done": false, "created_at": "2026-07-31T12:00:00"}
  ],
  "notes": "markdown — findings, mechanisms, decisions",
  "status": "todo | doing | done",
  "category": "one of the category slugs from config.json",
  "type": "feature | bug | chore (or empty)",
  "priority": "high | normal | low",
  "pinned": false,
  "archived": false,
  "images": ["a1b2c3d4e5f6-9f3a2b.png"],
  "created_at": "2026-07-31T12:00:00",
  "updated_at": "2026-07-31T12:34:56",
  "completed_at": "2026-07-31T12:34:56 — present only while status is done"
}
```

`subtasks` accepts two input formats on POST/PUT: the structured array above, or a
markdown string of `- [ ]` / `- [x]` lines (handy for agents), which the server parses
and matches to existing items by text. In both cases the server owns `id`,
`created_at`, and `completed_at` — a subtask is stamped when it flips to done and
unstamped if unchecked. Same for tasks: flipping status to `done` sets `completed_at`,
reopening clears it. `PUT {"archived": true}` archives a task (server stamps
`archived_at`); neither archiving nor pinning bumps `updated_at`.

**Concurrent writers:** if more than one process/agent writes at once, use the API —
it's the only path that serializes number assignment. Hand-editing `tasks.json` is fine
when nothing else is writing (or when the server is down).

## Configuring projects (categories)

Projects live in `config.json` next to `server.py` — your local setup, not part of the
repo:

```json
{
  "categories": {
    "backend":   { "label": "Backend",   "color": "#d2a8ff" },
    "marketing": { "label": "Marketing", "color": "#ff8a8f" }
  }
}
```

The server re-reads it on every page load, so edits apply on the next refresh — no
restart. Each category gets its own filter button and accent color (row edge glow,
filter highlight, modal control) generated at runtime. With no `config.json` the board
simply runs without project filters until you add one.

If you use Claude Code, just ask it to set up your projects — the bundled skill knows
the format.

## Using it with Claude Code

The repo ships a skill at [`.claude/skills/taskctrl/`](.claude/skills/taskctrl/SKILL.md)
that teaches Claude Code the whole workflow: when to create tasks, how to keep subtasks
and statuses current while working, the quality bar for notes (could you explain the
feature to a colleague from the notes alone?), and why all writes go through the API.

- Working inside this repo: Claude Code picks the skill up automatically.
- Board running elsewhere / used across projects: copy it to
  `~/.claude/skills/taskctrl/` and adjust the board URL/path at the top.

## License

[MIT](LICENSE)
