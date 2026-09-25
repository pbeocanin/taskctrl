# Setting up TASKCTRL

This is the install guide. It is written so that Claude Code can follow it end to end —
clone the repo, open Claude Code inside it, and say:

> Read SETUP.md and set up TASKCTRL for me.

Humans can follow the same steps by hand. Every step ends with a check, so nothing is
"done" until it's verified.

**Requirements:** Python 3.8+ and git. Nothing else — no pip, no npm.

## 1. Pick where it lives

The cloned repo directory is the install. `server.py`, `tasks.json` (the database),
`config.json` (your projects), and `images/` (attachments) all live side by side there.
`~/taskctrl` is a fine default. Don't move `server.py` on its own — it finds its data
files relative to itself.

```bash
git clone https://github.com/pbeocanin/taskctrl ~/taskctrl && cd ~/taskctrl
python3 --version   # 3.8 or newer
```

## 2. Start it once and check it answers

```bash
python3 server.py
# Task board on http://0.0.0.0:8100 (db: /home/you/taskctrl/tasks.json)
```

In another shell:

```bash
curl -s localhost:8100/api/tasks     # → {"tasks": []}
```

If 8100 is taken, pick another port with `TASKCTRL_PORT=9000 python3 server.py` and
use that port everywhere below. `TASKCTRL_HOST=127.0.0.1` keeps the board off your LAN
if you don't want other machines reaching it.

Stop the foreground server (Ctrl-C) before the next step.

## 3. Keep it running

A board that dies with the terminal is a board nobody updates. Pick one:

**Linux — systemd user service (recommended).** Survives logout and reboot.

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/taskctrl.service <<'UNIT'
[Unit]
Description=TASKCTRL task board

[Service]
WorkingDirectory=%h/taskctrl
ExecStart=/usr/bin/env python3 %h/taskctrl/server.py
# Environment=TASKCTRL_PORT=9000
Restart=on-failure

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now taskctrl
loginctl enable-linger "$USER"        # keep user services alive when logged out
systemctl --user status taskctrl --no-pager | head -5
```

Adjust `%h/taskctrl` if you cloned somewhere else. Logs: `journalctl --user -u taskctrl -f`.

**macOS — launchd.**

```bash
cat > ~/Library/LaunchAgents/com.taskctrl.board.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.taskctrl.board</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/env</string><string>python3</string><string>REPLACE_HOME/taskctrl/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>REPLACE_HOME/taskctrl</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>REPLACE_HOME/taskctrl/server.log</string>
  <key>StandardErrorPath</key><string>REPLACE_HOME/taskctrl/server.log</string>
</dict></plist>
PLIST
sed -i '' "s|REPLACE_HOME|$HOME|g" ~/Library/LaunchAgents/com.taskctrl.board.plist
launchctl load ~/Library/LaunchAgents/com.taskctrl.board.plist
```

**Anything else / quick and dirty.** Runs until reboot:

```bash
cd ~/taskctrl && nohup python3 server.py > server.log 2>&1 &
```

**Check:** `curl -s localhost:8100/api/tasks` answers again, from a fresh shell.

## 4. Configure your projects

The board filters tasks by project ("category"). Ask what the projects are — two to
five short names is typical — and write `config.json` next to `server.py`:

```json
{
  "tagline": "mission board",
  "categories": {
    "api":    { "label": "API",     "color": "#d2a8ff" },
    "webapp": { "label": "Web App", "color": "#ff8a8f" },
    "infra":  { "label": "Infra",   "color": "#79d8e6" }
  }
}
```

- Slugs: short, lowercase, stable — they're stored on every task.
- Colors: visually distinct 6-digit hex; each becomes that project's accent in the UI.
- `tagline` is the subtitle in the header. Optional.

No restart needed; reload the page. **Check:** the project buttons appear in the filter
panel with the right labels.

## 5. Install the skill globally

The skill in `.claude/skills/taskctrl/SKILL.md` teaches Claude Code the workflow: create
a task when work starts, keep the checklist current, write call-ready notes when it
ships. Inside this repo Claude picks it up automatically. To use the board from
**every** project, install it globally and point it at your board:

```bash
mkdir -p ~/.claude/skills/taskctrl
cp ~/taskctrl/.claude/skills/taskctrl/SKILL.md ~/.claude/skills/taskctrl/SKILL.md
```

Then edit the **Setup** block at the top of the copied file: set the board URL (with
your port) and the board directory (your clone path). Those are the only two lines to
touch.

## 6. Make Claude actually use it

A globally installed skill is available, but Claude only loads it when it decides the
task calls for it. To make the board a habit rather than an option, add one line to
`~/.claude/CLAUDE.md` (create the file if it doesn't exist):

```markdown
Load the `taskctrl` skill whenever starting substantive work — every feature, fix, or
investigation gets a task on the TASKCTRL board (http://localhost:8100), kept current
as the work happens.
```

Use your real URL. This is the step people skip and then wonder why the board stays
empty.

## 7. Verify end to end

From a directory that is *not* the taskctrl repo, start a new Claude Code session and
ask it to create a task for something small. The task should appear on the board within
15 seconds, with a toast in the top right announcing it. Delete it afterwards, or keep
it as your first real task.

Or without Claude:

```bash
curl -s -X POST localhost:8100/api/tasks -H 'Content-Type: application/json' \
  -d '{"title": "Hello board", "category": "api", "status": "todo"}'
```

## Notes for the machine you're on

- **LAN access:** the default bind is `0.0.0.0`, so `http://<this-machine's-ip>:8100`
  works from other devices on the network. Set `TASKCTRL_HOST=127.0.0.1` to disable that.
- **No auth.** The API is wide open by design for a trusted LAN. Do not port-forward
  it to the internet.
- **Backups:** `tasks.json` is the whole database; copy it and `images/` and you have
  everything.
- **Upgrading:** automatic. The server checks `origin/main` 30 s after boot and hourly,
  pulls when behind, and restarts itself in place (same pid, so systemd/launchd are
  fine). Data files are gitignored, so a pull never touches them. `curl -s -X POST
  localhost:8100/api/update` forces a check; `curl -s localhost:8100/api/version` shows
  the state. If you've edited `server.py` locally the update is **held** (amber pill in
  the header) until you commit, stash or revert — it never overwrites your changes.
  `Environment=TASKCTRL_AUTOUPDATE=0` in the service turns the automatic pull off.

## Done-checklist for Claude

Before reporting success, confirm and report each of these:

- [ ] `curl localhost:<port>/api/tasks` answers from a fresh shell (server is persistent)
- [ ] `config.json` written with the user's projects; filter buttons visible
- [ ] Skill copied to `~/.claude/skills/taskctrl/` with URL and directory filled in
- [ ] The load-the-skill line is in `~/.claude/CLAUDE.md`
- [ ] A test task round-tripped through the API
- [ ] The user has the board URL (and the LAN URL if they want it on other devices)
