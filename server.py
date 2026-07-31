#!/usr/bin/env python3
"""Local task board — stdlib only.

Tasks live in tasks.json next to this file. The file is reloaded on every
request, so edits made directly to it (by hand or by Claude) show up on the
next refresh without restarting the server.

Run:  python3 server.py   (binds 0.0.0.0:8100 — reachable from your LAN)
"""

import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = 8100
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tasks.json")

_lock = threading.Lock()

STATUSES = ("todo", "doing", "done")
PRIORITIES = ("high", "normal", "low")
TYPES = ("feature", "bug", "chore")

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_config():
    """Local board setup from config.json (categories, tagline). Re-read on every
    use so config edits apply without a restart. No file → defaults."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def categories():
    cats = load_config().get("categories", {})
    return cats if isinstance(cats, dict) else {}

IMAGES_DIR = os.path.join(BASE_DIR, "images")
IMAGE_CT_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
IMAGE_EXT_CT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "gif": "image/gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def sniff_image_ext(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def load_db():
    if not os.path.exists(DB_PATH):
        return {"tasks": []}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        db = json.load(f)
    # one-time migration: number tasks that predate the num field, by creation date
    unnumbered = [t for t in db["tasks"] if "num" not in t]
    if unnumbered:
        nxt = max((t.get("num", 0) for t in db["tasks"]), default=0)
        for t in sorted(unnumbered, key=lambda t: t.get("created_at", "")):
            nxt += 1
            t["num"] = nxt
        save_db(db)
    return db


def save_db(db):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DB_PATH)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Handler(BaseHTTPRequestHandler):
    server_version = "TaskBoard/2.0"

    # ---- helpers -------------------------------------------------------

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _task_id(self):
        m = re.fullmatch(r"/api/tasks/([\w-]+)", self.path)
        return m.group(1) if m else None

    def log_message(self, fmt, *args):
        pass  # keep stdout quiet

    # ---- routes --------------------------------------------------------

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            tagline = str(load_config().get("tagline") or "mission board")
            page = (PAGE
                    .replace("__CATEGORIES__", json.dumps(categories()))
                    .replace("__TAGLINE__", tagline.replace("&", "&amp;").replace("<", "&lt;")))
            self._send(200, page, "text/html; charset=utf-8")
        elif self.path == "/api/tasks":
            with _lock:
                self._json(200, load_db())
        elif self.path.startswith("/images/"):
            name = self.path[len("/images/"):]
            path = os.path.join(IMAGES_DIR, name)
            if not re.fullmatch(r"[\w-]+\.(png|jpe?g|webp|gif)", name) or not os.path.exists(path):
                return self._json(404, {"error": "not found"})
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", IMAGE_EXT_CT[name.rsplit(".", 1)[1].lower()])
            self.send_header("Content-Length", str(len(data)))
            # filenames carry a random suffix and are never reused → safe to cache hard
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        m = re.fullmatch(r"/api/tasks/([\w-]+)/images", self.path)
        if m:
            return self._upload_image(m.group(1))
        if self.path != "/api/tasks":
            return self._json(404, {"error": "not found"})
        body = self._read_body()
        title = (body.get("title") or "").strip()
        if not title:
            return self._json(400, {"error": "title is required"})
        task = {
            "id": uuid.uuid4().hex[:12],
            "num": 0,  # assigned below under the lock
            "title": title,
            "reasoning": (body.get("reasoning") or "").strip(),
            "subtasks": (body.get("subtasks") or "").strip(),
            "notes": (body.get("notes") or "").strip(),
            "status": body.get("status") if body.get("status") in STATUSES else "todo",
            "category": body.get("category") if body.get("category") in categories() else "",
            "priority": body.get("priority") if body.get("priority") in PRIORITIES else "normal",
            "type": body.get("type") if body.get("type") in TYPES else "",
            "pinned": bool(body.get("pinned")),
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        with _lock:
            db = load_db()
            task["num"] = max((t.get("num", 0) for t in db["tasks"]), default=0) + 1
            db["tasks"].append(task)
            save_db(db)
        self._json(201, task)

    def _upload_image(self, tid):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return self._json(400, {"error": "empty body — send raw image bytes"})
        if length > MAX_IMAGE_BYTES:
            return self._json(413, {"error": "image too large (10 MB cap)"})
        data = self.rfile.read(length)
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = IMAGE_CT_EXT.get(ct) or sniff_image_ext(data)
        if not ext:
            return self._json(415, {"error": "not a supported image (png/jpg/webp/gif)"})
        with _lock:
            db = load_db()
            task = next((t for t in db["tasks"] if t["id"] == tid), None)
            if not task:
                return self._json(404, {"error": "no such task"})
            os.makedirs(IMAGES_DIR, exist_ok=True)
            name = f"{tid}-{uuid.uuid4().hex[:6]}.{ext}"
            with open(os.path.join(IMAGES_DIR, name), "wb") as f:
                f.write(data)
            task.setdefault("images", []).append(name)
            task["updated_at"] = now_iso()  # an attachment is content, unlike a pin
            save_db(db)
        self._json(201, task)

    def do_PUT(self):
        tid = self._task_id()
        if not tid:
            return self._json(404, {"error": "not found"})
        body = self._read_body()
        with _lock:
            db = load_db()
            task = next((t for t in db["tasks"] if t["id"] == tid), None)
            if not task:
                return self._json(404, {"error": "no such task"})
            for field in ("title", "reasoning", "subtasks", "notes"):
                if field in body:
                    task[field] = str(body[field]).strip()
            if body.get("status") in STATUSES:
                task["status"] = body["status"]
            if "category" in body:
                task["category"] = body["category"] if body["category"] in categories() else ""
            if "priority" in body:
                task["priority"] = body["priority"] if body["priority"] in PRIORITIES else "normal"
            if "type" in body:
                task["type"] = body["type"] if body["type"] in TYPES else ""
            if "pinned" in body:
                task["pinned"] = bool(body["pinned"])
            if not task["title"]:
                return self._json(400, {"error": "title is required"})
            if set(body) != {"pinned"}:  # pin toggles are metadata, not work
                task["updated_at"] = now_iso()
            save_db(db)
        self._json(200, task)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/tasks/([\w-]+)/images/([\w.-]+)", self.path)
        if m:
            tid, name = m.group(1), m.group(2)
            with _lock:
                db = load_db()
                task = next((t for t in db["tasks"] if t["id"] == tid), None)
                if not task or name not in task.get("images", []):
                    return self._json(404, {"error": "no such image"})
                task["images"].remove(name)
                task["updated_at"] = now_iso()
                save_db(db)
                try:
                    os.remove(os.path.join(IMAGES_DIR, name))
                except OSError:
                    pass
            return self._json(200, task)

        tid = self._task_id()
        if not tid:
            return self._json(404, {"error": "not found"})
        with _lock:
            db = load_db()
            gone = next((t for t in db["tasks"] if t["id"] == tid), None)
            if not gone:
                return self._json(404, {"error": "no such task"})
            db["tasks"] = [t for t in db["tasks"] if t["id"] != tid]
            save_db(db)
            for name in gone.get("images", []):  # no orphan files
                try:
                    os.remove(os.path.join(IMAGES_DIR, name))
                except OSError:
                    pass
        self._json(200, {"ok": True})


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TASKCTRL</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='13' fill='%23070c13'/%3E%3Cpath d='M32 7 L53 19.5 V44.5 L32 57 L11 44.5 V19.5 Z' fill='none' stroke='%2341d8f7' stroke-width='4.5' stroke-linejoin='round'/%3E%3Ccircle cx='32' cy='32' r='7.5' fill='%2341f0a5'/%3E%3C/svg%3E">
<style>
  :root {
    --bg0: #04070c; --bg1: #070c13;
    --panel: #0a111a; --panel2: #0e1722; --inset: #060b11;
    --line: #16222f; --line2: #23374a;
    --ink: #d8e4ef; --dim: #93a7ba; --muted: #51667b;
    --cyan: #41d8f7; --cyan-dim: #1a6d80;
    --amber: #ffb454; --green: #41f0a5; --red: #ff5f66;
    --mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
    --sans: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  * { box-sizing: border-box; }
  html { scrollbar-color: var(--line2) var(--bg0); }
  body {
    margin: 0; min-height: 100vh; background: var(--bg0); color: var(--ink);
    font: 15px/1.55 var(--sans);
    background-image:
      radial-gradient(1200px 500px at 50% -10%, rgba(65,216,247,.05), transparent 60%),
      linear-gradient(var(--bg1), var(--bg0) 400px);
  }
  /* faint scanlines over everything */
  body::after {
    content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 999;
    background: repeating-linear-gradient(0deg, rgba(255,255,255,.012) 0 1px, transparent 1px 3px);
  }

  .wrap { max-width: 1020px; margin: 0 auto; padding: 20px 18px 90px; }

  /* ---------- top console bar ---------- */
  .console {
    display: flex; align-items: stretch; gap: 14px; flex-wrap: wrap;
    border: 1px solid var(--line); background: linear-gradient(180deg, var(--panel2), var(--panel));
    border-radius: 4px; padding: 12px 16px; margin-bottom: 22px; position: relative;
  }
  .console::before, .console::after {
    content: ""; position: absolute; width: 12px; height: 12px; border: 1px solid var(--cyan);
    opacity: .8;
  }
  .console::before { top: -1px; left: -1px; border-right: 0; border-bottom: 0; }
  .console::after { bottom: -1px; right: -1px; border-left: 0; border-top: 0; }
  .brand { display: flex; flex-direction: column; justify-content: center; margin-right: 8px; }
  .brand .name {
    font: 700 17px var(--mono); letter-spacing: .22em; color: var(--ink);
  }
  .brand .name .hex { color: var(--cyan); text-shadow: 0 0 8px rgba(65,216,247,.6); }
  .brand .tag { font: 10px var(--mono); letter-spacing: .3em; color: var(--muted); text-transform: uppercase; }
  .readouts { display: flex; gap: 10px; align-items: center; flex: 1; flex-wrap: wrap; }
  .ro {
    min-width: 74px; padding: 5px 12px 6px; border: 1px solid var(--line);
    background: var(--inset); border-radius: 3px; text-align: center;
  }
  .ro .l { display: block; font: 9px var(--mono); letter-spacing: .25em; color: var(--muted); }
  .ro .v { display: block; font: 700 20px/1.2 var(--mono); font-variant-numeric: tabular-nums; }
  .ro.open  .v { color: var(--amber); text-shadow: 0 0 10px rgba(255,180,84,.35); }
  .ro.doing .v { color: var(--cyan);  text-shadow: 0 0 10px rgba(65,216,247,.35); }
  .ro.done  .v { color: var(--green); text-shadow: 0 0 10px rgba(65,240,165,.35); }
  .clockbox { margin-left: auto; display: flex; flex-direction: column; justify-content: center; text-align: right; }
  .clockbox .t { font: 700 18px var(--mono); color: var(--dim); font-variant-numeric: tabular-nums; letter-spacing: .1em; }
  .clockbox .d { font: 10px var(--mono); letter-spacing: .25em; color: var(--muted); }

  /* ---------- buttons ---------- */
  button {
    font: 600 12px var(--mono); letter-spacing: .12em; text-transform: uppercase;
    color: var(--dim); background: var(--panel2); border: 1px solid var(--line2);
    border-radius: 3px; padding: 8px 14px; cursor: pointer; transition: all .12s;
  }
  button:hover { color: var(--cyan); border-color: var(--cyan-dim); box-shadow: 0 0 10px rgba(65,216,247,.15); }
  button.primary {
    color: #032027; background: var(--cyan); border-color: var(--cyan);
    box-shadow: 0 0 14px rgba(65,216,247,.35);
  }
  button.primary:hover { background: #6ae2fb; color: #032027; }
  button.danger:hover { color: var(--red); border-color: var(--red); box-shadow: 0 0 10px rgba(255,95,102,.2); }
  .console button.primary { align-self: center; }

  /* ---------- panels ---------- */
  .panel { margin-bottom: 30px; }
  .panel-head {
    display: flex; align-items: center; gap: 12px; margin-bottom: 10px;
    font: 700 11px var(--mono); letter-spacing: .3em; text-transform: uppercase;
  }
  .panel-head .rule { flex: 1; height: 1px; background: linear-gradient(90deg, var(--line2), transparent); }
  .panel-head .n { font-weight: 400; color: var(--muted); letter-spacing: .15em; }
  #ops .panel-head { color: var(--cyan); }
  #log .panel-head { color: var(--green); }
  #pins .panel-head { color: var(--amber); }
  #pins .row { border-color: rgba(255,180,84,.35); background: linear-gradient(90deg, rgba(255,180,84,.05), var(--panel) 45%); }

  /* pin toggle */
  .pinbtn {
    border: none; background: none; padding: 2px 4px; font-size: 14px; line-height: 1;
    filter: grayscale(1); opacity: .3; cursor: pointer; transition: all .12s;
  }
  .pinbtn:hover { filter: none; opacity: .85; box-shadow: none; transform: scale(1.15); }
  .pinbtn.on { filter: none; opacity: 1; }
  .m-pin { font: 700 10px var(--mono); letter-spacing: .2em; color: var(--amber); margin-left: 8px; }

  .rows { display: flex; flex-direction: column; gap: 6px; }
  .row {
    display: grid; grid-template-columns: 26px 38px 1fr auto auto auto auto auto auto; gap: 12px; align-items: center;
    border: 1px solid var(--line); border-left-width: 3px; background: var(--panel);
    border-radius: 3px; padding: 11px 14px; cursor: pointer; transition: all .12s;
    position: relative; overflow: hidden;
  }
  .row::after {
    content: ""; position: absolute; top: 0; bottom: 0; right: 0; width: 46px;
    pointer-events: none; border-radius: 0 3px 3px 0;
  }
  .row:hover { background: var(--panel2); border-color: var(--line2); transform: translateX(2px); }
  .row.todo  { border-left-color: var(--amber); }
  .row.doing { border-left-color: var(--cyan); }
  .row.done  { border-left-color: var(--green); background: linear-gradient(90deg, rgba(65,240,165,.04), var(--panel) 40%); }
  .row .tnum { font: 700 12px var(--mono); color: var(--muted); letter-spacing: .08em; font-variant-numeric: tabular-nums; }
  .row:hover .tnum { color: var(--cyan); }
  .row .title { font-weight: 600; font-size: 14.5px; }
  .row.done .title { color: var(--dim); }
  .row .when { font: 11px var(--mono); color: var(--muted); white-space: nowrap; }
  .chip {
    font: 10px var(--mono); letter-spacing: .1em; color: var(--dim);
    border: 1px solid var(--line2); border-radius: 2px; padding: 2px 8px; white-space: nowrap;
  }
  .chip.full { color: var(--green); border-color: rgba(65,240,165,.4); }

  /* ---------- HUD filter panel (game-menu mode on wide screens) ---------- */
  .hud { margin-bottom: 20px; }
  .hud-title { display: none; }
  .hud-inner { display: flex; flex-direction: column; gap: 6px; }
  .hud-sec { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .hud-lab { width: 72px; font: 700 9px var(--mono); letter-spacing: .25em; color: var(--muted); text-transform: uppercase; }
  .hud-fx { display: none; }

  @media (min-width: 1280px) {
    .wrap { margin-left: max(252px, calc((100vw - 1020px) / 2)); }
    .hud {
      position: fixed; left: 16px; top: 96px; z-index: 60; width: 200px; margin: 0;
      perspective: 1100px; perspective-origin: 85% 38%;
    }
    .hud-inner {
      display: block; position: relative;
      /* one flat tilted plane — no preserve-3d: 3D hit-testing of stacked children
         mis-targets the far-from-origin (right) half of the buttons */
      transform: rotateY(var(--ry, 0deg)) rotateX(var(--rx, 0deg));
      transform-origin: left center;
      transition: transform .5s cubic-bezier(.22,1,.36,1);
      animation: hud-in .6s cubic-bezier(.22,1,.36,1);
      background: linear-gradient(105deg, rgba(14,23,34,.94), rgba(6,11,17,.82));
      border: 1px solid var(--line2); border-radius: 4px; padding: 14px 12px 16px;
      box-shadow: 18px 24px 50px rgba(0,0,0,.55), 0 0 34px rgba(65,216,247,.07);
    }
    .hud:hover .hud-inner { transition-duration: .12s; }
    @keyframes hud-in { from { opacity: 0; transform: translateX(-60px) rotateY(42deg); } }
    /* periodic holo sweep across the panel */
    .hud-fx { display: block; position: absolute; inset: 0; border-radius: 4px; overflow: hidden; pointer-events: none; }
    .hud-fx::before {
      content: ""; position: absolute; inset: -20%;
      background: linear-gradient(115deg, transparent 42%, rgba(65,216,247,.10) 50%, transparent 58%);
      transform: translateX(-120%); animation: hud-sweep 5.5s ease-in-out infinite;
    }
    @keyframes hud-sweep { 0%, 55% { transform: translateX(-120%); } 80%, 100% { transform: translateX(120%); } }
    /* corner bracket + glowing receded edge */
    .hud-inner::before {
      content: ""; position: absolute; top: -1px; left: -1px; width: 12px; height: 12px;
      border: 1px solid var(--cyan); border-right: 0; border-bottom: 0; opacity: .8;
    }
    .hud-inner::after {
      content: ""; position: absolute; top: 10px; bottom: 10px; right: -2px; width: 2px;
      background: linear-gradient(180deg, transparent, var(--cyan), transparent); opacity: .45;
      filter: blur(.4px);
    }
    .hud-title {
      display: block; font: 700 10px var(--mono); letter-spacing: .34em; text-transform: uppercase;
      color: var(--cyan); text-shadow: 0 0 10px rgba(65,216,247,.55); margin: 0 0 4px 6px;
    }
    .hud-lab { width: auto; margin: 13px 0 5px 6px; letter-spacing: .3em; }
    .hud-sec { display: block; }
    .hud .cf {
      display: block; width: 100%; text-align: left; margin: 4px 0; padding: 10px 13px;
      background: rgba(4,7,12,.55); border-left-width: 3px; position: relative;
      transform: skewX(-8deg) translateX(0) scale(1);
      transition: transform .18s cubic-bezier(.22,1,.36,1), color .18s, border-color .18s, background .18s, box-shadow .18s;
      animation: cf-in .5s cubic-bezier(.22,1,.36,1) backwards;
      animation-delay: calc(.15s + var(--i, 0) * .05s);
    }
    @keyframes cf-in { from { opacity: 0; transform: skewX(-8deg) translateX(-36px) scale(1); } }
    .hud .cf > span { display: block; transform: skewX(8deg); }
    .hud .cf:hover {
      transform: skewX(-8deg) translateX(7px) scale(1.02);
      box-shadow: -6px 7px 14px rgba(0,0,0,.45);
    }
    .hud .cf.on {
      transform: skewX(-8deg) translateX(11px) scale(1.035);
      box-shadow: -9px 10px 20px rgba(0,0,0,.55);
    }
    /* breathing glow on the active option */
    .hud .cf.on::after {
      content: ""; position: absolute; inset: -1px; border-radius: 3px; pointer-events: none;
      box-shadow: 0 0 20px rgba(65,216,247,.3);
      animation: on-pulse 2.4s ease-in-out infinite;
    }
    @keyframes on-pulse { 0%, 100% { opacity: .35; } 50% { opacity: 1; } }
  }
  /* click glitch: quick chromatic split (all modes) */
  .cf.zap { animation: zap .28s steps(2, jump-none); }
  @keyframes zap {
    0%, 100% { filter: none; }
    25% { filter: brightness(1.7); text-shadow: 2px 0 rgba(255,95,102,.9), -2px 0 rgba(65,216,247,.9); }
    60% { text-shadow: -2px 0 rgba(255,95,102,.9), 2px 0 rgba(65,216,247,.9); }
  }
  /* short labels (Bug, Chore…) keep a full-size hit target */
  .hud .cf { min-width: 92px; }
  .cf.on { color: var(--cyan); border-color: var(--cyan-dim); background: rgba(65,216,247,.08); }
  /* per-category accents are generated at runtime from config.json */
  .cf.on[data-v="feature"] { color: #7ee787; border-color: rgba(126,231,135,.5); background: rgba(126,231,135,.08); }
  .cf.on[data-v="bug"] { color: #ffa657; border-color: rgba(255,166,87,.5); background: rgba(255,166,87,.08); }
  .cf.on[data-v="chore"] { color: var(--dim); border-color: var(--line2); background: rgba(147,167,186,.08); }
  .cf.on[data-v="high"] { color: var(--red); border-color: rgba(255,95,102,.5); background: rgba(255,95,102,.08); }
  .cf.on[data-v="normal"] { color: var(--dim); border-color: var(--line2); background: rgba(147,167,186,.08); }
  .cf.on[data-v="low"] { color: var(--muted); border-color: var(--line2); background: rgba(81,102,123,.12); }

  /* project mark (fat inset bar + glow bleed on the right edge) is generated
     at runtime per category from config.json */

  /* type badge */
  .typ { font: 700 9px var(--mono); letter-spacing: .14em; padding: 2px 7px;
         border-radius: 2px; border: 1px solid; white-space: nowrap; }
  .typ.feature { color: #7ee787; border-color: rgba(126,231,135,.4); }
  .typ.bug     { color: #ffa657; border-color: rgba(255,166,87,.45); }
  .typ.chore   { color: var(--dim); border-color: var(--line2); }
  .seg button.on.feature { background: rgba(126,231,135,.15); color: #7ee787; }
  .seg button.on.bug     { background: rgba(255,166,87,.15); color: #ffa657; }
  .seg button.on.chore   { background: rgba(147,167,186,.15); color: var(--dim); }

  /* priority badge */
  .prio { font: 700 9px var(--mono); letter-spacing: .14em; padding: 2px 7px;
          border-radius: 2px; border: 1px solid; white-space: nowrap; text-transform: uppercase; }
  .prio.high { color: var(--red); border-color: rgba(255,95,102,.45); background: rgba(255,95,102,.07); }
  .prio.low  { color: var(--muted); border-color: var(--line2); }
  .seg button.on.high   { background: rgba(255,95,102,.15); color: var(--red); }
  .seg button.on.normal { background: rgba(147,167,186,.15); color: var(--dim); }
  .seg button.on.low    { background: rgba(81,102,123,.2); color: var(--muted); }

  /* status lamp */
  .lamp { width: 10px; height: 10px; border-radius: 50%; justify-self: center; }
  .lamp.todo  { background: var(--amber); box-shadow: 0 0 8px rgba(255,180,84,.7); }
  .lamp.doing { background: var(--cyan);  box-shadow: 0 0 8px rgba(65,216,247,.8); animation: pulse 1.6s ease-in-out infinite; }
  .lamp.done  { background: var(--green); box-shadow: 0 0 8px rgba(65,240,165,.6); }
  @keyframes pulse { 50% { opacity: .35; box-shadow: 0 0 3px rgba(65,216,247,.3); } }

  .empty { color: var(--muted); font: 12px var(--mono); letter-spacing: .15em; padding: 18px 4px; text-transform: uppercase; }

  /* ---------- modal ---------- */
  .backdrop {
    position: fixed; inset: 0; z-index: 100; display: none;
    background: rgba(2,6,10,.78); backdrop-filter: blur(5px);
    padding: 5vh 16px; overflow-y: auto;
  }
  .backdrop.show { display: block; }
  .modal {
    max-width: 780px; margin: 0 auto; position: relative;
    background: linear-gradient(180deg, var(--panel2), var(--panel));
    border: 1px solid var(--line2); border-radius: 4px;
    box-shadow: 0 0 60px rgba(65,216,247,.08), 0 30px 80px rgba(0,0,0,.6);
    animation: rise .16s ease-out;
  }
  @keyframes rise { from { transform: translateY(10px); opacity: 0; } }
  .modal::before, .modal::after {
    content: ""; position: absolute; width: 16px; height: 16px; border: 2px solid var(--cyan); opacity: .9;
  }
  .modal::before { top: -2px; left: -2px; border-right: 0; border-bottom: 0; }
  .modal::after { bottom: -2px; right: -2px; border-left: 0; border-top: 0; }

  .m-head { padding: 18px 22px 0; }
  .m-id { font: 10px var(--mono); letter-spacing: .25em; color: var(--muted); margin-bottom: 6px; }
  .m-title { font-size: 21px; font-weight: 700; line-height: 1.3; margin: 0 0 14px; }
  .m-body { padding: 0 22px 8px; }
  .m-foot {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    padding: 14px 22px 18px; border-top: 1px solid var(--line); margin-top: 14px;
  }
  .m-foot .meta { font: 10px var(--mono); letter-spacing: .12em; color: var(--muted); flex: 1; min-width: 200px; }
  .m-close {
    position: absolute; top: 10px; right: 12px; border: none; background: none;
    font: 16px var(--mono); color: var(--muted); padding: 6px 8px;
  }
  .m-close:hover { color: var(--red); box-shadow: none; }

  /* segmented status control */
  .seg { display: inline-flex; border: 1px solid var(--line2); border-radius: 3px; overflow: hidden; margin-bottom: 4px; }
  .seg button { border: none; border-radius: 0; padding: 6px 14px; background: var(--inset); }
  .seg button + button { border-left: 1px solid var(--line); }
  .seg button:hover { box-shadow: none; }
  .seg button.on.todo  { background: rgba(255,180,84,.15); color: var(--amber); }
  .seg button.on.doing { background: rgba(65,216,247,.15); color: var(--cyan); }
  .seg button.on.done  { background: rgba(65,240,165,.15); color: var(--green); }

  .sect-label {
    font: 700 10px var(--mono); letter-spacing: .3em; text-transform: uppercase;
    color: var(--muted); margin: 18px 0 8px; display: flex; align-items: center; gap: 10px;
  }
  .sect-label::after { content: ""; flex: 1; height: 1px; background: var(--line); }
  .sect {
    background: var(--inset); border: 1px solid var(--line); border-radius: 3px;
    padding: 13px 15px; font-size: 14.5px;
  }
  .sect.empty-sect { color: var(--muted); font-style: italic; }

  /* markdown */
  .md { overflow-wrap: anywhere; }
  .md p { margin: 0 0 9px; } .md p:last-child { margin-bottom: 0; }
  .md ul, .md ol { margin: 0 0 9px; padding-left: 22px; }
  .md ul.cklist { list-style: none; padding-left: 4px; }
  .md h1, .md h2, .md h3 { font-size: 14px; margin: 12px 0 6px; color: var(--cyan); font-family: var(--mono); letter-spacing: .05em; }
  .md code {
    background: rgba(65,216,247,.07); border: 1px solid var(--line); border-radius: 3px;
    padding: 1px 5px; font: 13px var(--mono); color: #9adcff;
  }
  .md pre {
    background: var(--bg0); border: 1px solid var(--line); border-radius: 3px;
    padding: 10px 12px; overflow-x: auto; margin: 0 0 9px;
  }
  .md pre code { border: none; background: none; padding: 0; color: var(--dim); }
  .md a { color: var(--cyan); }
  .md strong { color: #fff; }

  /* real checkboxes */
  .ck-item { display: flex; align-items: flex-start; gap: 9px; padding: 3px 0; }
  .ck-num {
    flex: none; font: 11px/1.7 var(--mono); color: var(--muted); min-width: 22px;
    text-align: right; font-variant-numeric: tabular-nums; user-select: none;
  }
  .ck-item input[type=checkbox] {
    appearance: none; flex: none; width: 16px; height: 16px; margin-top: 3px; cursor: pointer;
    border: 1px solid var(--line2); border-radius: 2px; background: var(--bg0);
    display: grid; place-content: center; transition: all .12s;
  }
  .ck-item input[type=checkbox]:hover { border-color: var(--cyan); box-shadow: 0 0 6px rgba(65,216,247,.3); }
  .ck-item input[type=checkbox]:checked { background: rgba(65,240,165,.12); border-color: var(--green); }
  .ck-item input[type=checkbox]:checked::before {
    content: "✓"; font: 700 11px var(--mono); color: var(--green); text-shadow: 0 0 6px rgba(65,240,165,.8);
  }
  .ck-item input:checked + span { color: var(--muted); }
  .ck-item span { cursor: pointer; }

  /* editor */
  .ed label {
    display: block; font: 700 10px var(--mono); letter-spacing: .3em; text-transform: uppercase;
    color: var(--muted); margin: 16px 0 6px;
  }
  .ed label .hint { font-weight: 400; letter-spacing: .05em; text-transform: none; }
  .ed input[type=text], .ed textarea {
    width: 100%; font: 14.5px/1.5 var(--sans); color: var(--ink);
    background: var(--inset); border: 1px solid var(--line2); border-radius: 3px; padding: 9px 11px;
  }
  .ed textarea { resize: vertical; }
  .ed textarea.mono { font: 13px/1.5 var(--mono); }
  .ed input:focus, .ed textarea:focus { outline: none; border-color: var(--cyan-dim); box-shadow: 0 0 8px rgba(65,216,247,.15); }

  /* ---------- image attachments ---------- */
  .thumbs { display: flex; gap: 8px; overflow-x: auto; padding: 2px; }
  .thumb {
    position: relative; flex: none; width: 118px; height: 82px;
    border: 1px solid var(--line2); border-radius: 3px; overflow: hidden;
    cursor: zoom-in; background: var(--bg0);
  }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; transition: transform .12s; }
  .thumb:hover img { transform: scale(1.06); }
  .thumb .tdel {
    position: absolute; top: 3px; right: 3px; padding: 1px 6px; font-size: 11px;
    background: rgba(2,6,10,.75); border-color: transparent; color: var(--dim);
    opacity: 0; transition: opacity .12s;
  }
  .thumb:hover .tdel { opacity: 1; }
  .thumb .tdel:hover { color: var(--red); border-color: var(--red); box-shadow: none; }
  .addimg {
    flex: none; width: 118px; height: 82px; display: grid; place-content: center;
    border: 1px dashed var(--line2); border-radius: 3px; background: var(--inset);
    color: var(--muted); font: 600 9px/1.8 var(--mono); letter-spacing: .18em; text-transform: uppercase;
    cursor: pointer; text-align: center; transition: all .12s;
  }
  .addimg:hover { color: var(--cyan); border-color: var(--cyan-dim); }
  .modal.dragover { border-color: var(--cyan); box-shadow: 0 0 30px rgba(65,216,247,.3); }

  /* lightbox */
  .lightbox {
    position: fixed; inset: 0; z-index: 300; display: none;
    background: rgba(2,6,10,.94); align-items: center; justify-content: center;
  }
  .lightbox.show { display: flex; }
  .lightbox img {
    max-width: 92vw; max-height: 86vh; border: 1px solid var(--line2); border-radius: 3px;
    box-shadow: 0 30px 90px rgba(0,0,0,.7); cursor: default;
  }
  .lb-btn {
    position: fixed; top: 50%; transform: translateY(-50%); z-index: 301;
    font: 300 32px/1 var(--sans); text-transform: none; padding: 12px 17px;
    background: rgba(10,17,26,.65);
  }
  .lb-prev { left: 18px; } .lb-next { right: 18px; }
  .lb-x {
    position: fixed; top: 14px; right: 16px; z-index: 301;
    font-size: 15px; padding: 9px 13px; background: rgba(10,17,26,.65);
  }
  .lb-count {
    position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
    font: 11px var(--mono); letter-spacing: .3em; color: var(--dim);
  }

  @media (max-width: 640px) {
    .row { grid-template-columns: 20px 34px 1fr auto; }
    .row .chip, .row .when, .row .prio, .row .typ { display: none; }
    .clockbox { display: none; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header class="console">
    <div class="brand">
      <span class="name"><span class="hex">⬢</span> TASKCTRL</span>
      <span class="tag">__TAGLINE__</span>
    </div>
    <div class="readouts">
      <div class="ro open"><span class="l">QUEUED</span><span class="v" id="ro-todo">–</span></div>
      <div class="ro doing"><span class="l">ACTIVE</span><span class="v" id="ro-doing">–</span></div>
      <div class="ro done"><span class="l">COMPLETE</span><span class="v" id="ro-done">–</span></div>
    </div>
    <div class="clockbox"><span class="t" id="clock-t">--:--:--</span><span class="d" id="clock-d"></span></div>
    <button class="primary" id="new-btn">+ New Task</button>
  </header>

  <aside class="hud">
    <div class="hud-inner">
      <div class="hud-title">⌖ Filters</div>
      <div class="hud-sec" id="cat-sec"><span class="hud-lab">Project</span></div>
      <div class="hud-sec"><span class="hud-lab">Type</span>
        <button class="cf" data-f="typ" data-v="all"><span>All</span></button>
        <button class="cf" data-f="typ" data-v="feature"><span>Feature</span></button>
        <button class="cf" data-f="typ" data-v="bug"><span>Bug</span></button>
        <button class="cf" data-f="typ" data-v="chore"><span>Chore</span></button>
      </div>
      <div class="hud-sec"><span class="hud-lab">Priority</span>
        <button class="cf" data-f="prio" data-v="all"><span>All</span></button>
        <button class="cf" data-f="prio" data-v="high"><span>High</span></button>
        <button class="cf" data-f="prio" data-v="normal"><span>Normal</span></button>
        <button class="cf" data-f="prio" data-v="low"><span>Low</span></button>
      </div>
      <i class="hud-fx"></i>
    </div>
  </aside>

  <section class="panel" id="pins" style="display:none">
    <div class="panel-head">Pinned · Call Agenda<span class="rule"></span><span class="n" id="pins-n"></span></div>
    <div class="rows" id="pins-rows"></div>
  </section>

  <section class="panel" id="ops">
    <div class="panel-head">Operations<span class="rule"></span><span class="n" id="ops-n"></span></div>
    <div class="rows" id="ops-rows"></div>
  </section>

  <section class="panel" id="log">
    <div class="panel-head">Mission Log · Completed<span class="rule"></span><span class="n" id="log-n"></span></div>
    <div class="rows" id="log-rows"></div>
  </section>
</div>

<div class="backdrop" id="backdrop">
  <div class="modal" id="modal"></div>
</div>

<div class="lightbox" id="lightbox">
  <button class="lb-btn lb-prev" data-lb="-1">‹</button>
  <img id="lb-img" alt="">
  <button class="lb-btn lb-next" data-lb="1">›</button>
  <button class="lb-x" data-lb="x">✕</button>
  <div class="lb-count" id="lb-count"></div>
</div>
<input type="file" id="imgfile" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden>

<script>
let tasks = [];
let modalId = null;     // task id shown in modal, '' = new task, null = closed
let editMode = false;

const $ = s => document.querySelector(s);
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const STATUS_LABEL = { todo: 'Queued', doing: 'Active', done: 'Done' };
const CATS = __CATEGORIES__; // {slug: {label, color}} from config.json, injected by the server
const CAT_LABEL = Object.fromEntries(Object.entries(CATS).map(([k, v]) => [k, v.label || k]));
const TYPE_LABEL = { feature: 'FEAT', bug: 'BUG', chore: 'CHORE' };
const TYPE_FULL = { feature: 'Feature', bug: 'Bug', chore: 'Chore' };
const PRIO_LABEL = { high: '▲ High', normal: 'Normal', low: '▼ Low' };
const PRIO_RANK = { high: 0, normal: 1, low: 2 };
const prioOf = t => PRIO_RANK[t.priority] !== undefined ? t.priority : 'normal';
// exclusive filters, one per dimension, persisted individually
const FILTER_VALS = {
  cat:  ['all', ...Object.keys(CAT_LABEL)],
  typ:  ['all', ...Object.keys(TYPE_LABEL)],
  prio: ['all', ...Object.keys(PRIO_LABEL)],
};
const filters = {};
for (const k of Object.keys(FILTER_VALS)) {
  const v = localStorage.getItem('tc-f' + k);
  filters[k] = FILTER_VALS[k].includes(v) ? v : 'all';
}
localStorage.removeItem('tc-cats'); localStorage.removeItem('tc-cat'); // pre-exclusive keys
const DIM = { cat: t => t.category || '', typ: t => t.type || '', prio: t => prioOf(t) };
const matchDim = (t, k) => filters[k] === 'all' || DIM[k](t) === filters[k];

/* categories are config-driven: generate their accent CSS and the Project
   filter row at boot, then hand out cascade indexes to every HUD button */
function rgba(hex, a) {
  const n = parseInt(hex.replace('#', ''), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
{
  let css = '';
  for (const [k, v] of Object.entries(CATS)) {
    const c = /^#[0-9a-fA-F]{6}$/.test(v.color || '') ? v.color : '#41d8f7';
    css += `.cf.on[data-v="${k}"]{color:${c};border-color:${rgba(c, .5)};background:${rgba(c, .08)}}
.seg button.on.${k}{background:${rgba(c, .15)};color:${c}}
.row.cat-${k}{box-shadow:inset -4px 0 0 ${rgba(c, .65)}}
.row.cat-${k}::after{background:linear-gradient(270deg,${rgba(c, .13)},transparent)}`;
  }
  document.head.appendChild(Object.assign(document.createElement('style'), { textContent: css }));
  const sec = $('#cat-sec');
  if (Object.keys(CATS).length) {
    sec.insertAdjacentHTML('beforeend',
      `<button class="cf" data-f="cat" data-v="all"><span>All</span></button>` +
      Object.entries(CATS).map(([k, v]) =>
        `<button class="cf" data-f="cat" data-v="${k}"><span>${esc(v.label || k)}</span></button>`).join(''));
  } else {
    sec.style.display = 'none'; // no categories configured — no Project filter
  }
  document.querySelectorAll('.hud .cf').forEach((b, i) => b.style.setProperty('--i', i));
}

/* ---------- markdown (headings, bold, italic, code, lists, links, checkboxes) ---------- */
function md(src, interactive) {
  if (!src) return '';
  const blocks = [];
  src = src.replace(/```([\s\S]*?)```/g, (_, code) => {
    blocks.push('<pre><code>' + esc(code.replace(/^\n|\n$/g,'')) + '</code></pre>');
    return '\n@@BLOCK' + (blocks.length - 1) + '@@\n';
  });
  const inline = s => esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  let html = '', list = null, para = [], ckIdx = 0;
  const flushPara = () => { if (para.length) { html += '<p>' + para.join('<br>') + '</p>'; para = []; } };
  const flushList = () => { if (list) { html += '</' + (list === 'ck' ? 'ul' : list) + '>'; list = null; } };
  for (const raw of src.split('\n')) {
    const line = raw.replace(/\s+$/, '');
    const blk = line.match(/^@@BLOCK(\d+)@@$/);
    const h  = line.match(/^(#{1,3})\s+(.*)/);
    const ck = line.match(/^[-*]\s+\[( |x|X)\]\s*(.*)/);
    const ul = line.match(/^[-*]\s+(.*)/);
    const ol = line.match(/^\d+[.)]\s+(.*)/);
    if (blk) { flushPara(); flushList(); html += blocks[+blk[1]]; }
    else if (h) { flushPara(); flushList(); html += `<h${h[1].length}>` + inline(h[2]) + `</h${h[1].length}>`; }
    else if (ck) {
      flushPara();
      if (list !== 'ck') { flushList(); html += '<ul class="cklist">'; list = 'ck'; }
      const checked = ck[1].toLowerCase() === 'x';
      html += `<li class="ck-item"><span class="ck-num">${ckIdx + 1}.</span><input type="checkbox" data-ck="${ckIdx}" ${checked ? 'checked' : ''} ${interactive ? '' : 'disabled'}><span data-ckl="${ckIdx}">${inline(ck[2])}</span></li>`;
      ckIdx++;
    }
    else if (ul) { flushPara(); if (list !== 'ul') { flushList(); html += '<ul>'; list = 'ul'; } html += '<li>' + inline(ul[1]) + '</li>'; }
    else if (ol) { flushPara(); if (list !== 'ol') { flushList(); html += '<ol>'; list = 'ol'; } html += '<li>' + inline(ol[1]) + '</li>'; }
    else if (line === '') { flushPara(); flushList(); }
    else { flushList(); para.push(inline(line)); }
  }
  flushPara(); flushList();
  return html;
}

const ckStats = notes => {
  const all = (notes.match(/^[-*]\s+\[( |x|X)\]/gm) || []).length;
  const done = (notes.match(/^[-*]\s+\[(x|X)\]/gm) || []).length;
  return { all, done };
};

/* ---------- api ---------- */
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? {'Content-Type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error((await res.json()).error || res.statusText);
  return res.json();
}

async function refresh() {
  if (editMode) return; // never clobber an open editor
  tasks = (await api('GET', '/api/tasks')).tasks;
  render();
}

/* ---------- board ---------- */
function rowHtml(t) {
  const { all, done } = ckStats((t.subtasks || '') + '\n' + t.notes);
  const chip = all ? `<span class="chip ${done === all ? 'full' : ''}">CK ${done}/${all}</span>` : '<span></span>';
  const nImg = (t.images || []).length;
  const ichip = nImg ? `<span class="chip">IMG ${nImg}</span>` : '<span></span>';
  const p = prioOf(t);
  const prio = p !== 'normal' ? `<span class="prio ${p}">${PRIO_LABEL[p]}</span>` : '<span></span>';
  const typ = t.type && TYPE_LABEL[t.type]
    ? `<span class="typ ${t.type}">${TYPE_LABEL[t.type]}</span>` : '<span></span>';
  return `
  <div class="row ${t.status} ${t.category ? 'cat-' + t.category : ''}" data-id="${t.id}" title="${t.category || ''}">
    <span class="lamp ${t.status}"></span>
    <span class="tnum">T${String(t.num).padStart(2, '0')}</span>
    <span class="title">${esc(t.title)}</span>
    ${typ}
    ${prio}
    ${chip}
    ${ichip}
    <span class="when">${t.updated_at.slice(5, 16).replace('T', ' · ')}</span>
    <button class="pinbtn ${t.pinned ? 'on' : ''}" data-pin="${t.id}" title="${t.pinned ? 'Unpin' : 'Pin for call agenda'}">📌</button>
  </div>`;
}

function render() {
  // pinned = temporary call agenda; ignores category filters, tasks also stay in their normal section
  const pins = tasks.filter(t => t.pinned).sort((a, b) => a.num - b.num);
  $('#pins').style.display = pins.length ? '' : 'none';
  $('#pins-rows').innerHTML = pins.map(rowHtml).join('');
  $('#pins-n').textContent = String(pins.length).padStart(2, '0');

  const matched = tasks.filter(t => Object.keys(DIM).every(k => matchDim(t, k)));
  const pool = matched.filter(t => !t.pinned); // pinned rows live only in the agenda section
  const open = pool.filter(t => t.status !== 'done');
  const done = pool.filter(t => t.status === 'done');
  // priority first, then active before queued within each level
  open.sort((a, b) =>
    (PRIO_RANK[prioOf(a)] - PRIO_RANK[prioOf(b)]) ||
    ((a.status === 'doing' ? 0 : 1) - (b.status === 'doing' ? 0 : 1)) || (a.num - b.num));
  // newest completed first
  done.sort((a, b) => b.updated_at.localeCompare(a.updated_at));

  $('#ops-rows').innerHTML = open.map(rowHtml).join('') || '<div class="empty">// no open tasks — all systems nominal</div>';
  $('#log-rows').innerHTML = done.map(rowHtml).join('') || '<div class="empty">// nothing completed yet</div>';
  $('#ops-n').textContent = open.length ? String(open.length).padStart(2, '0') : '00';
  $('#log-n').textContent = done.length ? String(done.length).padStart(2, '0') : '00';
  $('#ro-todo').textContent  = String(matched.filter(t => t.status === 'todo').length).padStart(2, '0');
  $('#ro-doing').textContent = String(matched.filter(t => t.status === 'doing').length).padStart(2, '0');
  $('#ro-done').textContent  = String(matched.filter(t => t.status === 'done').length).padStart(2, '0');
  document.querySelectorAll('.cf').forEach(b =>
    b.classList.toggle('on', filters[b.dataset.f] === b.dataset.v));
  renderModal();
}

/* ---------- modal ---------- */
function sect(label, text, interactive, field) {
  return `<div class="sect-label">${label}</div>` +
    (text ? `<div class="sect md" ${field ? `data-ckfield="${field}"` : ''}>${md(text, interactive)}</div>`
          : `<div class="sect empty-sect">— nothing recorded —</div>`);
}

function segControl(current) {
  return `<div class="seg">` + Object.entries(STATUS_LABEL).map(([v, l]) =>
    `<button class="${v === current ? 'on ' + v : ''}" data-setstatus="${v}">${l}</button>`).join('') + `</div>`;
}

function segCat(current) {
  return `<div class="seg">` + Object.entries(CAT_LABEL).map(([v, l]) =>
    `<button class="${v === current ? 'on ' + v : ''}" data-setcat="${v}">${l}</button>`).join('') + `</div>`;
}

function segType(current) {
  return `<div class="seg">` + Object.entries(TYPE_FULL).map(([v, l]) =>
    `<button class="${v === current ? 'on ' + v : ''}" data-settype="${v}">${l}</button>`).join('') + `</div>`;
}

function segPrio(current) {
  return `<div class="seg">` + Object.entries(PRIO_LABEL).map(([v, l]) =>
    `<button class="${v === current ? 'on ' + v : ''}" data-setprio="${v}">${l}</button>`).join('') + `</div>`;
}

function renderModal() {
  const bd = $('#backdrop');
  if (modalId === null) { bd.classList.remove('show'); document.body.style.overflow = ''; return; }
  bd.classList.add('show');
  document.body.style.overflow = 'hidden';
  const m = $('#modal');
  const t = modalId ? tasks.find(x => x.id === modalId) : null;

  if (editMode || modalId === '') {
    const d = t || { title: '', reasoning: '', subtasks: '', notes: '', status: 'todo', category: '', priority: 'normal', type: '' };
    m.innerHTML = `
      <button class="m-close" data-act="close">✕</button>
      <div class="m-head">
        <div class="m-id">${t ? 'EDIT · TASK ' + String(t.num).padStart(2, '0') + ' · ' + t.id : 'NEW TASK'}</div>
      </div>
      <div class="m-body ed">
        <label>Task</label>
        <input type="text" id="e-title" value="${esc(d.title)}" placeholder="What needs to be done">
        <label>Reasoning <span class="hint">— why this task exists</span></label>
        <textarea id="e-reasoning" rows="3">${esc(d.reasoning)}</textarea>
        <label>Subtasks <span class="hint">— one "- [ ]" line per subtask</span></label>
        <textarea id="e-subtasks" class="mono" rows="5">${esc(d.subtasks || '')}</textarea>
        <label>Output / Notes <span class="hint">— markdown</span></label>
        <textarea id="e-notes" class="mono" rows="9">${esc(d.notes)}</textarea>
        <label>Status</label>
        ${segControl(d.status)}
        <input type="hidden" id="e-status" value="${d.status}">
        ${Object.keys(CAT_LABEL).length ? `<label>Category <span class="hint">— click again to clear</span></label>${segCat(d.category || '')}` : ''}
        <input type="hidden" id="e-cat" value="${d.category || ''}">
        <label>Type <span class="hint">— click again to clear</span></label>
        ${segType(d.type || '')}
        <input type="hidden" id="e-typ" value="${d.type || ''}">
        <label>Priority</label>
        ${segPrio(prioOf(d))}
        <input type="hidden" id="e-prio" value="${prioOf(d)}">
      </div>
      <div class="m-foot">
        <span class="meta"></span>
        <button data-act="cancel">Cancel</button>
        <button class="primary" data-act="save">Save</button>
      </div>`;
    $('#e-title').focus();
    return;
  }

  if (!t) { closeModal(); return; }
  m.innerHTML = `
    <button class="m-close" data-act="close">✕</button>
    <div class="m-head">
      <div class="m-id">TASK ${String(t.num).padStart(2, '0')} · ${t.id}${t.pinned ? '<span class="m-pin">📌 PINNED</span>' : ''}</div>
      <h2 class="m-title">${esc(t.title)}</h2>
      <div style="display:flex;gap:10px;flex-wrap:wrap">${segControl(t.status)}${segType(t.type || '')}${Object.keys(CAT_LABEL).length ? segCat(t.category || '') : ''}${segPrio(prioOf(t))}</div>
    </div>
    <div class="m-body">
      ${sect('Reasoning', t.reasoning, false)}
      ${sect('Subtasks', t.subtasks, true, 'subtasks')}
      ${sect('Output / Notes', t.notes, true, 'notes')}
      <div class="sect-label">Images</div>
      <div class="thumbs">
        ${(t.images || []).map((n, i) => `
        <div class="thumb" data-lbopen="${i}">
          <img src="/images/${n}" alt="" loading="lazy">
          <button class="tdel" data-imgdel="${n}" title="Delete image">✕</button>
        </div>`).join('')}
        <div class="addimg" id="addimg">+ image<br>drop · paste<br>or click</div>
      </div>
    </div>
    <div class="m-foot">
      <span class="meta">CREATED ${t.created_at.replace('T',' ')} · UPDATED ${t.updated_at.replace('T',' ')}</span>
      <button class="danger" data-act="delete">Delete</button>
      <button data-act="pin">${t.pinned ? 'Unpin' : '📌 Pin'}</button>
      <button data-act="edit">Edit</button>
    </div>`;
}

function closeModal() { closeLb(); modalId = null; editMode = false; renderModal(); }

async function saveEditor() {
  const body = {
    title: $('#e-title').value,
    reasoning: $('#e-reasoning').value,
    subtasks: $('#e-subtasks').value,
    notes: $('#e-notes').value,
    status: $('#e-status').value,
    category: $('#e-cat').value,
    priority: $('#e-prio').value,
    type: $('#e-typ').value,
  };
  if (!body.title.trim()) { $('#e-title').focus(); return; }
  try {
    if (modalId) {
      await api('PUT', '/api/tasks/' + modalId, body);
    } else {
      const created = await api('POST', '/api/tasks', body);
      modalId = created.id;
    }
    editMode = false;
    await refresh();
  } catch (e) { alert(e.message); }
}

/* ---------- images ---------- */
let lbIdx = -1; // index into the open task's images; -1 = lightbox closed

function curImages() {
  const t = tasks.find(x => x.id === modalId);
  return t ? (t.images || []) : [];
}

function openLb(i) {
  const list = curImages();
  if (!list.length) return;
  lbIdx = (i + list.length) % list.length;
  $('#lb-img').src = '/images/' + list[lbIdx];
  $('#lb-count').textContent = (lbIdx + 1) + ' / ' + list.length;
  const many = list.length > 1;
  $('.lb-prev').style.display = many ? '' : 'none';
  $('.lb-next').style.display = many ? '' : 'none';
  $('#lightbox').classList.add('show');
}

function closeLb() { lbIdx = -1; $('#lightbox').classList.remove('show'); }

async function uploadImages(files) {
  if (!modalId || editMode) return;
  for (const f of files) {
    if (!f.type.startsWith('image/')) continue;
    if (f.size > 10 * 1024 * 1024) { alert(f.name + ' is over the 10 MB cap'); continue; }
    const res = await fetch('/api/tasks/' + modalId + '/images', {
      method: 'POST', headers: { 'Content-Type': f.type }, body: f,
    });
    if (!res.ok) { alert((await res.json()).error || res.statusText); break; }
  }
  await refresh();
}

async function toggleCheckbox(taskId, field, idx, checked) {
  const t = tasks.find(x => x.id === taskId);
  if (!t) return;
  let i = -1;
  const text = (t[field] || '').replace(/(^|\n)([-*]\s+\[)( |x|X)(\])/g, (m, pre, open, state, close) => {
    i++;
    return i === idx ? pre + open + (checked ? 'x' : ' ') + close : m;
  });
  await api('PUT', '/api/tasks/' + taskId, { [field]: text });
  await refresh();
}

/* ---------- events ---------- */
document.addEventListener('click', async ev => {
  const lb = ev.target.closest('[data-lb]');
  if (lb) { lb.dataset.lb === 'x' ? closeLb() : openLb(lbIdx + +lb.dataset.lb); return; }
  if (ev.target === $('#lightbox')) { closeLb(); return; }

  const idel = ev.target.closest('[data-imgdel]');
  if (idel) {
    if (confirm('Delete this image?')) {
      await api('DELETE', '/api/tasks/' + modalId + '/images/' + idel.dataset.imgdel);
      await refresh();
    }
    return;
  }
  const th = ev.target.closest('[data-lbopen]');
  if (th) { openLb(+th.dataset.lbopen); return; }
  if (ev.target.closest('#addimg')) { $('#imgfile').click(); return; }

  const pb = ev.target.closest('.pinbtn');
  if (pb) {
    const t = tasks.find(x => x.id === pb.dataset.pin);
    await api('PUT', '/api/tasks/' + pb.dataset.pin, { pinned: !(t && t.pinned) });
    await refresh();
    return;
  }

  const row = ev.target.closest('.row');
  if (row) { modalId = row.dataset.id; editMode = false; renderModal(); return; }

  const act = ev.target.closest('[data-act]');
  if (act) {
    const a = act.dataset.act;
    if (a === 'close') closeModal();
    else if (a === 'cancel') { if (modalId === '') closeModal(); else { editMode = false; renderModal(); } }
    else if (a === 'save') await saveEditor();
    else if (a === 'edit') { editMode = true; renderModal(); }
    else if (a === 'pin') {
      const t = tasks.find(x => x.id === modalId);
      await api('PUT', '/api/tasks/' + modalId, { pinned: !(t && t.pinned) });
      await refresh();
    }
    else if (a === 'delete') {
      if (confirm('Delete this task permanently?')) {
        await api('DELETE', '/api/tasks/' + modalId);
        closeModal();
        await refresh();
      }
    }
    return;
  }

  const cf = ev.target.closest('.cf');
  if (cf) {
    const { f, v } = cf.dataset;
    filters[f] = (filters[f] === v) ? 'all' : v; // re-click the active filter → back to All
    localStorage.setItem('tc-f' + f, filters[f]);
    cf.classList.remove('zap'); void cf.offsetWidth; cf.classList.add('zap');
    render();
    return;
  }

  const segc = ev.target.closest('[data-setcat]');
  if (segc) {
    const editing = editMode || modalId === '';
    const current = editing ? $('#e-cat').value : (tasks.find(x => x.id === modalId)?.category || '');
    const v = segc.dataset.setcat === current ? '' : segc.dataset.setcat;
    if (editing) {
      $('#e-cat').value = v;
      segc.parentElement.querySelectorAll('button').forEach(b =>
        b.className = b.dataset.setcat === v ? 'on ' + v : '');
    } else if (modalId) {
      await api('PUT', '/api/tasks/' + modalId, { category: v });
      await refresh();
    }
    return;
  }

  const segt = ev.target.closest('[data-settype]');
  if (segt) {
    const editing = editMode || modalId === '';
    const current = editing ? $('#e-typ').value : (tasks.find(x => x.id === modalId)?.type || '');
    const v = segt.dataset.settype === current ? '' : segt.dataset.settype;
    if (editing) {
      $('#e-typ').value = v;
      segt.parentElement.querySelectorAll('button').forEach(b =>
        b.className = b.dataset.settype === v ? 'on ' + v : '');
    } else if (modalId) {
      await api('PUT', '/api/tasks/' + modalId, { type: v });
      await refresh();
    }
    return;
  }

  const segp = ev.target.closest('[data-setprio]');
  if (segp) {
    const v = segp.dataset.setprio;
    if (editMode || modalId === '') {
      $('#e-prio').value = v;
      segp.parentElement.querySelectorAll('button').forEach(b =>
        b.className = b.dataset.setprio === v ? 'on ' + v : '');
    } else if (modalId) {
      await api('PUT', '/api/tasks/' + modalId, { priority: v });
      await refresh();
    }
    return;
  }

  const seg = ev.target.closest('[data-setstatus]');
  if (seg) {
    const v = seg.dataset.setstatus;
    if (editMode || modalId === '') {
      $('#e-status').value = v;
      seg.parentElement.querySelectorAll('button').forEach(b =>
        b.className = b.dataset.setstatus === v ? 'on ' + v : '');
    } else if (modalId) {
      await api('PUT', '/api/tasks/' + modalId, { status: v });
      await refresh();
    }
    return;
  }

  if (ev.target === $('#backdrop')) closeModal();
});

document.addEventListener('change', async ev => {
  const cb = ev.target.closest('input[data-ck]');
  if (cb && modalId && !editMode) {
    const field = cb.closest('[data-ckfield]')?.dataset.ckfield || 'notes';
    await toggleCheckbox(modalId, field, +cb.dataset.ck, cb.checked);
  }
});

document.addEventListener('keydown', ev => {
  if ($('#lightbox').classList.contains('show')) {
    if (ev.key === 'Escape') closeLb();
    else if (ev.key === 'ArrowLeft') openLb(lbIdx - 1);
    else if (ev.key === 'ArrowRight') openLb(lbIdx + 1);
    return;
  }
  if (ev.key === 'Escape' && modalId !== null && !editMode) closeModal();
});

/* image intake: paste, drag-drop, file picker (view mode of an existing task only) */
document.addEventListener('paste', ev => {
  if (!modalId || editMode) return;
  const files = [...(ev.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'));
  if (files.length) { ev.preventDefault(); uploadImages(files); }
});

const modalEl = $('#modal');
modalEl.addEventListener('dragover', ev => {
  if (!modalId || editMode) return;
  ev.preventDefault();
  modalEl.classList.add('dragover');
});
modalEl.addEventListener('dragleave', () => modalEl.classList.remove('dragover'));
modalEl.addEventListener('drop', ev => {
  if (!modalId || editMode) return;
  ev.preventDefault();
  modalEl.classList.remove('dragover');
  uploadImages([...ev.dataTransfer.files]);
});

$('#imgfile').onchange = async ev => { await uploadImages([...ev.target.files]); ev.target.value = ''; };

$('#new-btn').onclick = () => { modalId = ''; editMode = false; renderModal(); };

/* HUD mouse-tracking tilt (wide mode; harmless no-op when panel is flat) */
const hudEl = $('.hud'), hudInner = $('.hud-inner');
hudEl.addEventListener('mousemove', ev => {
  const r = hudInner.getBoundingClientRect();
  const x = (ev.clientX - r.left) / r.width, y = (ev.clientY - r.top) / r.height;
  hudInner.style.setProperty('--ry', (6 - x * 12).toFixed(1) + 'deg');
  hudInner.style.setProperty('--rx', ((0.5 - y) * 5).toFixed(1) + 'deg');
});
hudEl.addEventListener('mouseleave', () => {
  hudInner.style.removeProperty('--ry');
  hudInner.style.removeProperty('--rx');
});

/* clock */
function tick() {
  const n = new Date();
  const p = x => String(x).padStart(2, '0');
  $('#clock-t').textContent = p(n.getHours()) + ':' + p(n.getMinutes()) + ':' + p(n.getSeconds());
  $('#clock-d').textContent = n.toISOString().slice(0, 10);
}
setInterval(tick, 1000); tick();

refresh();
setInterval(refresh, 15000); // pick up direct edits to tasks.json
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        save_db({"tasks": []})
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Task board on http://{HOST}:{PORT} (db: {DB_PATH})")
    server.serve_forever()
