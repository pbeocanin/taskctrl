#!/usr/bin/env python3
"""Local task board — stdlib only.

Tasks live in tasks.json next to this file. The file is reloaded on every
request, so edits made directly to it (by hand or by Claude) show up on the
next refresh without restarting the server.

Run:  python3 server.py   (binds 0.0.0.0:8100 — reachable from your LAN)
      TASKCTRL_PORT=9000 python3 server.py   to use another port
      TASKCTRL_HOST=127.0.0.1 ...            to keep it off the LAN
"""

import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

HOST = os.environ.get("TASKCTRL_HOST", "0.0.0.0")
PORT = int(os.environ.get("TASKCTRL_PORT", "8100"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tasks.json")

_lock = threading.Lock()

# In-memory feed of recent mutations, polled by the page for toast
# notifications. Lost on restart by design — toasts only matter live.
_events = deque(maxlen=200)
_event_seq = 0


def add_event(actor, action, task, detail=""):
    """Record a mutation. Callers already hold _lock."""
    global _event_seq
    _event_seq += 1
    _events.append({
        "seq": _event_seq,
        "ts": now_iso(),
        "actor": actor,  # "board" = this page's own UI, anything else = an agent
        "action": action,  # created | updated | deleted
        "task_id": task["id"],
        "num": task.get("num", 0),
        "title": task.get("title", ""),
        "detail": detail,
    })

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


# ---- bench: a plain drop folder (~/bench) reachable from the board -----------
BENCH_DIR = os.environ.get("TASKCTRL_BENCH") or os.path.expanduser("~/bench")
MAX_BENCH_BYTES = 500 * 1024 * 1024
BENCH_HIDE = {"node_modules", "lighthouse", ".git", "__pycache__"}


def bench_safe_name(raw):
    """Strip any path and control chars; refuse dotfiles and empty names."""
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f/]", "", name)
    if not name or name.startswith("."):
        return None
    return name[:200]


def bench_safe_rel(raw):
    """Normalise a folder path relative to ~/bench ("" = the root, "a/b" = a subfolder).

    Returns None for anything that could escape or reach a hidden dir: absolute
    paths, "..", dotfile segments, hidden names, control chars, or a folder that
    doesn't exist. Folders are never created from here — only ones the user (or an
    agent) already made on the VM can be written into."""
    raw = (raw or "").replace("\\", "/").strip().strip("/")
    if not raw:
        return ""
    parts = []
    for seg in raw.split("/"):
        seg = re.sub(r"[\x00-\x1f]", "", seg).strip()
        if not seg or seg == ".":
            continue
        if seg == ".." or seg.startswith(".") or seg in BENCH_HIDE:
            return None
        parts.append(seg)
    rel = "/".join(parts)
    full = os.path.realpath(os.path.join(BENCH_DIR, rel))
    root = os.path.realpath(BENCH_DIR)
    if not (full == root or full.startswith(root + os.sep)) or not os.path.isdir(full):
        return None
    return rel


def bench_unique(name, rel=""):
    """foo.csv → foo-2.csv, foo-3.csv … never overwrite an existing bench file.
    Clashes are checked per folder, so tools/foo.csv and foo.csv don't collide."""
    base, dot, ext = name.rpartition(".")
    if not dot or not base:
        base, ext = name, ""
    cand, n = name, 1
    while os.path.exists(os.path.join(BENCH_DIR, rel, cand)):
        n += 1
        cand = f"{base}-{n}.{ext}" if ext else f"{base}-{n}"
    return cand


def bench_listing(rel=""):
    out = []
    try:
        entries = os.scandir(os.path.join(BENCH_DIR, rel))
    except OSError:
        return out
    with entries:
        for e in entries:
            if e.name in BENCH_HIDE or e.name.startswith("."):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            out.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size,
                        "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    out.sort(key=lambda x: not x["dir"])  # stable: folders first, newest on top within each
    return out


def load_db():
    if not os.path.exists(DB_PATH):
        return {"tasks": []}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        db = json.load(f)
    changed = False
    # one-time migration: number tasks that predate the num field, by creation date
    unnumbered = [t for t in db["tasks"] if "num" not in t]
    if unnumbered:
        nxt = max((t.get("num", 0) for t in db["tasks"]), default=0)
        for t in sorted(unnumbered, key=lambda t: t.get("created_at", "")):
            nxt += 1
            t["num"] = nxt
        changed = True
    for t in db["tasks"]:
        # one-time migrations: markdown-string subtasks → structured objects, and
        # completed_at backfill for tasks finished before the field existed.
        # updated_at is the closest known moment for both — approximate, not exact.
        if isinstance(t.get("subtasks"), str):
            t["subtasks"] = [dict(
                {"id": new_st_id(), "text": p["text"], "reasoning": "",
                 "done": p["done"], "created_at": t.get("created_at") or now_iso()},
                **({"completed_at": t.get("updated_at") or now_iso()} if p["done"] else {}),
            ) for p in parse_subtasks_md(t["subtasks"])]
            changed = True
        if t.get("status") == "done" and "completed_at" not in t:
            t["completed_at"] = t.get("updated_at") or now_iso()
            changed = True
    if changed:
        save_db(db)
    return db


def save_db(db):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DB_PATH)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_st_id():
    return uuid.uuid4().hex[:6]


def parse_subtasks_md(text):
    """Markdown checklist / plain lines → [{text, done}] (agent-compat input)."""
    items = []
    for line in text.splitlines():
        line = line.strip()
        m = re.fullmatch(r"[-*]\s+\[( |x|X)\]\s*(.*)", line)
        if m:
            txt, done = m.group(2).strip(), m.group(1).lower() == "x"
        else:
            txt, done = line.lstrip("-* ").strip(), False
        if txt:
            items.append({"text": txt, "done": done})
    return items


def normalize_subtasks(incoming, existing, now):
    """Normalize a POST/PUT subtasks payload into the stored structure.

    Accepts a list of dicts (the UI sends full objects) or a markdown/plain-line
    string (agent compatibility). Surviving items keep their id, created_at,
    reasoning, and completed_at; the server owns all timestamps — a subtask that
    flips to done is stamped now, one that flips back loses its stamp."""
    if isinstance(incoming, str):
        incoming = parse_subtasks_md(incoming)
    elif not isinstance(incoming, list):
        return existing
    pool = [e for e in existing if isinstance(e, dict)]

    def claim(p):
        for e in pool:
            if p.get("id") and e.get("id") == p.get("id"):
                pool.remove(e)
                return e
        for e in pool:
            if not p.get("id") and e.get("text") == p.get("text"):
                pool.remove(e)
                return e
        return None

    out = []
    for p in incoming:
        if not isinstance(p, dict):
            continue
        text = str(p.get("text") or "").strip()
        if not text:
            continue
        old = claim(p) or {}
        done = bool(p.get("done", old.get("done", False)))
        item = {
            "id": old.get("id") or new_st_id(),
            "text": text,
            "reasoning": str(p.get("reasoning", old.get("reasoning", "")) or "").strip(),
            "done": done,
            "created_at": old.get("created_at") or now,
        }
        if done:
            item["completed_at"] = (old.get("completed_at") if old.get("done") else None) or now
        out.append(item)
    return out


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

    def _actor(self):
        # the board's own fetches send X-Board-Client; anything else is an agent
        return "board" if self.headers.get("X-Board-Client") else "agent"

    def log_message(self, fmt, *args):
        pass  # keep stdout quiet

    # ---- routes --------------------------------------------------------

    def do_GET(self):
        if self.path in ("/", "/bench") or self.path.startswith(("/?", "/bench?")):
            # same page for the board and the bench; the JS picks the mode from the path
            tagline = str(load_config().get("tagline") or "mission board")
            page = (PAGE
                    .replace("__CATEGORIES__", json.dumps(categories()))
                    .replace("__TAGLINE__", tagline.replace("&", "&amp;").replace("<", "&lt;")))
            self._send(200, page, "text/html; charset=utf-8")
        elif self.path == "/api/tasks":
            with _lock:
                self._json(200, load_db())
        elif self.path == "/api/events" or self.path.startswith("/api/events?"):
            m = re.search(r"[?&]since=(\d+)", self.path)
            since = int(m.group(1)) if m else None
            with _lock:
                evs = [] if since is None else [e for e in _events if e["seq"] > since]
                self._json(200, {"seq": _event_seq, "events": evs})
        elif self.path == "/api/bench" or self.path.startswith("/api/bench?"):
            m = re.search(r"[?&]path=([^&]*)", self.path)
            rel = bench_safe_rel(unquote(m.group(1)) if m else "")
            if rel is None:
                return self._json(404, {"error": "no such bench folder"})
            self._json(200, {"dir": BENCH_DIR, "path": rel, "files": bench_listing(rel)})
        elif self.path.startswith("/bench/"):
            return self._bench_download(self.path[len("/bench/"):])
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
        if self.path == "/api/tasks/mark-reviewed":
            # bulk "all filmed": flags every done-but-unreviewed task, archived included
            now = now_iso()
            with _lock:
                db = load_db()
                hit = [t for t in db["tasks"] if t.get("status") == "done" and not t.get("reviewed")]
                for t in hit:
                    t["reviewed"] = True
                    t["reviewed_at"] = now
                if hit:
                    save_db(db)
            return self._json(200, {"marked": len(hit)})
        if self.path == "/api/bench":
            return self._bench_upload()
        m = re.fullmatch(r"/api/tasks/([\w-]+)/images", self.path)
        if m:
            return self._upload_image(m.group(1))
        if self.path != "/api/tasks":
            return self._json(404, {"error": "not found"})
        body = self._read_body()
        title = (body.get("title") or "").strip()
        if not title:
            return self._json(400, {"error": "title is required"})
        now = now_iso()
        task = {
            "id": uuid.uuid4().hex[:12],
            "num": 0,  # assigned below under the lock
            "title": title,
            "reasoning": (body.get("reasoning") or "").strip(),
            "subtasks": normalize_subtasks(body.get("subtasks") or "", [], now),
            "notes": (body.get("notes") or "").strip(),
            "status": body.get("status") if body.get("status") in STATUSES else "todo",
            "category": body.get("category") if body.get("category") in categories() else "",
            "priority": body.get("priority") if body.get("priority") in PRIORITIES else "normal",
            "type": body.get("type") if body.get("type") in TYPES else "",
            "pinned": bool(body.get("pinned")),
            "created_at": now,
            "updated_at": now,
        }
        if body.get("prod_shape"):
            task["prod_shape"] = True
        if task["status"] == "done":
            task["completed_at"] = now
        with _lock:
            db = load_db()
            task["num"] = max((t.get("num", 0) for t in db["tasks"]), default=0) + 1
            db["tasks"].append(task)
            save_db(db)
            add_event(self._actor(), "created", task)
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
            add_event(self._actor(), "updated", task, "image added")
        self._json(201, task)

    def _bench_upload(self):
        # raw bytes in the body, original filename in X-Filename (URL-encoded)
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return self._json(400, {"error": "empty body — send raw file bytes"})
        if length > MAX_BENCH_BYTES:
            return self._json(413, {"error": "file too large (500 MB cap)"})
        name = bench_safe_name(unquote(self.headers.get("X-Filename") or ""))
        if not name:
            return self._json(400, {"error": "X-Filename header required (no dotfiles)"})
        os.makedirs(BENCH_DIR, exist_ok=True)
        # X-Bench-Dir (URL-encoded, optional) picks an EXISTING subfolder of ~/bench
        rel = bench_safe_rel(unquote(self.headers.get("X-Bench-Dir") or ""))
        if rel is None:
            return self._json(404, {"error": "no such bench folder"})
        with _lock:  # the unique-name check and the create must not interleave
            name = bench_unique(name, rel)
            fd = os.open(os.path.join(BENCH_DIR, rel, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        remaining, tmp_err = length, None
        with os.fdopen(fd, "wb") as f:
            while remaining:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    tmp_err = "connection closed mid-upload"
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if tmp_err:
            os.unlink(os.path.join(BENCH_DIR, rel, name))
            return self._json(400, {"error": tmp_err})
        self._json(201, {"name": name, "path": rel, "size": length, "dir": BENCH_DIR})

    def _bench_download(self, raw):
        # /bench/<file> or /bench/<sub>/<folder>/<file>, optionally ?inline=1 for
        # the bench page's preview pane
        raw, _, query = raw.partition("?")
        inline = "inline=1" in query.split("&")
        raw = unquote(raw)
        folder, _, leaf = raw.rpartition("/")
        rel, name = bench_safe_rel(folder), bench_safe_name(leaf)
        path = os.path.join(BENCH_DIR, rel, name) if rel is not None and name else None
        if not path or name in BENCH_HIDE or not os.path.isfile(path):
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if inline:
            # images keep their type; everything else is shown as plain text so an
            # .html or .svg dropped in bench can never run scripts on the board's origin
            if not ctype.startswith("image/") or ctype == "image/svg+xml":
                ctype = "text/plain; charset=utf-8"
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition",
                         ("inline" if inline else "attachment") + f"; filename*=UTF-8''{quote(name)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

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
            now = now_iso()
            changed = []  # human-readable fragments for the event feed
            for field in ("title", "reasoning", "notes"):
                if field in body:
                    val = str(body[field]).strip()
                    if val != task.get(field, ""):
                        changed.append(field)
                    task[field] = val
            if "subtasks" in body:
                old_st = task.get("subtasks") or []
                task["subtasks"] = normalize_subtasks(body["subtasks"], old_st, now)
                if task["subtasks"] != old_st:
                    n_new = sum(1 for s in task["subtasks"] if s.get("done")) - sum(1 for s in old_st if s.get("done"))
                    changed.append(f"{n_new} subtask{'s' if n_new > 1 else ''} ✓" if n_new > 0 else "subtasks")
            if body.get("status") in STATUSES:
                if body["status"] != task["status"]:
                    changed.append("→ " + body["status"])
                # completed_at marks the todo/doing → done transition; reopening clears it
                if body["status"] == "done" and task["status"] != "done":
                    task["completed_at"] = now
                elif body["status"] != "done" and task["status"] == "done":
                    task.pop("completed_at", None)
                if body["status"] != task["status"]:
                    # any status change resets the video flag: a fresh completion is
                    # pending film again, and open tasks don't carry the flag at all
                    task.pop("reviewed", None)
                    task.pop("reviewed_at", None)
                task["status"] = body["status"]
            if "category" in body:
                val = body["category"] if body["category"] in categories() else ""
                if val != task.get("category", ""):
                    changed.append("category")
                task["category"] = val
            if "priority" in body:
                val = body["priority"] if body["priority"] in PRIORITIES else "normal"
                if val != task.get("priority", "normal"):
                    changed.append("priority")
                task["priority"] = val
            if "type" in body:
                val = body["type"] if body["type"] in TYPES else ""
                if val != task.get("type", ""):
                    changed.append("type")
                task["type"] = val
            if "pinned" in body:
                if bool(body["pinned"]) != bool(task.get("pinned")):
                    changed.append("pinned" if body["pinned"] else "unpinned")
                task["pinned"] = bool(body["pinned"])
            if "prod_shape" in body:
                # red warning: this task's work changes the shape of prod data
                # (migration or row reinterpretation). Cleared once live on prod.
                val = bool(body["prod_shape"])
                if val != bool(task.get("prod_shape")):
                    changed.append("PROD SHAPE" if val else "prod-shape cleared")
                if val:
                    task["prod_shape"] = True
                else:
                    task.pop("prod_shape", None)
            if "archived" in body:
                arch = bool(body["archived"])
                if arch != bool(task.get("archived")):
                    changed.append("archived" if arch else "unarchived")
                if arch and not task.get("archived"):
                    task["archived_at"] = now
                elif not arch:
                    task.pop("archived_at", None)
                task["archived"] = arch
            if "reviewed" in body:
                # filmed-in-update-video flag; meta like a pin — no updated_at bump, no event
                if bool(body["reviewed"]):
                    if not task.get("reviewed"):
                        task["reviewed"] = True
                        task["reviewed_at"] = now
                else:
                    task.pop("reviewed", None)
                    task.pop("reviewed_at", None)
            if not task["title"]:
                return self._json(400, {"error": "title is required"})
            if not set(body) <= {"pinned", "archived", "reviewed"}:  # metadata toggles, not work
                task["updated_at"] = now
            save_db(db)
            if changed:  # no-op PUTs don't make noise
                add_event(self._actor(), "updated", task, ", ".join(changed))
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
                add_event(self._actor(), "updated", task, "image removed")
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
            add_event(self._actor(), "deleted", gone)
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
  button, a.btnlink {
    font: 600 12px var(--mono); letter-spacing: .12em; text-transform: uppercase;
    color: var(--dim); background: var(--panel2); border: 1px solid var(--line2);
    border-radius: 3px; padding: 8px 14px; cursor: pointer; transition: all .12s;
  }
  a.btnlink { display: inline-flex; align-items: center; text-decoration: none; }
  button:hover, a.btnlink:hover { color: var(--cyan); border-color: var(--cyan-dim); box-shadow: 0 0 10px rgba(65,216,247,.15); }
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
    display: grid; grid-template-columns: 26px 38px 1fr auto auto auto auto auto auto auto; gap: 12px; align-items: center;
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
  .chip.rec { color: var(--red); border-color: rgba(255,95,102,.45); }
  .chip.rec::first-letter { animation: rec-blink 1.6s steps(1) infinite; }
  @keyframes rec-blink { 50% { color: transparent; } }
  #review-all {
    font: 700 10px var(--mono); letter-spacing: .15em; text-transform: uppercase;
    color: var(--green); background: rgba(65,240,165,.08); border: 1px solid rgba(65,240,165,.4);
    border-radius: 2px; padding: 3px 10px; cursor: pointer;
  }
  #review-all:hover { background: rgba(65,240,165,.16); }

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

  /* prod data-shape warning */
  .shape {
    font: 700 9px var(--mono); letter-spacing: .14em; padding: 2px 7px; margin-right: 8px;
    border-radius: 2px; border: 1px solid var(--red); color: var(--red);
    background: rgba(255,95,102,.1); white-space: nowrap;
    text-shadow: 0 0 6px rgba(255,95,102,.55); animation: shape-pulse 1.6s ease-in-out infinite;
  }
  @keyframes shape-pulse { 50% { box-shadow: 0 0 12px rgba(255,95,102,.55); } }
  .row.pshape { border-color: rgba(255,95,102,.45); }
  .row.pshape:hover { border-color: var(--red); }

  /* priority badge */
  .prio { font: 700 9px var(--mono); letter-spacing: .14em; padding: 2px 7px;
          border-radius: 2px; border: 1px solid; white-space: nowrap; text-transform: uppercase; }
  .prio.high { color: var(--red); border-color: rgba(255,95,102,.45); background: rgba(255,95,102,.07); }
  .prio.low  { color: var(--muted); border-color: var(--line2); }
  .seg button.on.high   { background: rgba(255,95,102,.15); color: var(--red); }
  .seg button.on.normal { background: rgba(147,167,186,.15); color: var(--dim); }
  .seg button.on.low    { background: rgba(81,102,123,.2); color: var(--muted); }

  /* view filter (Today / Archived) */
  .cf.on[data-v="today"]    { color: var(--green); border-color: rgba(65,240,165,.5); background: rgba(65,240,165,.08); }
  .cf.on[data-v="archived"] { color: var(--muted); border-color: var(--line2); background: rgba(81,102,123,.12); }
  .row.arch { opacity: .6; }
  .when.tdy { color: var(--green); }

  /* structured subtask list */
  .st-row { padding: 4px 0; }
  .st-row.done .st-text { color: var(--muted); }
  .st-main { flex: 1; min-width: 0; }
  .st-reason { font-size: 12.5px; color: var(--muted); font-style: italic; margin-top: 1px; }
  .st-btn {
    border: none; background: none; padding: 2px 5px; font-size: 12px;
    color: var(--muted); opacity: 0; transition: opacity .12s, color .12s;
  }
  .st-row:hover .st-btn { opacity: .8; }
  .st-btn:hover { color: var(--cyan); box-shadow: none; }
  .st-btn.del:hover { color: var(--red); }
  .st-none { color: var(--muted); font-style: italic; }
  .st-add { margin-top: 9px; }
  .st-add input, .st-ed input {
    width: 100%; font: 13.5px/1.5 var(--sans); color: var(--ink);
    background: var(--bg0); border: 1px solid var(--line2); border-radius: 3px; padding: 6px 10px;
  }
  .st-add input:focus, .st-ed input:focus { outline: none; border-color: var(--cyan-dim); box-shadow: 0 0 8px rgba(65,216,247,.15); }
  .st-ed { flex: 1; display: flex; flex-direction: column; gap: 6px; }
  .st-ed-btns { display: flex; gap: 6px; }

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

  /* bench page (/bench) — file list + preview pane, drop files into ~/bench */
  .bench { display: none; }
  body.bench-mode .hud, body.bench-mode .panel, body.bench-mode .readouts, body.bench-mode #new-btn { display: none; }
  body.bench-mode .wrap { max-width: 1500px; margin: 0 auto; }
  body.bench-mode .bench { display: grid; grid-template-columns: minmax(300px, 2fr) 3fr; gap: 22px; align-items: start; }
  .bench-side { min-width: 0; }
  .bench-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 14px; }
  .bench-head .bt { font: 700 13px var(--mono); letter-spacing: .22em; color: var(--cyan); text-transform: uppercase; }
  .bench-head .bp { font: 11px var(--mono); color: var(--muted); letter-spacing: .06em; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bench-head .bp button { all: unset; cursor: pointer; color: var(--dim); }
  .bench-head .bp button:hover { color: var(--cyan); text-decoration: underline; }
  .bench-head .bp .sep { margin: 0 5px; opacity: .5; }
  .bench-head .bp .cur { color: var(--cyan); }
  .dropzone {
    display: grid; place-content: center; min-height: 96px; text-align: center;
    border: 1px dashed var(--line2); border-radius: 3px; background: var(--inset);
    color: var(--muted); font: 600 10px/1.9 var(--mono); letter-spacing: .18em; text-transform: uppercase;
    cursor: pointer; transition: all .12s;
  }
  .dropzone:hover, .dropzone.dragover { color: var(--cyan); border-color: var(--cyan); box-shadow: 0 0 24px rgba(65,216,247,.18); }
  .bench-prog { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
  .bench-prog:empty { display: none; }
  .bp-row { display: grid; grid-template-columns: 1fr auto; gap: 4px 10px; font: 11px var(--mono); color: var(--dim); }
  .bp-row .bp-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bp-row .bp-pct { color: var(--cyan); font-variant-numeric: tabular-nums; }
  .bp-row.err .bp-pct { color: var(--red); }
  .bp-row .bp-bar { grid-column: 1 / -1; height: 3px; background: var(--line); border-radius: 2px; overflow: hidden; }
  .bp-row .bp-bar i { display: block; height: 100%; width: 0; background: var(--cyan); transition: width .1s; }
  .bp-row.err .bp-bar i { background: var(--red); }
  .bench-list { margin-top: 16px; border-top: 1px solid var(--line); }
  .bf {
    display: grid; grid-template-columns: 1fr auto auto auto; gap: 12px; align-items: baseline;
    padding: 6px 4px; border-bottom: 1px solid var(--line); font: 12px var(--mono); color: var(--ink);
    cursor: pointer;
  }
  .bf:hover { background: var(--panel2); color: var(--cyan); }
  .bf.sel { background: var(--panel2); box-shadow: inset 2px 0 0 var(--cyan); }
  .bf.sel .bn { color: var(--cyan); }
  .bf .bn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bf .bn.dir { color: var(--dim); }
  .bf.dir:hover .bn { color: var(--cyan); }
  .bf.dir.dragover { background: var(--panel2); box-shadow: inset 0 0 0 1px var(--cyan); }
  .bf.dir.dragover .bn, .bf.dir.dragover .bw { color: var(--cyan); }
  .bf .bs, .bf .bw { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .bf .bd { color: var(--muted); text-decoration: none; padding: 0 3px; font-size: 13px; line-height: 1; }
  .bf .bd:hover { color: var(--cyan); text-shadow: 0 0 8px rgba(65,216,247,.6); }
  .bench-empty { padding: 20px 4px; font: 11px var(--mono); color: var(--muted); letter-spacing: .1em; text-transform: uppercase; }
  .console .benchbtn { align-self: center; margin-right: 10px; }
  /* preview pane */
  .preview {
    position: sticky; top: 20px; min-width: 0; display: flex; flex-direction: column;
    max-height: calc(100vh - 40px); min-height: 60vh;
    border: 1px solid var(--line); border-radius: 4px;
    background: linear-gradient(180deg, var(--panel2), var(--panel));
  }
  .preview::before, .preview::after { content: ""; position: absolute; width: 12px; height: 12px; border: 1px solid var(--cyan); opacity: .8; }
  .preview::before { top: -1px; left: -1px; border-right: 0; border-bottom: 0; }
  .preview::after { bottom: -1px; right: -1px; border-left: 0; border-top: 0; }
  .pv-head { display: flex; align-items: center; gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--line); font: 12px var(--mono); min-height: 42px; }
  .pv-head .pn { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink); }
  .pv-head .pm { color: var(--muted); font-size: 11px; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .pv-head .pv-x { padding: 3px 8px; font-size: 11px; }
  .pv-dl { color: var(--cyan); text-decoration: none; font: 600 10px var(--mono); letter-spacing: .15em; text-transform: uppercase; white-space: nowrap; }
  .pv-dl:hover { text-decoration: underline; }
  .pv-body { flex: 1; min-height: 0; overflow: auto; padding: 14px 16px; }
  .pv-text { margin: 0; font: 12px/1.55 var(--mono); color: var(--dim); white-space: pre-wrap; overflow-wrap: anywhere; tab-size: 4; }
  .pv-img {
    display: block; max-width: 100%; height: auto; margin: 0 auto;
    background: repeating-conic-gradient(var(--panel2) 0 25%, var(--inset) 0 50%) 0 0 / 20px 20px;
  }
  .pv-body .md { font-size: 14px; }
  .pv-body .md h1 { font-size: 18px; } .pv-body .md h2 { font-size: 15px; }
  .pv-empty { display: grid; place-content: center; gap: 8px; height: 100%; min-height: 40vh; text-align: center; font: 600 10px/1.9 var(--mono); letter-spacing: .18em; text-transform: uppercase; color: var(--muted); }
  @media (max-width: 900px) {
    body.bench-mode .bench { grid-template-columns: 1fr; }
    .preview { position: relative; top: auto; max-height: none; min-height: 40vh; }
  }
  @media (max-width: 760px) { .bf .bw { display: none; } }

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
  /* toasts — live feed of agent activity, docked to the right edge like a sidebar */
  .toasts {
    position: fixed; top: 0; right: 0; bottom: 0; z-index: 400;
    display: flex; flex-direction: column; align-items: stretch; gap: 10px;
    width: min(380px, calc(100vw - 24px));
    padding: 16px 12px 16px 16px;
    pointer-events: none; overflow: hidden;
  }
  .toasts:not(:empty) {
    background: linear-gradient(270deg, rgba(0,0,0,.30), transparent 70%);
  }
  .toast {
    display: flex; flex-direction: column; gap: 4px; padding: 10px 14px;
    border: 1px solid var(--line2); border-left: 3px solid var(--amber);
    border-radius: 6px 0 0 6px; margin-right: -12px;
    background: linear-gradient(180deg, var(--panel2), var(--panel));
    box-shadow: 0 8px 28px rgba(0,0,0,.55);
    font: 12px/1.45 var(--mono); color: var(--dim); cursor: pointer;
    pointer-events: auto;
    animation: toast-in .22s ease-out;
  }
  .toast.created { border-left-color: var(--green); }
  .toast.deleted { border-left-color: var(--red); }
  .toast.bench { border-left-color: var(--cyan); }
  .toast .trow { display: flex; align-items: baseline; gap: 8px; }
  .toast .tn { color: var(--cyan); font-weight: 600; flex: none; }
  .toast .td { color: var(--muted); flex: none; margin-left: auto; }
  .toast.created .td { color: var(--green); }
  .toast.deleted .td { color: var(--red); }
  .toast .tt {
    color: var(--ink); font-size: 13px; line-height: 1.4;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .toast.out { opacity: 0; transform: translateX(16px); transition: opacity .3s, transform .3s; }
  @keyframes toast-in { from { opacity: 0; transform: translateX(16px); } }
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
    <a class="btnlink benchbtn" id="bench-btn" href="/bench">⬡ Bench</a>
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
      <div class="hud-sec"><span class="hud-lab">View</span>
        <button class="cf" data-f="view" data-v="all"><span>All</span></button>
        <button class="cf" data-f="view" data-v="today"><span>Today</span></button>
        <button class="cf" data-f="view" data-v="video"><span>To Film</span></button>
        <button class="cf" data-f="view" data-v="archived"><span>Archived</span></button>
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
    <div class="panel-head">Mission Log · Completed<span class="rule"></span><button id="review-all" style="display:none">✓ All filmed</button><span class="n" id="log-n"></span></div>
    <div class="rows" id="log-rows"></div>
  </section>

  <section class="bench" id="bench">
    <div class="bench-side">
      <div class="bench-head">
        <span class="bt">⬡ Bench</span>
        <span class="bp" id="bench-path"></span>
      </div>
      <div class="dropzone" id="bench-drop">drop files anywhere<br>paste · or click to pick<br><span style="opacity:.6">any type · 500 mb cap · lands in <span id="bench-target">~/bench</span></span></div>
      <div class="bench-prog" id="bench-prog"></div>
      <div class="bench-list" id="bench-list"></div>
    </div>
    <div class="preview" id="preview">
      <div class="pv-head" id="pv-head"></div>
      <div class="pv-body" id="pv-body"></div>
    </div>
  </section>
</div>

<div class="toasts" id="toasts"></div>

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
<input type="file" id="benchfile" multiple hidden>

<script>
let tasks = [];
let modalId = null;     // task id shown in modal, '' = new task, null = closed
let editMode = false;
let stEdit = null;      // subtask id currently being edited inline, null = none

const $ = s => document.querySelector(s);
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const STATUS_LABEL = { todo: 'Queued', doing: 'Active', done: 'Done' };
const CATS = __CATEGORIES__; // {slug: {label, color}} from config.json, injected by the server
/* /bench serves this same page; bench mode hides the board and shows the file list + preview */
const BENCH_MODE = location.pathname === '/bench';
if (BENCH_MODE) {
  document.body.classList.add('bench-mode');
  document.title = 'BENCH · TASKCTRL';
  $('#bench-btn').textContent = '⬢ Board'; $('#bench-btn').href = '/';
}
const CAT_LABEL = Object.fromEntries(Object.entries(CATS).map(([k, v]) => [k, v.label || k]));
const TYPE_LABEL = { feature: 'FEAT', bug: 'BUG', chore: 'CHORE' };
const TYPE_FULL = { feature: 'Feature', bug: 'Bug', chore: 'Chore' };
const PRIO_LABEL = { high: '▲ High', normal: 'Normal', low: '▼ Low' };
const PRIO_RANK = { high: 0, normal: 1, low: 2 };
const prioOf = t => PRIO_RANK[t.priority] !== undefined ? t.priority : 'normal';
const todayStr = () => {
  const n = new Date(), p = x => String(x).padStart(2, '0');
  return n.getFullYear() + '-' + p(n.getMonth() + 1) + '-' + p(n.getDate());
};
const doneToday = t => (t.completed_at || '').slice(0, 10) === todayStr();
const stsDoneToday = t => (t.subtasks || []).filter(s => (s.completed_at || '').slice(0, 10) === todayStr()).length;
// exclusive filters, one per dimension, persisted individually
const FILTER_VALS = {
  cat:  ['all', ...Object.keys(CAT_LABEL)],
  typ:  ['all', ...Object.keys(TYPE_LABEL)],
  prio: ['all', ...Object.keys(PRIO_LABEL)],
  view: ['all', 'today', 'video', 'archived'],
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
    // X-Board-Client tells the event feed this change is ours, not an agent's
    headers: body ? {'Content-Type': 'application/json', 'X-Board-Client': '1'} : {'X-Board-Client': '1'},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error((await res.json()).error || res.statusText);
  return res.json();
}

async function refresh() {
  if (editMode || stEdit !== null || modalId === '') return; // never clobber an open editor (incl. the New Task form)
  const stNew = $('#st-new');
  if (stNew && (stNew.value.trim() || document.activeElement === stNew)) return; // mid-typing a new subtask
  tasks = (await api('GET', '/api/tasks')).tasks;
  render();
}

/* structured subtasks: every change PUTs the full array; the server keeps ids,
   creation times and completion stamps for surviving items */
function curSt() { const t = tasks.find(x => x.id === modalId); return t ? (t.subtasks || []) : []; }
async function putSt(list) { await api('PUT', '/api/tasks/' + modalId, { subtasks: list }); await refresh(); }

/* ---------- board ---------- */
function rowHtml(t) {
  const st = t.subtasks || [];
  const notesCk = ckStats(t.notes || '');
  const all = st.length + notesCk.all;
  const done = st.filter(s => s.done).length + notesCk.done;
  const chip = all ? `<span class="chip ${done === all ? 'full' : ''}">CK ${done}/${all}</span>` : '<span></span>';
  const nImg = (t.images || []).length;
  const ichip = nImg ? `<span class="chip">IMG ${nImg}</span>` : '<span></span>';
  const rec = (t.status === 'done' && !t.reviewed) ? '<span class="chip rec">● REC</span>' : '<span></span>';
  const p = prioOf(t);
  const prio = p !== 'normal' ? `<span class="prio ${p}">${PRIO_LABEL[p]}</span>` : '<span></span>';
  const typ = t.type && TYPE_LABEL[t.type]
    ? `<span class="typ ${t.type}">${TYPE_LABEL[t.type]}</span>` : '<span></span>';
  // done rows show when they were closed, not when last touched
  const stamp = (t.status === 'done' && t.completed_at) ? t.completed_at : t.updated_at;
  let when = stamp.slice(5, 16).replace('T', ' · '), whenCls = '';
  if (filters.view === 'today') {
    const n = stsDoneToday(t);
    whenCls = ' tdy';
    when = doneToday(t) ? '✓ ' + (t.completed_at || '').slice(11, 16)
                        : '✓ ' + n + ' subtask' + (n === 1 ? '' : 's');
  }
  return `
  <div class="row ${t.status} ${t.archived ? 'arch' : ''} ${t.prod_shape ? 'pshape' : ''} ${t.category ? 'cat-' + t.category : ''}" data-id="${t.id}" title="${t.category || ''}">
    <span class="lamp ${t.status}"></span>
    <span class="tnum">T${String(t.num).padStart(2, '0')}</span>
    <span class="title">${t.prod_shape ? '<span class="shape">PROD SHAPE</span>' : ''}${esc(t.title)}</span>
    ${typ}
    ${prio}
    ${chip}
    ${ichip}
    ${rec}
    <span class="when${whenCls}">${when}</span>
    <button class="pinbtn ${t.pinned ? 'on' : ''}" data-pin="${t.id}" title="${t.pinned ? 'Unpin' : 'Pin for call agenda'}">📌</button>
  </div>`;
}

function render() {
  // pinned = temporary call agenda; ignores filters, but archived tasks never surface here
  const pins = tasks.filter(t => t.pinned && !t.archived).sort((a, b) => a.num - b.num);
  $('#pins').style.display = pins.length ? '' : 'none';
  $('#pins-rows').innerHTML = pins.map(rowHtml).join('');
  $('#pins-n').textContent = String(pins.length).padStart(2, '0');

  // view dimension first: archived tasks only exist in the Archived view;
  // Today = closed today or ≥1 subtask checked off today
  const visible = tasks
    .filter(t => filters.view === 'archived' ? t.archived : !t.archived)
    .filter(t => filters.view !== 'today' || doneToday(t) || stsDoneToday(t))
    // To Film = completed but not yet covered in an update video, whenever it was closed
    .filter(t => filters.view !== 'video' || (t.status === 'done' && !t.reviewed));
  // prod-shape warnings must never hide behind project/type/priority filters
  const matched = visible.filter(t => t.prod_shape || Object.keys(DIM).every(k => matchDim(t, k)));
  const pool = matched.filter(t => !t.pinned); // pinned rows live only in the agenda section
  const open = pool.filter(t => t.status !== 'done');
  const done = pool.filter(t => t.status === 'done');
  // priority first, then active before queued within each level
  open.sort((a, b) =>
    (PRIO_RANK[prioOf(a)] - PRIO_RANK[prioOf(b)]) ||
    ((a.status === 'doing' ? 0 : 1) - (b.status === 'doing' ? 0 : 1)) || (a.num - b.num));
  // most recently closed first
  done.sort((a, b) => (b.completed_at || b.updated_at).localeCompare(a.completed_at || a.updated_at));

  $('#ops-rows').innerHTML = open.map(rowHtml).join('') || '<div class="empty">// no open tasks — all systems nominal</div>';
  $('#log-rows').innerHTML = done.map(rowHtml).join('') ||
    `<div class="empty">${filters.view === 'video' ? '// nothing left to film — all caught up' : '// nothing completed yet'}</div>`;
  $('#review-all').style.display = (filters.view === 'video' && done.length) ? '' : 'none';
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

function stSection(t) {
  const st = t.subtasks || [];
  const rows = st.map((s, i) => {
    if (stEdit === s.id) return `
      <div class="ck-item st-row"><span class="ck-num">${i + 1}.</span>
        <div class="st-ed">
          <input type="text" id="st-ed-text" value="${esc(s.text)}">
          <input type="text" id="st-ed-reason" value="${esc(s.reasoning || '')}" placeholder="Reasoning — why this subtask exists (optional)">
          <div class="st-ed-btns">
            <button class="primary" data-stsave="${s.id}">Save</button>
            <button data-stcancel="1">Cancel</button>
          </div>
        </div>
      </div>`;
    return `
      <div class="ck-item st-row ${s.done ? 'done' : ''}" title="created ${(s.created_at || '—').replace('T', ' ')}${s.completed_at ? ' · completed ' + s.completed_at.replace('T', ' ') : ''}">
        <span class="ck-num">${i + 1}.</span>
        <input type="checkbox" data-stck="${s.id}" ${s.done ? 'checked' : ''}>
        <div class="st-main">
          <span class="st-text">${esc(s.text)}</span>
          ${s.reasoning ? `<div class="st-reason">${esc(s.reasoning)}</div>` : ''}
        </div>
        <button class="st-btn" data-sted="${s.id}" title="Edit subtask">✎</button>
        <button class="st-btn del" data-stdel="${s.id}" title="Remove subtask">✕</button>
      </div>`;
  }).join('');
  return `<div class="sect-label">Subtasks</div>
    <div class="sect" data-stlist>
      ${rows || '<span class="st-none">— none —</span>'}
      <div class="st-add"><input type="text" id="st-new" placeholder="+ add subtask — Enter to save"></div>
    </div>`;
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
        ${t ? '' : `<label>Subtasks <span class="hint">— one per line (optional)</span></label>
        <textarea id="e-subtasks" class="mono" rows="4"></textarea>`}
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
      <div class="m-id">TASK ${String(t.num).padStart(2, '0')} · ${t.id}${t.prod_shape ? '<span class="m-pin" style="color:var(--red)">PROD DATA SHAPE</span>' : ''}${t.pinned ? '<span class="m-pin">📌 PINNED</span>' : ''}${t.archived ? '<span class="m-pin" style="color:var(--muted)">🗄 ARCHIVED</span>' : ''}</div>
      <h2 class="m-title">${esc(t.title)}</h2>
      <div style="display:flex;gap:10px;flex-wrap:wrap">${segControl(t.status)}${segType(t.type || '')}${Object.keys(CAT_LABEL).length ? segCat(t.category || '') : ''}${segPrio(prioOf(t))}</div>
    </div>
    <div class="m-body">
      ${sect('Reasoning', t.reasoning, false)}
      ${stSection(t)}
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
      <span class="meta">CREATED ${t.created_at.replace('T',' ')} · UPDATED ${t.updated_at.replace('T',' ')}${t.completed_at ? ' · COMPLETED ' + t.completed_at.replace('T',' ') : ''}${t.reviewed_at ? ' · FILMED ' + t.reviewed_at.replace('T',' ') : ''}${t.archived ? ' · ARCHIVED ' + (t.archived_at || '').replace('T',' ') : ''}</span>
      <button class="danger" data-act="delete">Delete</button>
      <button data-act="archive">${t.archived ? 'Unarchive' : '🗄 Archive'}</button>
      ${t.status === 'done' ? `<button data-act="review">${t.reviewed ? 'Unmark filmed' : '🎥 Filmed'}</button>` : ''}
      <button data-act="shape" ${t.prod_shape ? 'style="color:var(--red);border-color:var(--red)"' : ''}>${t.prod_shape ? 'Clear shape' : 'Prod shape'}</button>
      <button data-act="pin">${t.pinned ? 'Unpin' : '📌 Pin'}</button>
      <button data-act="edit">Edit</button>
    </div>`;
}

function closeModal() { closeLb(); modalId = null; editMode = false; stEdit = null; renderModal(); }

async function saveEditor() {
  const body = {
    title: $('#e-title').value,
    reasoning: $('#e-reasoning').value,
    notes: $('#e-notes').value,
    status: $('#e-status').value,
    category: $('#e-cat').value,
    priority: $('#e-prio').value,
    type: $('#e-typ').value,
  };
  if (!body.title.trim()) { $('#e-title').focus(); return; }
  try {
    if (modalId) {
      await api('PUT', '/api/tasks/' + modalId, body); // subtasks untouched — managed in task view
    } else {
      body.subtasks = $('#e-subtasks').value;
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
      method: 'POST', headers: { 'Content-Type': f.type, 'X-Board-Client': '1' }, body: f,
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

  const sted = ev.target.closest('[data-sted]');
  if (sted) { stEdit = sted.dataset.sted; renderModal(); $('#st-ed-text')?.focus(); return; }
  const stcan = ev.target.closest('[data-stcancel]');
  if (stcan) { stEdit = null; renderModal(); return; }
  const stsave = ev.target.closest('[data-stsave]');
  if (stsave) {
    const text = $('#st-ed-text').value.trim();
    if (!text) { $('#st-ed-text').focus(); return; }
    const reasoning = $('#st-ed-reason').value.trim();
    const list = curSt().map(s => s.id === stsave.dataset.stsave ? { ...s, text, reasoning } : s);
    stEdit = null;
    await putSt(list);
    return;
  }
  const stdel = ev.target.closest('[data-stdel]');
  if (stdel) {
    if (confirm('Remove this subtask?')) {
      stEdit = null;
      await putSt(curSt().filter(s => s.id !== stdel.dataset.stdel));
    }
    return;
  }

  const row = ev.target.closest('.row');
  if (row) { modalId = row.dataset.id; editMode = false; stEdit = null; renderModal(); return; }

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
    else if (a === 'shape') {
      const t = tasks.find(x => x.id === modalId);
      await api('PUT', '/api/tasks/' + modalId, { prod_shape: !(t && t.prod_shape) });
      await refresh();
    }
    else if (a === 'archive') {
      const t = tasks.find(x => x.id === modalId);
      await api('PUT', '/api/tasks/' + modalId, { archived: !(t && t.archived) });
      await refresh();
    }
    else if (a === 'review') {
      const t = tasks.find(x => x.id === modalId);
      await api('PUT', '/api/tasks/' + modalId, { reviewed: !(t && t.reviewed) });
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
  const scb = ev.target.closest('input[data-stck]');
  if (scb && modalId && !editMode) {
    await putSt(curSt().map(s => s.id === scb.dataset.stck ? { ...s, done: scb.checked } : s));
    return;
  }
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
  if (ev.key === 'Enter' && ev.target.id === 'st-new') {
    const text = ev.target.value.trim();
    if (text) { ev.target.value = ''; putSt([...curSt(), { text }]).then(() => $('#st-new')?.focus()); }
    return;
  }
  if (ev.key === 'Enter' && (ev.target.id === 'st-ed-text' || ev.target.id === 'st-ed-reason')) {
    $('[data-stsave]')?.click();
    return;
  }
  if (ev.key === 'Escape' && BENCH_MODE && benchFile) { previewFile(''); benchSyncUrl(true); return; }
  if (ev.key === 'Escape' && stEdit !== null) { stEdit = null; renderModal(); return; }
  if (ev.key === 'Escape' && modalId !== null && !editMode) closeModal();
});

/* image intake: paste, drag-drop, file picker (view mode of an existing task only) */
document.addEventListener('paste', ev => {
  if (BENCH_MODE) {
    const files = [...(ev.clipboardData?.files || [])];
    if (files.length) { ev.preventDefault(); uploadBench(files); }
    return;
  }
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

/* ---------- bench page ---------- */
const benchDrop = $('#bench-drop');
const fmtSize = n => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB'
  : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1073741824).toFixed(2) + ' GB';

/* the open folder, relative to ~/bench ("" = root); remembered across reloads */
let benchCwd = '';
try { benchCwd = localStorage.getItem('taskctrl.benchCwd') || ''; } catch {}
let benchFile = '';   // file open in the preview pane ('' = none)
let benchFiles = [];  // listing of the open folder, as the server returned it
const benchJoin = (rel, name) => rel ? rel + '/' + name : name;
const benchLabel = rel => '~/bench' + (rel ? '/' + rel : '');
const benchUrl = (name, inline) =>
  '/bench/' + benchJoin(benchCwd, name).split('/').map(encodeURIComponent).join('/') + (inline ? '?inline=1' : '');
const fmtWhen = m => m.replace('T', ' ').slice(0, 16);

/* folder + open file live in the URL (?p=folder&f=file) so back/forward and bookmarks work */
const benchParams = () => { const u = new URLSearchParams(location.search); return { p: u.get('p') || '', f: u.get('f') || '' }; };
function benchSyncUrl(push) {
  if (!BENCH_MODE) return;
  const u = new URLSearchParams();
  if (benchCwd) u.set('p', benchCwd);
  if (benchFile) u.set('f', benchFile);
  const url = '/bench' + (u.toString() ? '?' + u : '');
  if (url !== location.pathname + location.search) history[push ? 'pushState' : 'replaceState'](null, '', url);
}

function renderBenchCrumb(rel) {
  const parts = rel ? rel.split('/') : [];
  let html = parts.length ? '<button data-rel="">~/bench</button>' : '<span class="cur">~/bench</span>';
  parts.forEach((p, i) => {
    const sub = parts.slice(0, i + 1).join('/');
    html += '<span class="sep">/</span>' + (i === parts.length - 1
      ? `<span class="cur">${esc(p)}</span>` : `<button data-rel="${esc(sub)}">${esc(p)}</button>`);
  });
  $('#bench-path').innerHTML = html;
  $('#bench-target').textContent = benchLabel(rel);
}

async function loadBench(rel = benchCwd) {
  let d;
  try { d = await api('GET', '/api/bench?path=' + encodeURIComponent(rel)); }
  catch (e) {
    if (rel) { benchCwd = ''; return loadBench(''); } // remembered folder is gone → back to root
    throw e;
  }
  benchCwd = d.path; benchFiles = d.files;
  try { localStorage.setItem('taskctrl.benchCwd', benchCwd); } catch {}
  renderBenchCrumb(benchCwd);
  const list = $('#bench-list');
  const up = benchCwd ? `<div class="bf dir" data-rel="${esc(benchCwd.split('/').slice(0, -1).join('/'))}"><span class="bn dir">../</span><span class="bs"></span><span class="bw"></span><span></span></div>` : '';
  if (!d.files.length) { list.innerHTML = up + '<div class="bench-empty">folder is empty</div>'; return; }
  list.innerHTML = up + d.files.map(f => f.dir
    ? `<div class="bf dir" data-rel="${esc(benchJoin(benchCwd, f.name))}" title="open · or drop files onto it"><span class="bn dir">${esc(f.name)}/</span><span class="bs"></span><span class="bw">${esc(fmtWhen(f.mtime))}</span><span></span></div>`
    : `<div class="bf file${f.name === benchFile ? ' sel' : ''}" data-name="${esc(f.name)}" title="preview">` +
      `<span class="bn">${esc(f.name)}</span><span class="bs">${fmtSize(f.size)}</span>` +
      `<span class="bw">${esc(fmtWhen(f.mtime))}</span>` +
      `<a class="bd" href="${benchUrl(f.name)}" download="${esc(f.name)}" title="download">⤓</a></div>`).join('');
}

/* what the pane can show: images inline, markdown rendered, anything text-like as text */
const IMG_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'bmp']);
const TEXT_EXT = new Set(['txt', 'md', 'markdown', 'log', 'csv', 'tsv', 'json', 'jsonl', 'js', 'mjs', 'cjs', 'ts',
  'py', 'sh', 'bash', 'zsh', 'html', 'htm', 'css', 'svg', 'xml', 'yml', 'yaml', 'toml', 'ini', 'conf', 'cfg',
  'env', 'sql', 'diff', 'patch', 'rb', 'go', 'rs', 'java', 'c', 'h', 'cpp', 'php', 'lock', 'gitignore']);
const PREVIEW_MAX = 2 * 1024 * 1024;

async function previewFile(name) {
  benchFile = name || '';
  document.querySelectorAll('.bf.file').forEach(r => r.classList.toggle('sel', r.dataset.name === benchFile));
  const head = $('#pv-head'), body = $('#pv-body');
  if (!benchFile) {
    head.innerHTML = '<span class="pn" style="color:var(--muted)">preview</span>';
    body.innerHTML = '<div class="pv-empty">select a file to preview</div>';
    return;
  }
  const f = benchFiles.find(x => x.name === benchFile && !x.dir);
  const ext = benchFile.includes('.') ? benchFile.split('.').pop().toLowerCase() : '';
  const dl = `<a class="pv-dl" href="${benchUrl(benchFile)}" download="${esc(benchFile)}">⤓ download</a>`;
  head.innerHTML = `<span class="pn" title="${esc(benchLabel(benchCwd) + '/' + benchFile)}">${esc(benchFile)}</span>` +
    `<span class="pm">${f ? fmtSize(f.size) + ' · ' + esc(fmtWhen(f.mtime)) : ''}</span>${f ? dl : ''}` +
    `<button class="pv-x" id="pv-x" title="close (esc)">✕</button>`;
  if (!f) { body.innerHTML = '<div class="pv-empty">no such file in this folder</div>'; return; }
  if (IMG_EXT.has(ext)) { body.innerHTML = `<img class="pv-img" src="${benchUrl(benchFile, true)}" alt="${esc(benchFile)}">`; return; }
  if (!TEXT_EXT.has(ext)) { body.innerHTML = `<div class="pv-empty">no preview for .${esc(ext || '?')} files<span>${dl}</span></div>`; return; }
  if (f.size > PREVIEW_MAX) { body.innerHTML = `<div class="pv-empty">${fmtSize(f.size)} is over the 2 MB preview cap<span>${dl}</span></div>`; return; }
  body.innerHTML = '<div class="pv-empty">loading…</div>';
  const want = benchFile;
  let text;
  try {
    const r = await fetch(benchUrl(benchFile, true), { headers: { 'X-Board-Client': '1' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    text = await r.text();
  } catch (e) { if (benchFile === want) body.innerHTML = `<div class="pv-empty">${esc(e.message)}</div>`; return; }
  if (benchFile !== want) return; // user clicked another file while this one loaded
  body.innerHTML = (ext === 'md' || ext === 'markdown')
    ? `<div class="md">${md(text)}</div>`
    : `<pre class="pv-text">${esc(text)}</pre>`;
  body.scrollTop = 0;
}

/* open a folder (and optionally a file in it), then reflect it in the URL */
async function benchGo(rel, file = '', push = true) {
  await loadBench(rel);
  await previewFile(file);
  benchSyncUrl(push);
}

/* XHR rather than fetch so the progress bar is real — bench files run to hundreds of MB */
function uploadBenchOne(f, rel) {
  const row = document.createElement('div');
  row.className = 'bp-row';
  row.innerHTML = `<span class="bp-name">${esc(f.name)}${rel ? ' → ' + esc(rel) + '/' : ''}</span><span class="bp-pct">0%</span><div class="bp-bar"><i></i></div>`;
  $('#bench-prog').appendChild(row);
  const pct = row.querySelector('.bp-pct'), bar = row.querySelector('.bp-bar i');
  return new Promise(resolve => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/bench');
    xhr.setRequestHeader('Content-Type', f.type || 'application/octet-stream');
    xhr.setRequestHeader('X-Filename', encodeURIComponent(f.name));
    xhr.setRequestHeader('X-Bench-Dir', encodeURIComponent(rel));
    xhr.setRequestHeader('X-Board-Client', '1');
    xhr.upload.onprogress = ev => {
      if (!ev.lengthComputable) return;
      const p = Math.round(ev.loaded / ev.total * 100);
      pct.textContent = p + '%'; bar.style.width = p + '%';
    };
    xhr.onload = () => {
      let res = {};
      try { res = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status === 201) {
        pct.textContent = 'saved as ' + res.name; bar.style.width = '100%';
        showToast({ action: 'bench', title: res.name, detail: fmtSize(res.size) + ' → ' + benchLabel(res.path) });
        setTimeout(() => row.remove(), 2500);
      } else {
        row.classList.add('err'); pct.textContent = res.error || ('HTTP ' + xhr.status);
      }
      resolve();
    };
    xhr.onerror = () => { row.classList.add('err'); pct.textContent = 'network error'; resolve(); };
    xhr.send(f);
  });
}
/* rel = folder to land in; defaults to the open one */
async function uploadBench(files, rel = benchCwd) {
  files = [...files];
  if (!files.length) return;
  for (const f of files) {
    if (f.size > 500 * 1024 * 1024) { alert(f.name + ' is over the 500 MB cap'); continue; }
    await uploadBenchOne(f, rel);
  }
  await loadBench();
}

if (BENCH_MODE) {
  benchDrop.onclick = () => $('#benchfile').click();
  $('#benchfile').onchange = async ev => { await uploadBench(ev.target.files); ev.target.value = ''; };
  // one drop handler for the whole page — a drop on the zone bubbles up here, so a
  // second listener on the zone itself would upload every file twice
  benchDrop.addEventListener('dragover', () => benchDrop.classList.add('dragover'));
  benchDrop.addEventListener('dragleave', () => benchDrop.classList.remove('dragover'));
  document.addEventListener('dragover', ev => ev.preventDefault());
  document.addEventListener('drop', ev => {
    ev.preventDefault(); benchDrop.classList.remove('dragover');
    // dropped onto a folder row → straight into that folder; anywhere else → the open folder
    const dirRow = ev.target.closest('.bf.dir');
    if (dirRow) dirRow.classList.remove('dragover');
    uploadBench(ev.dataTransfer.files, dirRow ? dirRow.dataset.rel : benchCwd);
  });
  // rows: folders open on click, files open in the preview; the ⤓ link downloads as before
  $('#bench-list').addEventListener('click', ev => {
    if (ev.target.closest('.bd')) return;
    const dirRow = ev.target.closest('.bf.dir');
    if (dirRow) { benchGo(dirRow.dataset.rel).catch(e => alert(e.message)); return; }
    const fileRow = ev.target.closest('.bf.file');
    if (fileRow) { previewFile(fileRow.dataset.name); benchSyncUrl(true); }
  });
  $('#bench-list').addEventListener('dragover', ev => {
    const dirRow = ev.target.closest('.bf.dir');
    document.querySelectorAll('.bf.dir.dragover').forEach(r => { if (r !== dirRow) r.classList.remove('dragover'); });
    if (dirRow) dirRow.classList.add('dragover');
  });
  $('#bench-list').addEventListener('dragleave', ev => {
    const dirRow = ev.target.closest('.bf.dir');
    if (dirRow && !dirRow.contains(ev.relatedTarget)) dirRow.classList.remove('dragover');
  });
  $('#bench-path').addEventListener('click', ev => {
    const b = ev.target.closest('button[data-rel]');
    if (b) benchGo(b.dataset.rel).catch(e => alert(e.message));
  });
  $('#pv-head').addEventListener('click', ev => {
    if (ev.target.closest('#pv-x')) { previewFile(''); benchSyncUrl(true); }
  });
  window.addEventListener('popstate', () => { const { p, f } = benchParams(); benchGo(p, f, false).catch(() => {}); });
  // a bare /bench opens the folder remembered from last time; ?p= in the URL wins
  const { p, f } = benchParams();
  benchGo(location.search ? p : benchCwd, f, false).catch(e => alert(e.message));
}

$('#review-all').onclick = async ev => {
  ev.stopPropagation(); // panel-head clicks shouldn't fall through to row handling
  await api('POST', '/api/tasks/mark-reviewed');
  await refresh();
};

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

if (!BENCH_MODE) { refresh(); setInterval(refresh, 15000); } // pick up direct edits to tasks.json

/* ---------- toasts: live feed of changes made through the API ---------- */
let evSeq = null; // last event seq seen; null until the first poll primes it
async function pollEvents() {
  let r;
  try {
    r = await api('GET', evSeq === null ? '/api/events' : '/api/events?since=' + evSeq);
  } catch (e) { return; } // server briefly down — next poll retries
  if (evSeq === null) { evSeq = r.seq; return; } // prime only, no toasts for history
  evSeq = r.seq;
  if (r.events.length && !BENCH_MODE) refresh(); // something changed — show it now, not in 15s
  r.events.filter(e => e.actor !== 'board').forEach(showToast);
}
function showToast(e) {
  const box = $('#toasts');
  while (box.children.length >= 5) box.firstChild.remove(); // cap the stack
  const el = document.createElement('div');
  el.className = 'toast ' + e.action;
  el.innerHTML =
    `<div class="trow">` +
      `<span class="tn">${e.action === 'bench' ? 'BENCH' : 'T' + String(e.num).padStart(2, '0')}</span>` +
      `<span class="td">${esc(e.detail || e.action)}</span>` +
    `</div>` +
    `<div class="tt">${esc(e.title)}</div>`;
  const dismiss = () => { el.classList.add('out'); setTimeout(() => el.remove(), 320); };
  el.onclick = () => {
    dismiss();
    if (e.action === 'bench') { if (BENCH_MODE) loadBench().catch(() => {}); else location.href = '/bench'; return; }
    if (e.action !== 'deleted' && tasks.find(t => t.id === e.task_id)) {
      modalId = e.task_id; editMode = false; stEdit = null; renderModal();
    }
  };
  box.appendChild(el);
  setTimeout(dismiss, 7000);
}
setInterval(pollEvents, 4000); pollEvents();
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
