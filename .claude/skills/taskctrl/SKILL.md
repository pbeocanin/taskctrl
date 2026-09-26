---
name: taskctrl
description: Maintain the TASKCTRL board for all substantive work in this project. Load whenever starting, progressing, or finishing any feature, fix, investigation, or piece of tooling — and keep tasks and their notes current as the work happens.
---

# TASKCTRL board

## Setup — the only two lines to edit when installing this skill globally

- **Board URL:** `http://localhost:8100`
- **Board directory** (holds `server.py`, `tasks.json`, `config.json`): the directory
  this repo was cloned to — e.g. `~/taskctrl`

Inside the taskctrl repo this skill works as-is. Copied to `~/.claude/skills/taskctrl/`
it applies to every project; fill in the two lines above so the paths below resolve.
Install steps live in the repo's `SETUP.md`.

## What this is

Work is tracked on a TASKCTRL board at the board URL, backed by `tasks.json` in the
board directory. **All writes go through the REST API** — multiple agents may work
concurrently, and the server is the only thing that serializes writes and assigns task
numbers safely; two agents editing the JSON directly WILL mint duplicate task numbers.
Reading the file directly is always fine, and the page polls every 15s so API writes
show up on their own.

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
- **subtasks** — the task's checklist, stored as an ARRAY of objects:
  `{"id", "text", "reasoning", "done", "created_at", "completed_at"}`. Checklists go
  HERE, not in notes. This is what "subtask N" refers to. When writing via the API you
  may send either the array (send full objects back, changing only what you mean to
  change) or a markdown string of `- [ ]` / `- [x]` lines, which the server parses and
  matches to existing items by text. Either way the server owns `id`, `created_at`,
  and `completed_at` (stamped when an item flips to done, cleared when unchecked) —
  never fabricate those. `reasoning` on a subtask is optional and usually empty.
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
- **archived** — hides the task from every view except the board's Archived view,
  without deleting it. Set via `PUT {"archived": true|false}`; the server manages
  `archived_at` and doesn't bump `updated_at` for it. Archive only when asked. Prefer
  archiving over deleting for anything with history worth keeping.
- **prod_shape** — a red "handle with care" warning: this task changes the shape of
  data that already exists in production (a schema migration, reinterpreting a
  column, re-encoding stored rows). Set `PUT {"prod_shape": true}` when starting such
  work — it's the one flag you SHOULD set on your own initiative, because it exists
  to warn the user before they deploy. The row gets a pulsing `PROD SHAPE` badge and
  bypasses all board filters. Clear it (`false`) once the change is live. Projects
  without production data can ignore this flag entirely.
- **completed_at** — server-managed: stamped when status flips to `done`, cleared on
  reopen. Powers the board's Today view ("what was closed today"). Never set it in an
  API body — it's ignored there; status changes are the only way it moves.
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

All paths are relative to the board URL from the Setup block.

- **Create:** `POST /api/tasks` with any subset of fields — the server assigns `id`,
  `num`, and timestamps. Never pick a `num` yourself.
- **Update:** `PUT /api/tasks/<id>` with just the fields to change (partial updates
  preserve everything else). The server bumps `updated_at` (except pin-only toggles).
- **Delete:** `DELETE /api/tasks/<id>`.
- **Find an id:** `GET /api/tasks` and match on `num` (task numbers are what the user
  says; ids are what the API wants).

```bash
# start work
curl -s -X POST http://localhost:8100/api/tasks -H 'Content-Type: application/json' \
  -d '{"title": "Rate-limit the search endpoint", "category": "api", "type": "feature",
       "status": "doing", "reasoning": "One client saturated /search over the weekend.",
       "subtasks": "- [ ] pick a strategy\n- [ ] middleware\n- [ ] load-test"}'

# progress: check items off by resending the checklist with [x]
curl -s -X PUT http://localhost:8100/api/tasks/<id> -H 'Content-Type: application/json' \
  -d '{"subtasks": "- [x] pick a strategy\n- [x] middleware\n- [ ] load-test"}'

# finish: notes + status in one call
curl -s -X PUT http://localhost:8100/api/tasks/<id> -H 'Content-Type: application/json' \
  -d '{"status": "done", "notes": "**How it works**\n1. ..."}'
```

Send JSON bodies with `-d @file` or a heredoc when the notes are long — quoting
markdown inline in a shell gets fragile fast.

Every API write shows up on the user's open board as a live toast within a few
seconds, naming the task and what changed — so no-op PUTs are fine (they make no
noise), but avoid churny writes; each one is a notification.

Direct edits to `tasks.json` are the fallback only for when the server is down: edit
the file (it's the source of truth), then bring the server back (`systemctl --user
restart taskctrl` if it was installed as a service, otherwise `python3 server.py` from
the board directory, backgrounded). When editing by hand:
preserve other tasks exactly, keep the `{"tasks": [...]}` shape, use a fresh
12-lowercase-hex `id`, set `num` to max(num)+1, timestamps are local time
(`YYYY-MM-DDTHH:MM:SS`, no timezone). Hand-written subtasks must use the structured
shape (fresh 6-hex `id` per item); a markdown string is only understood by the API,
though the server will migrate one on next load.

NEVER renumber existing tasks — numbers must stay stable so references keep meaning
the same thing (deleting a task retires its number).

## Board configuration

Projects (categories) live in `config.json` next to `server.py`:

```json
{
  "tagline": "mission board · home lab",
  "categories": {
    "slug": { "label": "Display Name", "color": "#a1b2c3" }
  }
}
```

`tagline` is the subtitle under the TASKCTRL wordmark (optional, defaults to
`mission board`). When the user asks to set up or change the projects on their board, create or edit
this file directly — the server re-reads it on every page load, no restart needed.
Use short lowercase slugs, and pick visually distinct colors (6-digit hex) since the
color becomes the category's accent throughout the UI. Renaming a slug orphans tasks
that use the old one, so if the user renames a project, also update the `category`
field on its existing tasks (via the API).

## How the user references tasks and subtasks

Suggested vocabulary — the board displays these numbers, so this is how people
naturally talk about it:

- **"task 3" / "T03"** = the task with `num: 3`, regardless of status or position.
- **"subtask 2 of task 3"** = the 2nd item in that task's `subtasks` array, counting
  from 1 (the board numbers them). Subtask numbers are positional — append new items
  at the end rather than inserting in the middle, unless asked.
- "do tasks 1-3" is an instruction to execute that work: set the task to `doing`, do
  the referenced items, check them off as each completes, record findings in notes.
