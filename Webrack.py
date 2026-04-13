#!/usr/bin/env python3
"""
webrack.py — Directory-to-HTTP Module Server  (Python/Web Edition)
══════════════════════════════════════════════════════════════════
A full reimagining of webrack.c as a sleek Python app with a
browser-based UI.  Drop-in replacement: same concept, same config
file (~/.config/webrack/modules.conf), zero GTK dependency.

Usage
─────
  pip install flask
  python webrack.py
  → Open http://localhost:7474

Features
────────
  • Per-module HTTP/1.1 file servers (threaded, port-per-module)
  • Directory listings with sizes + timestamps
  • MIME detection from extension
  • Live stats streamed via SSE (no polling, no WebSocket dep)
  • Config auto-saved; restored on next launch
  • Keyboard shortcut: ⌘/Ctrl+N to add a module
"""

import os, sys, json, time, threading, urllib.parse, html as _html, queue, logging
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.getLogger("werkzeug").setLevel(logging.ERROR)

try:
    from flask import Flask, jsonify, request, Response, stream_with_context
except ImportError:
    print("Flask is required:  pip install flask"); sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────────────────────

MGMT_PORT   = 7474
MAX_MODULES = 16
CONFIG_FILE = Path.home() / ".config" / "webrack" / "modules.conf"

MIME = {
    "html":"text/html;charset=utf-8","htm":"text/html;charset=utf-8",
    "css":"text/css","js":"application/javascript","mjs":"application/javascript",
    "json":"application/json","txt":"text/plain;charset=utf-8",
    "md":"text/plain;charset=utf-8","xml":"application/xml",
    "svg":"image/svg+xml","png":"image/png","jpg":"image/jpeg",
    "jpeg":"image/jpeg","gif":"image/gif","webp":"image/webp",
    "avif":"image/avif","ico":"image/x-icon","pdf":"application/pdf",
    "zip":"application/zip","gz":"application/gzip","tar":"application/x-tar",
    "mp4":"video/mp4","webm":"video/webm","mp3":"audio/mpeg",
    "wav":"audio/wav","ogg":"audio/ogg","woff":"font/woff","woff2":"font/woff2",
    "c":"text/plain;charset=utf-8","h":"text/plain;charset=utf-8",
    "py":"text/plain;charset=utf-8","rs":"text/plain;charset=utf-8",
    "go":"text/plain;charset=utf-8","ts":"text/plain;charset=utf-8",
    "sh":"text/plain;charset=utf-8","yml":"text/plain;charset=utf-8",
    "yaml":"text/plain;charset=utf-8","toml":"text/plain;charset=utf-8",
}

def mime_for(p: str) -> str:
    return MIME.get(Path(p).suffix.lstrip(".").lower(), "application/octet-stream")

def fmt_bytes(b: int) -> str:
    for unit, div in (("GB", 1<<30), ("MB", 1<<20), ("KB", 1<<10)):
        if b >= div: return f"{b/div:.1f} {unit}"
    return f"{b} B"

# ─────────────────────────────────────────────────────────────────────────────
#  Module — owns one file-serving HTTPServer on its own thread
# ─────────────────────────────────────────────────────────────────────────────

class Module:
    _ctr = 0
    _ctr_lock = threading.RLock()

    def __init__(self, root: str, port: int):
        with Module._ctr_lock:
            Module._ctr += 1
            self.id = Module._ctr
        self.root   = str(Path(root).resolve())
        self.port   = port
        self.active = False
        self._srv: HTTPServer | None = None
        self._lock  = threading.RLock()
        self.requests  = 0
        self.bytes_out = 0

    def _inc(self, n: int):
        with self._lock: self.requests += 1; self.bytes_out += n

    def to_dict(self) -> dict:
        with self._lock:
            return dict(id=self.id, root=self.root, port=self.port,
                        active=self.active, requests=self.requests,
                        bytes_out=self.bytes_out, bytes_fmt=fmt_bytes(self.bytes_out),
                        url=f"http://localhost:{self.port}" if self.active else None,
                        name=Path(self.root).name or self.root)

    def start(self) -> bool | str:
        """Returns True on success, or an error string."""
        if self.active: return True
        mod = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self):  self._handle()
            def do_HEAD(self): self._handle(head=True)

            def _handle(self, head=False):
                raw  = urllib.parse.unquote(self.path.split("?")[0])
                safe = raw.lstrip("/")
                try:
                    full = (Path(mod.root) / safe).resolve()
                    full.relative_to(Path(mod.root).resolve())
                except ValueError:
                    self.send_error(403); return
                if not full.exists():  self.send_error(404); return
                if full.is_dir():      self._dir(full, raw, head)
                else:                  self._file(full, head)

            def _file(self, p: Path, head: bool):
                try:   data = p.read_bytes()
                except: self.send_error(403); return
                self.send_response(200)
                self.send_header("Content-Type",   mime_for(str(p)))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control",  "no-cache")
                self.end_headers()
                if not head: self.wfile.write(data); mod._inc(len(data))

            def _dir(self, p: Path, url: str, head: bool):
                idx = p / "index.html"
                if idx.exists(): self._file(idx, head); return
                if not url.endswith("/"):
                    self.send_response(301)
                    self.send_header("Location", url + "/"); self.end_headers(); return
                rows = ""
                if url != "/":
                    rows += '<tr><td class="ic">🗂</td><td><a href="../">../</a></td><td></td><td></td></tr>'
                try:
                    ents = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
                except PermissionError: self.send_error(403); return
                for e in ents:
                    if e.name.startswith("."): continue
                    st = e.stat()
                    ic = "📁" if e.is_dir() else "📄"
                    sz = "" if e.is_dir() else fmt_bytes(st.st_size)
                    mt = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                    hr = _html.escape(e.name + ("/" if e.is_dir() else ""))
                    nm = _html.escape(e.name)
                    rows += f'<tr><td class="ic">{ic}</td><td><a href="{hr}">{nm}</a></td><td>{sz}</td><td>{mt}</td></tr>'
                body = (DIR_HTML
                    .replace("{{TITLE}}", _html.escape(url))
                    .replace("{{ROWS}}",  rows)
                    .replace("{{ROOT}}",  _html.escape(mod.root))
                    .replace("{{PORT}}",  str(mod.port))
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type",   "text/html;charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head: self.wfile.write(body); mod._inc(len(body))

        try:
            srv = HTTPServer(("", self.port), Handler)
            srv.timeout = 0.5
            self._srv   = srv
            self.active = True
            def _run():
                while self.active: srv.handle_request()
                srv.server_close()
            threading.Thread(target=_run, daemon=True, name=f"wrm{self.id}").start()
            return True
        except OSError as e:
            return str(e)

    def stop(self):
        if not self.active: return
        self.active = False
        if self._srv:
            try: self._srv.server_close()
            except: pass
        self._srv = None

# ─────────────────────────────────────────────────────────────────────────────
#  Registry — thread-safe module list + persistence
# ─────────────────────────────────────────────────────────────────────────────

class Registry:
    def __init__(self):
        self._lock = threading.RLock()
        self._mods: list[Module] = []

    def all(self) -> list[Module]:
        with self._lock: return list(self._mods)

    def by_id(self, mid: int) -> Module | None:
        with self._lock:
            for m in self._mods:
                if m.id == mid: return m
        return None

    def add(self, root: str, port: int) -> tuple[Module | None, str | None]:
        if not Path(root).is_dir():   return None, f"Not a directory: {root}"
        if not (1 <= port <= 65535):  return None, "Port must be between 1 and 65535"
        with self._lock:
            if len(self._mods) >= MAX_MODULES: return None, f"Maximum of {MAX_MODULES} modules reached"
            for m in self._mods:
                if m.port == port: return None, f"Port {port} is already used by another module"
            mod = Module(root, port)
            self._mods.append(mod)
        self._save()
        return mod, None

    def remove(self, mid: int) -> bool:
        with self._lock:
            for i, m in enumerate(self._mods):
                if m.id == mid:
                    m.stop(); self._mods.pop(i); self._save(); return True
        return False

    def _save(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            txt = "".join(f"{m.port}\t{m.root}\n" for m in self._mods)
        CONFIG_FILE.write_text(txt)

    def load(self):
        if not CONFIG_FILE.exists(): return
        for ln in CONFIG_FILE.read_text().splitlines():
            ln = ln.strip()
            if "\t" not in ln: continue
            ps, root = ln.split("\t", 1)
            try: port = int(ps)
            except: continue
            if Path(root).is_dir() and 1 <= port <= 65535:
                self.add(root, port)

REG = Registry()

# ─────────────────────────────────────────────────────────────────────────────
#  SSE broadcast
# ─────────────────────────────────────────────────────────────────────────────

_qs: list[queue.Queue] = []
_ql = threading.RLock()

def _push(evt: str, data):
    msg = f"event:{evt}\ndata:{json.dumps(data)}\n\n"
    with _ql:
        for q in list(_qs):
            try: q.put_nowait(msg)
            except queue.Full: pass

def _stats_loop():
    while True:
        time.sleep(0.9)
        _push("stats", [m.to_dict() for m in REG.all()])

threading.Thread(target=_stats_loop, daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
#  Flask API
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def index(): return UI

@app.route("/api/modules", methods=["GET"])
def api_list(): return jsonify([m.to_dict() for m in REG.all()])

@app.route("/api/modules", methods=["POST"])
def api_add():
    b = request.get_json(force=True) or {}
    root = str(b.get("root", "")).strip()
    try:    port = int(b.get("port", 0))
    except: return jsonify(error="Invalid port number"), 400
    mod, err = REG.add(root, port)
    if err: return jsonify(error=err), 400
    _push("added", mod.to_dict())
    return jsonify(mod.to_dict()), 201

@app.route("/api/modules/<int:mid>", methods=["DELETE"])
def api_del(mid):
    return jsonify(ok=True) if REG.remove(mid) else (jsonify(error="Not found"), 404)

@app.route("/api/modules/<int:mid>/start", methods=["POST"])
def api_start(mid):
    m = REG.by_id(mid)
    if not m: return jsonify(error="Not found"), 404
    r = m.start()
    if r is not True: return jsonify(error=str(r)), 500
    _push("update", m.to_dict())
    return jsonify(m.to_dict())

@app.route("/api/modules/<int:mid>/stop", methods=["POST"])
def api_stop(mid):
    m = REG.by_id(mid)
    if not m: return jsonify(error="Not found"), 404
    m.stop(); _push("update", m.to_dict())
    return jsonify(m.to_dict())

@app.route("/api/events")
def api_sse():
    q: queue.Queue = queue.Queue(maxsize=80)
    with _ql: _qs.append(q)
    def stream():
        yield f"event:init\ndata:{json.dumps([m.to_dict() for m in REG.all()])}\n\n"
        try:
            while True:
                try:    yield q.get(timeout=20)
                except queue.Empty: yield ":ping\n\n"
        finally:
            with _ql:
                try: _qs.remove(q)
                except ValueError: pass
    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# ─────────────────────────────────────────────────────────────────────────────
#  Directory listing template (served by each module's file server)
# ─────────────────────────────────────────────────────────────────────────────

DIR_HTML = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#04060c;--surf:#080c16;--border:rgba(255,255,255,.05);--accent:#00e5b0;
  --ind:#818cf8;--txt:#dde4f0;--txt2:#8892a8;--txt3:#2d3748;--mono:'JetBrains Mono',monospace}
body{background:var(--bg);color:var(--txt);font-family:var(--mono);font-size:13px;
  padding:40px 48px;min-height:100vh;-webkit-font-smoothing:antialiased}
.header{margin-bottom:32px}
.title{font-size:15px;font-weight:500;color:var(--txt);margin-bottom:6px;letter-spacing:-0.02em}
.title span{color:var(--accent)}
.meta{font-size:11px;color:var(--txt3);display:flex;gap:16px;align-items:center}
.badge{padding:2px 10px;border-radius:100px;background:rgba(129,140,248,.12);
  border:1px solid rgba(129,140,248,.2);color:var(--ind);font-size:10px;letter-spacing:.06em}
table{width:100%;border-collapse:collapse}
thead th{text-align:left;padding:7px 14px;color:var(--txt3);font-size:10px;
  text-transform:uppercase;letter-spacing:.12em;border-bottom:1px solid var(--border)}
tbody td{padding:10px 14px;border-bottom:1px solid rgba(255,255,255,.025)}
.ic{width:28px;opacity:.55}
td:nth-child(3){color:var(--ind);font-size:11px}
td:nth-child(4){color:var(--txt3);font-size:11px}
a{color:var(--txt);text-decoration:none;transition:color .15s}
a:hover{color:var(--accent)}
tr:hover td{background:rgba(0,229,176,.025)}
.foot{margin-top:28px;font-size:11px;color:var(--txt3)}
</style></head><body>
<div class="header">
  <div class="title">/ <span>{{TITLE}}</span></div>
  <div class="meta">
    <span class="badge">WebRack</span>
    <span>{{ROOT}}</span>
    <span>:{{PORT}}</span>
  </div>
</div>
<table>
<thead><tr><th></th><th>Name</th><th>Size</th><th>Modified</th></tr></thead>
<tbody>{{ROWS}}</tbody>
</table>
<div class="foot">WebRack · directory module server</div>
</body></html>"""

# ─────────────────────────────────────────────────────────────────────────────
#  Management UI  (the gorgeous part)
# ─────────────────────────────────────────────────────────────────────────────

UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WebRack</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;500;600;700;800&family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500&family=JetBrains+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
/* ── Reset ── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:           #04050d;
  --surf:         #080c18;
  --card:         rgba(10, 14, 26, 0.88);
  --card-h:       rgba(12, 17, 32, 0.95);
  --border:       rgba(255, 255, 255, 0.055);
  --border-h:     rgba(255, 255, 255, 0.11);
  --accent:       #00e5b0;
  --accent-dim:   rgba(0, 229, 176, 0.10);
  --accent-glow:  rgba(0, 229, 176, 0.28);
  --accent-mid:   rgba(0, 229, 176, 0.18);
  --ind:          #818cf8;
  --ind-dim:      rgba(129, 140, 248, 0.10);
  --danger:       #ff4560;
  --danger-dim:   rgba(255, 69, 96, 0.10);
  --txt:          #dde4f2;
  --txt2:         #8892a8;
  --txt3:         #38455a;
  --display:      'Syne', system-ui, sans-serif;
  --sans:         'DM Sans', system-ui, sans-serif;
  --mono:         'JetBrains Mono', monospace;
  --r-sm:         10px;
  --r-md:         16px;
  --r-lg:         22px;
  --r-xl:         30px;
}

html, body { height: 100%; }
body {
  background: var(--bg);
  color: var(--txt);
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.55;
  min-height: 100%;
  overflow-x: hidden;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* ── Animated atmosphere ── */
.atm {
  position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden;
}
.atm-grid {
  position: absolute; inset: -100px;
  background-image:
    linear-gradient(rgba(255,255,255,.016) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.016) 1px, transparent 1px);
  background-size: 52px 52px;
  animation: gridrift 80s linear infinite;
}
@keyframes gridrift { to { transform: translate(52px, 52px); } }

.atm-aurora {
  position: absolute; inset: 0;
  background:
    radial-gradient(ellipse 80% 55% at 10% 75%, rgba(0,229,176,.038) 0%, transparent 65%),
    radial-gradient(ellipse 60% 70% at 90% 15%, rgba(129,140,248,.032) 0%, transparent 65%),
    radial-gradient(ellipse 50% 40% at 50% 50%, rgba(0,90,255,.018) 0%, transparent 80%);
  animation: aurorapulse 22s ease-in-out infinite alternate;
}
@keyframes aurorapulse {
  0%   { opacity: .6; transform: scale(1); }
  100% { opacity: 1;  transform: scale(1.06); }
}

/* ── Shell ── */
.shell {
  position: relative; z-index: 1;
  display: flex; flex-direction: column;
  min-height: 100vh;
  max-width: 1180px;
  margin: 0 auto;
  padding: 0 32px 72px;
}

/* ── Header ── */
.hdr {
  position: sticky; top: 0; z-index: 200;
  display: flex; align-items: center; gap: 14px;
  padding: 18px 32px;
  margin: 0 -32px;
  background: rgba(4, 5, 13, 0.82);
  backdrop-filter: blur(28px) saturate(1.6);
  -webkit-backdrop-filter: blur(28px) saturate(1.6);
  border-bottom: 1px solid var(--border);
}

.brand { display: flex; align-items: baseline; gap: 10px; flex: 1; }
.brand-hex {
  font-size: 20px; line-height: 1;
  filter: drop-shadow(0 0 10px var(--accent)) drop-shadow(0 0 20px rgba(0,229,176,.4));
  animation: hexglow 3s ease-in-out infinite alternate;
}
@keyframes hexglow {
  0%   { filter: drop-shadow(0 0 8px var(--accent)) drop-shadow(0 0 18px rgba(0,229,176,.35)); }
  100% { filter: drop-shadow(0 0 14px var(--accent)) drop-shadow(0 0 28px rgba(0,229,176,.55)); }
}
.brand-name {
  font-family: var(--display);
  font-size: 21px; font-weight: 700;
  color: var(--txt); letter-spacing: -0.045em;
}
.brand-tag {
  font-family: var(--mono);
  font-size: 10px; font-weight: 300;
  color: var(--txt3); letter-spacing: .09em;
  text-transform: uppercase;
}

.hdr-right { display: flex; align-items: center; gap: 10px; }

/* Status pill */
.st-pill {
  display: flex; align-items: center; gap: 7px;
  padding: 5px 13px;
  border-radius: 100px;
  background: var(--surf);
  border: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 11px; font-weight: 400;
  color: var(--txt3);
  transition: all .35s;
  user-select: none;
}
.st-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--txt3);
  transition: all .35s;
  flex-shrink: 0;
}
.st-pill.live { color: var(--accent); border-color: rgba(0,229,176,.22); }
.st-pill.live .st-dot {
  background: var(--accent);
  box-shadow: 0 0 0 3px rgba(0,229,176,.2);
  animation: dotpulse 1.8s ease-in-out infinite;
}
@keyframes dotpulse { 50% { box-shadow: 0 0 0 5px rgba(0,229,176,.06); } }

/* Add button */
.btn-add {
  display: flex; align-items: center; gap: 6px;
  padding: 8px 20px;
  border-radius: 100px;
  border: 1px solid var(--accent);
  background: transparent;
  color: var(--accent);
  font-family: var(--sans); font-size: 13px; font-weight: 500;
  cursor: pointer; letter-spacing: .01em;
  transition: all .2s; position: relative; overflow: hidden;
}
.btn-add::after {
  content: ""; position: absolute; inset: 0;
  background: var(--accent); opacity: 0; transition: opacity .2s;
}
.btn-add:hover::after { opacity: .1; }
.btn-add:hover { box-shadow: 0 0 22px var(--accent-glow); }
.btn-add:active { transform: scale(.97); }
.btn-add-plus { font-size: 18px; line-height: 1; }

/* ── Main ── */
.main { flex: 1; padding-top: 36px; }

/* ── Empty ── */
.empty {
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  padding: 110px 24px;
  gap: 14px;
  animation: fadein .5s .15s both;
}
@keyframes fadein { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: none; } }
.empty-orb {
  width: 72px; height: 72px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 30px;
  background: rgba(255,255,255,.03);
  border: 1px solid var(--border);
  margin-bottom: 4px;
}
.empty-title {
  font-family: var(--display); font-size: 19px; font-weight: 600;
  color: var(--txt3); letter-spacing: -.03em;
}
.empty-body {
  font-size: 13px; color: var(--txt3); text-align: center;
  max-width: 280px; line-height: 1.65;
}
.empty-body strong { color: var(--txt2); }

/* ── Grid ── */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
  gap: 14px;
}
@media (max-width: 560px) { .grid { grid-template-columns: 1fr; } }

/* ── Card ── */
.card {
  position: relative;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  overflow: hidden;
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
  transition: border-color .3s, box-shadow .3s, transform .2s, background .3s;
  animation: cardrise .45s cubic-bezier(.16,1,.3,1) both;
  will-change: transform;
}
@keyframes cardrise {
  from { opacity: 0; transform: translateY(22px) scale(.98); }
  to   { opacity: 1; transform: none; }
}
.card:hover {
  border-color: var(--border-h);
  background: var(--card-h);
  box-shadow: 0 10px 40px rgba(0,0,0,.45);
  transform: translateY(-2px);
}
.card.active {
  border-color: rgba(0, 229, 176, 0.2);
  box-shadow:
    0 0 0 1px rgba(0, 229, 176, 0.07),
    0 10px 36px rgba(0, 0, 0, .5);
}
.card.active:hover {
  border-color: rgba(0, 229, 176, 0.32);
  box-shadow:
    0 0 0 1px rgba(0, 229, 176, 0.12),
    0 12px 42px rgba(0, 0, 0, .55),
    0 0 60px rgba(0, 229, 176, 0.04);
}

/* Side accent bar */
.card-bar {
  position: absolute; top: 0; left: 0;
  width: 3px; height: 100%;
  background: var(--txt3);
  transition: background .4s, box-shadow .4s;
}
.card.active .card-bar {
  background: var(--accent);
  box-shadow: 2px 0 16px rgba(0, 229, 176, 0.4), 2px 0 6px rgba(0, 229, 176, 0.6);
}

/* Card body */
.card-body { padding: 20px 22px 18px 26px; }

.card-top {
  display: flex; align-items: flex-start; gap: 12px; margin-bottom: 14px;
}
.card-icon { font-size: 21px; line-height: 1; margin-top: 2px; opacity: .75; flex-shrink: 0; }
.card-meta { flex: 1; min-width: 0; }
.card-name {
  font-family: var(--mono); font-size: 13px; font-weight: 500;
  color: var(--txt); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; line-height: 1.3;
}
.card-path {
  font-family: var(--mono); font-size: 11px; font-weight: 300;
  color: var(--txt3); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; margin-top: 3px;
}
.port-badge {
  flex-shrink: 0; margin-top: 2px;
  padding: 3px 10px; border-radius: 100px;
  background: var(--ind-dim);
  border: 1px solid rgba(129,140,248,.14);
  font-family: var(--mono); font-size: 11px;
  color: var(--ind); font-weight: 400;
}

/* URL row */
.card-url {
  height: 20px; margin-bottom: 16px;
  font-family: var(--mono); font-size: 12px;
}
.card-url a {
  color: var(--accent); text-decoration: none;
  opacity: .8; transition: opacity .2s;
}
.card-url a:hover { opacity: 1; }
.card-url .off { color: var(--txt3); }

/* Stats */
.card-stats { display: flex; align-items: center; gap: 18px; }
.stat {
  display: flex; align-items: center; gap: 5px;
  font-family: var(--mono); font-size: 12px; color: var(--txt2);
}
.stat-lbl { font-size: 10px; color: var(--txt3); text-transform: uppercase; letter-spacing: .08em; }
.stat-val { font-weight: 500; transition: color .3s; }
.stat-val.bump { color: var(--accent); animation: bumpfade .6s forwards; }
@keyframes bumpfade { 0% { color: var(--accent); } 100% { color: var(--txt2); } }

/* Card footer */
.card-foot {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 22px 13px 26px;
  border-top: 1px solid var(--border);
  background: rgba(0,0,0,.12);
}

/* Remove button */
.btn-rm {
  display: flex; align-items: center; gap: 5px;
  padding: 5px 11px; border-radius: 100px;
  border: 1px solid transparent;
  background: transparent; cursor: pointer;
  color: var(--txt3); font-family: var(--sans); font-size: 12px;
  transition: all .2s;
}
.btn-rm:hover {
  color: var(--danger);
  background: var(--danger-dim);
  border-color: rgba(255,69,96,.15);
}

/* ── Toggle ── */
.tog-wrap { display: flex; align-items: center; gap: 9px; }
.tog-lbl {
  font-family: var(--mono); font-size: 11px;
  color: var(--txt3); min-width: 22px; text-align: right;
  transition: color .3s;
}
.tog-lbl.on { color: var(--accent); }

.tog { position: relative; width: 52px; height: 28px; cursor: pointer; }
.tog input { position: absolute; opacity: 0; width: 0; height: 0; }
.tog-track {
  position: absolute; inset: 0; border-radius: 100px;
  background: rgba(255,255,255,.06);
  border: 1px solid rgba(255,255,255,.07);
  transition: background .3s, border-color .3s, box-shadow .3s;
}
.tog input:checked ~ .tog-track {
  background: rgba(0, 229, 176, .18);
  border-color: rgba(0, 229, 176, .38);
  box-shadow: 0 0 14px rgba(0,229,176,.22), inset 0 0 10px rgba(0,229,176,.06);
}
.tog-thumb {
  position: absolute; top: 4px; left: 4px;
  width: 20px; height: 20px; border-radius: 50%;
  background: rgba(255,255,255,.35);
  transition: transform .26s cubic-bezier(.4,0,.2,1), background .3s, box-shadow .3s;
}
.tog input:checked ~ .tog-thumb {
  transform: translateX(24px);
  background: var(--accent);
  box-shadow: 0 0 12px rgba(0,229,176,.7);
}

/* ── Modal ── */
.ovl {
  position: fixed; inset: 0; z-index: 500;
  background: rgba(2, 3, 8, 0.75);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  display: none; align-items: center; justify-content: center; padding: 24px;
}
.ovl.open { display: flex; animation: ovlfade .18s ease; }
@keyframes ovlfade { from { opacity: 0; } }

.modal {
  position: relative;
  background: #0a0e1c;
  border: 1px solid rgba(255,255,255,.1);
  border-radius: var(--r-xl);
  padding: 34px 34px 30px;
  width: 100%; max-width: 490px;
  box-shadow: 0 40px 100px rgba(0,0,0,.8), 0 0 0 1px rgba(255,255,255,.04);
  animation: modalrise .28s cubic-bezier(.16,1,.3,1);
  overflow: hidden;
}
@keyframes modalrise {
  from { opacity: 0; transform: scale(.94) translateY(10px); }
}
/* Top shimmer line */
.modal::before {
  content: ""; position: absolute; top: 0; left: 0; right: 0; height: 1px;
  background: linear-gradient(90deg, transparent 5%, rgba(0,229,176,.35) 50%, transparent 95%);
}

.modal-title {
  font-family: var(--display); font-size: 21px; font-weight: 600;
  color: var(--txt); letter-spacing: -.04em; margin-bottom: 6px;
}
.modal-sub { font-size: 13px; color: var(--txt2); margin-bottom: 30px; }

.field { margin-bottom: 22px; }
.field-lbl {
  display: block; font-family: var(--mono); font-size: 10px;
  color: var(--txt3); text-transform: uppercase; letter-spacing: .1em; margin-bottom: 9px;
}
.field-row { display: flex; gap: 8px; }
.inp {
  width: 100%; padding: 11px 14px;
  background: rgba(255,255,255,.04);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  color: var(--txt); font-family: var(--mono); font-size: 13px;
  outline: none; transition: border-color .2s, box-shadow .2s, background .2s;
  appearance: none; -webkit-appearance: none;
}
.inp::placeholder { color: var(--txt3); }
.inp:focus {
  border-color: rgba(0,229,176,.38);
  box-shadow: 0 0 0 3px rgba(0,229,176,.08);
  background: rgba(0,229,176,.025);
}
.inp-sm { width: 96px !important; flex-shrink: 0; }

.modal-err {
  padding: 10px 14px; border-radius: var(--r-sm);
  background: var(--danger-dim); border: 1px solid rgba(255,69,96,.2);
  color: var(--danger); font-size: 12px;
  margin-top: -8px; margin-bottom: 16px;
  display: none;
}
.modal-err.show { display: block; }

.modal-acts { display: flex; gap: 10px; justify-content: flex-end; margin-top: 28px; }
.btn {
  padding: 10px 22px; border-radius: 100px; border: none;
  font-family: var(--sans); font-size: 13px; font-weight: 500; cursor: pointer;
  transition: all .2s;
}
.btn-ghost {
  background: transparent; color: var(--txt2);
  border: 1px solid var(--border);
}
.btn-ghost:hover { background: rgba(255,255,255,.04); color: var(--txt); }
.btn-prim {
  background: var(--accent); color: #04050d;
  font-weight: 600; letter-spacing: .01em;
}
.btn-prim:hover { box-shadow: 0 0 24px rgba(0,229,176,.45); transform: translateY(-1px); }
.btn-prim:active { transform: scale(.97); }
.btn-prim:disabled { opacity: .35; pointer-events: none; }

/* ── Toasts ── */
.toasts {
  position: fixed; bottom: 28px; right: 28px; z-index: 1000;
  display: flex; flex-direction: column; gap: 8px; pointer-events: none;
}
.toast {
  padding: 11px 18px; border-radius: var(--r-md);
  font-size: 13px; max-width: 320px;
  border: 1px solid var(--border);
  backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
  animation: toastin .3s cubic-bezier(.16,1,.3,1);
}
@keyframes toastin { from { opacity: 0; transform: translateX(18px) scale(.95); } }
.toast-ok  { background: rgba(0,229,176,.1);  border-color: rgba(0,229,176,.2);  color: var(--accent); }
.toast-err { background: rgba(255,69,96,.1);  border-color: rgba(255,69,96,.2);  color: var(--danger); }
.toast-inf { background: rgba(10,14,26,.92);  color: var(--txt2); }

/* ── Utilities ── */
.hidden { display: none !important; }
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,.07); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.13); }
</style>
</head>
<body>

<div class="atm" aria-hidden="true">
  <div class="atm-grid"></div>
  <div class="atm-aurora"></div>
</div>

<div class="shell">

  <header class="hdr" role="banner">
    <div class="brand">
      <span class="brand-hex" aria-hidden="true">⬡</span>
      <span class="brand-name">WebRack</span>
      <span class="brand-tag">directory server</span>
    </div>
    <div class="hdr-right">
      <div class="st-pill" id="stpill" role="status" aria-live="polite">
        <div class="st-dot"></div>
        <span id="sttext">loading…</span>
      </div>
      <button class="btn-add" onclick="openModal()" title="Add module (⌘N)">
        <span class="btn-add-plus" aria-hidden="true">+</span>
        Add Module
      </button>
    </div>
  </header>

  <main class="main" id="main">
    <div class="empty hidden" id="empty">
      <div class="empty-orb" aria-hidden="true">📦</div>
      <div class="empty-title">No modules yet</div>
      <div class="empty-body">
        Click <strong>Add Module</strong> to start serving a directory
        over HTTP. Each module binds its own port.
      </div>
    </div>
    <div class="grid" id="grid" role="list" aria-label="Modules"></div>
  </main>

</div>

<!-- Add Module Modal -->
<div class="ovl" id="ovl" role="dialog" aria-modal="true"
     aria-labelledby="modal-title" onclick="ovlClick(event)">
  <div class="modal" id="modal">
    <div class="modal-title" id="modal-title">New Module</div>
    <div class="modal-sub">Serve a local directory over HTTP on any available port.</div>

    <div class="field">
      <label class="field-lbl" for="inp-dir">Directory path</label>
      <input class="inp" id="inp-dir" type="text"
             placeholder="/home/user/projects/site"
             autocomplete="off" spellcheck="false">
    </div>
    <div class="field">
      <label class="field-lbl" for="inp-port">Port</label>
      <div class="field-row">
        <input class="inp inp-sm" id="inp-port" type="number"
               min="1" max="65535" placeholder="8080" value="8080">
      </div>
    </div>

    <div class="modal-err" id="modal-err" role="alert"></div>

    <div class="modal-acts">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-prim" id="btn-confirm" onclick="doAdd()">Add Module</button>
    </div>
  </div>
</div>

<!-- Toasts -->
<div class="toasts" id="toasts" aria-live="assertive"></div>

<script>
/* ══ State ══════════════════════════════════════════════════════════════════ */
const mods = {};   // id (number) → module dict

/* ══ SSE ════════════════════════════════════════════════════════════════════ */

window.addEventListener("beforeunload", () => {
  if (window.es) window.es.close();
});

function connectSSE() {
  window.es = new EventSource("/api/events");

  es.addEventListener("init", e => {
    const list = JSON.parse(e.data);
    Object.keys(mods).forEach(k => delete mods[k]);
    document.getElementById("grid").innerHTML = "";
    list.forEach((m, i) => renderMod(m, i * 55));
    syncStatus();
  });

  es.addEventListener("stats", e => {
    JSON.parse(e.data).forEach(m => { mods[m.id] = m; patchStats(m); });
    syncStatus();
  });

  es.addEventListener("update", e => { const m = JSON.parse(e.data); patchCard(m); syncStatus(); });
  es.addEventListener("added",  e => { const m = JSON.parse(e.data); renderMod(m, 0); syncStatus(); });
  // Let the browser's native EventSource handle reconnections safely.
  es.onerror = () => { console.warn("SSE connection interrupted. Auto-reconnecting..."); };
}

/* ══ Render ═════════════════════════════════════════════════════════════════ */
function cid(id) { return `card-${id}`; }

function renderMod(m, delay = 0) {
  mods[m.id] = m;
  if (document.getElementById(cid(m.id))) { patchCard(m); return; }
  const el = document.createElement("div");
  el.className = "card" + (m.active ? " active" : "");
  el.id = cid(m.id);
  el.setAttribute("role", "listitem");
  el.style.animationDelay = delay + "ms";
  el.innerHTML = cardHTML(m);
  document.getElementById("grid").appendChild(el);
  el.querySelector(".tog input").addEventListener("change", ev => onToggle(m.id, ev.target.checked));
  syncEmpty();
}

function cardHTML(m) {
  const urlHtml = m.active
    ? `<a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.url)} ↗</a>`
    : `<span class="off">localhost:${m.port}</span>`;
  return `
  <div class="card-bar" aria-hidden="true"></div>
  <div class="card-body">
    <div class="card-top">
      <div class="card-icon" aria-hidden="true">📁</div>
      <div class="card-meta">
        <div class="card-name" title="${esc(m.name)}">${esc(m.name)}</div>
        <div class="card-path" title="${esc(m.root)}">${esc(m.root)}</div>
      </div>
      <div class="port-badge" title="Port">:${m.port}</div>
    </div>
    <div class="card-url" id="url-${m.id}">${urlHtml}</div>
    <div class="card-stats">
      <div class="stat">
        <span class="stat-lbl">req</span>
        <span class="stat-val" id="req-${m.id}">${m.requests}</span>
      </div>
      <div class="stat">
        <span class="stat-lbl">tx</span>
        <span class="stat-val" id="byt-${m.id}">${m.bytes_fmt}</span>
      </div>
    </div>
  </div>
  <div class="card-foot">
    <button class="btn-rm" onclick="doRemove(${m.id})" title="Remove module">
      ✕ Remove
    </button>
    <div class="tog-wrap">
      <span class="tog-lbl${m.active ? ' on' : ''}" id="lbl-${m.id}">
        ${m.active ? "On" : "Off"}
      </span>
      <label class="tog" title="${m.active ? 'Stop server' : 'Start server'}">
        <input type="checkbox" id="tog-${m.id}" ${m.active ? "checked" : ""}>
        <div class="tog-track"></div>
        <div class="tog-thumb"></div>
      </label>
    </div>
  </div>`;
}

function patchCard(m) {
  mods[m.id] = m;
  const card = document.getElementById(cid(m.id));
  if (!card) { renderMod(m); return; }
  card.className = "card" + (m.active ? " active" : "");
  const tog = document.getElementById(`tog-${m.id}`);
  if (tog) tog.checked = m.active;
  const lbl = document.getElementById(`lbl-${m.id}`);
  if (lbl) { lbl.textContent = m.active ? "On" : "Off"; lbl.className = "tog-lbl" + (m.active ? " on" : ""); }
  const urlDiv = document.getElementById(`url-${m.id}`);
  if (urlDiv) urlDiv.innerHTML = m.active
    ? `<a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.url)} ↗</a>`
    : `<span class="off">localhost:${m.port}</span>`;
  patchStats(m);
}

function patchStats(m) {
  const rv = document.getElementById(`req-${m.id}`);
  const bv = document.getElementById(`byt-${m.id}`);
  if (rv && rv.textContent !== String(m.requests)) {
    rv.textContent = m.requests;
    bump(rv);
  }
  if (bv && bv.textContent !== m.bytes_fmt) {
    bv.textContent = m.bytes_fmt;
    bump(bv);
  }
}

function bump(el) {
  el.classList.remove("bump");
  void el.offsetWidth;
  el.classList.add("bump");
}

/* ══ Status pill ════════════════════════════════════════════════════════════ */
function syncStatus() {
  const all    = Object.values(mods);
  const active = all.filter(m => m.active).length;
  const pill   = document.getElementById("stpill");
  const txt    = document.getElementById("sttext");
  if (active > 0) {
    txt.textContent = `${active} of ${all.length} active`;
    pill.className  = "st-pill live";
  } else {
    txt.textContent = all.length === 0 ? "no modules" : `${all.length} module${all.length > 1 ? "s" : ""}`;
    pill.className  = "st-pill";
  }
}

function syncEmpty() {
  const hasAny = Object.keys(mods).length > 0;
  document.getElementById("empty").classList.toggle("hidden", hasAny);
}

/* ══ Toggle ═════════════════════════════════════════════════════════════════ */
async function onToggle(id, on) {
  const tog = document.getElementById(`tog-${id}`);
  if (tog) tog.disabled = true;
  try {
    const r    = await fetch(`/api/modules/${id}/${on ? "start" : "stop"}`, { method:"POST" });
    const data = await r.json();
    if (!r.ok) {
      toast(data.error || "Action failed", "err");
      if (tog) { tog.checked = !on; tog.disabled = false; }
      return;
    }
    patchCard(data);
    toast(on ? `Serving :${data.port}` : "Server stopped", on ? "ok" : "inf");
  } catch {
    toast("Network error", "err");
    if (tog) { tog.checked = !on; }
  }
  if (tog) tog.disabled = false;
  syncStatus();
}

/* ══ Remove ═════════════════════════════════════════════════════════════════ */
async function doRemove(id) {
  const m = mods[id];
  if (!m) return;
  if (m.active && !confirm(`Stop and remove the server on port ${m.port}?`)) return;
  try {
    const r = await fetch(`/api/modules/${id}`, { method:"DELETE" });
    if (!r.ok) { toast("Could not remove", "err"); return; }
    const card = document.getElementById(cid(id));
    if (card) {
      card.style.transition = "opacity .22s, transform .22s";
      card.style.opacity = "0"; card.style.transform = "scale(.95)";
      setTimeout(() => card.remove(), 220);
    }
    delete mods[id];
    syncStatus(); syncEmpty();
    toast("Module removed", "inf");
  } catch { toast("Network error", "err"); }
}

/* ══ Modal ══════════════════════════════════════════════════════════════════ */
function openModal() {
  document.getElementById("ovl").classList.add("open");
  document.getElementById("modal-err").classList.remove("show");
  document.getElementById("inp-dir").value  = "";
  document.getElementById("inp-port").value = suggestPort();
  setTimeout(() => document.getElementById("inp-dir").focus(), 80);
}
function closeModal() { document.getElementById("ovl").classList.remove("open"); }
function ovlClick(e) { if (e.target.id === "ovl") closeModal(); }

function suggestPort() {
  const used = new Set(Object.values(mods).map(m => m.port));
  for (let p = 8080; p < 8200; p++) if (!used.has(p)) return p;
  return 8080;
}

async function doAdd() {
  const dir  = document.getElementById("inp-dir").value.trim();
  const port = parseInt(document.getElementById("inp-port").value) || 0;
  const errEl = document.getElementById("modal-err");
  const btn   = document.getElementById("btn-confirm");
  errEl.classList.remove("show");
  if (!dir)              { showModalErr("Please enter a directory path."); return; }
  if (port < 1 || port > 65535) { showModalErr("Port must be between 1 and 65535."); return; }
  btn.disabled = true; btn.textContent = "Adding…";
  try {
    const r = await fetch("/api/modules", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ root: dir, port }),
    });
    const data = await r.json();
    if (!r.ok) { showModalErr(data.error || "Failed to add module."); return; }
    closeModal();
    renderMod(data, 0); syncStatus(); syncEmpty();
    toast(`Module added on :${data.port}`, "ok");
  } catch { showModalErr("Network error — is the server running?"); }
  finally { btn.disabled = false; btn.textContent = "Add Module"; }
}

function showModalErr(msg) {
  const el = document.getElementById("modal-err");
  el.textContent = msg; el.classList.add("show");
}

/* ══ Toasts ═════════════════════════════════════════════════════════════════ */
function toast(msg, type = "inf") {
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .25s, transform .25s";
    el.style.opacity = "0"; el.style.transform = "translateX(16px)";
    setTimeout(() => el.remove(), 260);
  }, 3000);
}

/* ══ Keyboard ═══════════════════════════════════════════════════════════════ */
document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeModal();
  if ((e.metaKey || e.ctrlKey) && e.key === "n") { e.preventDefault(); openModal(); }
});
document.getElementById("inp-dir").addEventListener("keydown",
  e => { if (e.key === "Enter") document.getElementById("inp-port").focus(); });
document.getElementById("inp-port").addEventListener("keydown",
  e => { if (e.key === "Enter") doAdd(); });

/* ══ Escape helper ══════════════════════════════════════════════════════════ */
function esc(s) {
  return String(s ?? "")
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

/* ══ Boot ════════════════════════════════════════════════════════════════════ */
window.addEventListener("load", () => {
  setTimeout(connectSSE, 275); // 100ms buffer
  syncEmpty();
});
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    REG.load()
    n = len(REG.all())
    url = f"http://localhost:{MGMT_PORT}"
    print(f"""
  ⬡  WebRack  — directory module server
  ─────────────────────────────────────────
  Open  →  {url}
  {"Loaded " + str(n) + " saved module" + ("s" if n != 1 else "") + "." if n else "No saved modules — add one in the UI."}
  Press Ctrl+C to quit.
""")
    app.run(host="0.0.0.0", port=MGMT_PORT, debug=False, threaded=True)