---
name: taskctrl
description: Maintain the TASKCTRL board for all substantive work in this project. Load whenever starting, progressing, or finishing any feature, fix, investigation, or piece of tooling — and keep tasks and their notes current as the work happens.
---

# TASKCTRL board

Work is tracked on a TASKCTRL board (default `http://localhost:8100`, backed by
`tasks.json` in the taskctrl directory). **All writes go through the REST API** —
multiple agents may work concurrently, and the server is the only thing that
serializes writes and assigns task numbers safely; two agents editing the JSON
directly WILL mint duplicate task numbers. Reading the file directly is always fine,
and the page polls every 15s so API writes show up on their own.

## The workflow

1. **When work starts** on anything substantive (a feature, a fix, an investigation, a
   tool), create a task for it — status `doing` if starting immediately, `todo` if
   queued. Do this at the start, not retroactively at the end.
2. **As work progresses**, keep the task current: check off checklist items, append
   findings to notes, flip status. Multi-part work gets a `- [ ]` checklist in the
   `subtasks` field rather than a flood of separate tasks.
3. **When work finishes**, set status `done` and make sure the notes meet the standard
   below — that's the moment the notes matter most.

Small conversational asks (answer a question, read a file) don't need tasks. When in
doubt: if the work produces something the user might later have to explain to someone
else, it gets a task.

## Field standards

- **title** — short, concrete, outcome-shaped ("Build X", "Fix Y", "Decide Z").
- **reasoning** — one or two plain sentences on why the task exists. Optional; skip it
  when the why is obvious from the title. Never pad it.
- **subtasks** — the task's checklist, one `- [ ]` / `- [x]` line per subtask
  (markdown, rendered as live checkboxes). Checklists go HERE, not in notes. This is
  what "subtask N" refers to.
- **notes** — **the most important field.** See below.
- **status** — `todo` | `doing` | `done`. Keep it honest; `done` means done and verified.
- **category** — one of the project slugs configured in `config.json` (see "Board
  configuration"). Set it on every new task; the board filters on it, so an
  uncategorized task is invisible in filtered views. If no categories are configured
  yet, leave it empty.
- **type** — `feature` | `bug` | `chore`, or empty for untyped. Orthogonal to category:
  new capability → `feature`; something broken → `bug`; tooling/setup/meta → `chore`.
- **priority** — `high` | `normal` | `low`, default `normal`. The board sorts open
  tasks high → normal → low. Set `high` only when the user signals urgency, `low` for
  back-burner items — don't invent priorities the user didn't signal.
- **pinned** — the user's temporary call agenda. Only pin/unpin when asked, never on
  your own initiative. Pin toggles don't bump `updated_at`.
- **images** — attach via the API only (`POST /api/tasks/<id>/images` with raw image
  bytes); never invent filenames in the JSON.

## The notes standard

Notes are what the user reads later — often on a call, without code open — to explain
what was built. The test: could the user describe the feature's behavior accurately to
a colleague using only the notes?

For anything with logic in it (ranking, scoring, filtering, routing, validation),
notes MUST spell out:

- **How it works** — the actual mechanism, step by step, with real numbers: inputs,
  weights, multipliers, sort orders, tiebreaks. "Ranks by weighted score" is not
  enough; "base 0–40 pts from zip-level averages, ×1.5 if accident <12 months old" is.
- **The gateways** — every threshold, cap, validation gate, or kill-switch the data
  passes through: what trips it, and what happens on failure (fail open or closed?).
- **Key decisions** — choices someone might question later, with the one-line why.
- **Where it lives** — file paths, URLs, branch/PR if relevant.

Use markdown. `- [ ]` lines render as live checkboxes but belong in `subtasks` — keep
notes for prose, findings, and mechanisms.

## Updating the board — API first

- **Create:** `POST /api/tasks` with any subset of fields — the server assigns `id`,
  `num`, and timestamps. Never pick a `num` yourself.
- **Update:** `PUT /api/tasks/<id>` with just the fields to change (partial updates
  preserve everything else). The server bumps `updated_at` (except pin-only toggles).
- **Delete:** `DELETE /api/tasks/<id>`.

Direct edits to `tasks.json` are the fallback only for when the server is down: edit
the file (it's the source of truth), then restart with
`python3 server.py` (from the taskctrl directory, backgrounded). When editing by hand:
preserve other tasks exactly, keep the `{"tasks": [...]}` shape, use a fresh
12-lowercase-hex `id`, set `num` to max(num)+1, timestamps are local time
(`YYYY-MM-DDTHH:MM:SS`, no timezone).

NEVER renumber existing tasks — numbers must stay stable so references keep meaning
the same thing (deleting a task retires its number).

## Board configuration

Projects (categories) live in `config.json` next to `server.py`:

```json
{
  "categories": {
    "slug": { "label": "Display Name", "color": "#a1b2c3" }
  }
}
```

When the user asks to set up or change the projects on their board, create or edit
this file directly — the server re-reads it on every page load, no restart needed.
Use short lowercase slugs, and pick visually distinct colors (6-digit hex) since the
color becomes the category's accent throughout the UI. Renaming a slug orphans tasks
that use the old one, so if the user renames a project, also update the `category`
field on its existing tasks (via the API).

## How the user references tasks and subtasks

- **"task 3" / "T03"** = the task with `num: 3`, regardless of status or position.
- **"subtask 2 of task 3"** = the 2nd `- [ ]`/`- [x]` line in that task's `subtasks`
  field, counting from 1 (the board numbers them). Subtask numbers are positional —
  append new items at the end rather than inserting in the middle, unless asked.
- "do tasks 1-3" is an instruction to execute that work: set the task to `doing`, do
  the referenced items, check them off as each completes, record findings in notes.
