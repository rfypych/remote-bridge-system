#!/usr/bin/env python3
"""
Bridge Agent v5.0 - Multi-Agent Orchestration
Run this on the target machine (e.g., WSL, remote server, Windows)

Features:
- Multi-agent support with agent_id, concurrency limiter, mission workspaces
- Command execution with cwd, env, timeout control
- Background process execution + poll with smart hints
- File: upload, download (chunked), read text, write text
- Filesystem: list, stat, mkdir, delete, move
- Mission system with pre-built agent prompts
- Session logging to .jsonl
- Stats tracking (uptime, request count, bytes)
- Shell auto-detection
- Gzip compression for large responses
- Optional API key authentication
- Beautiful colored output with agent tags
"""

import http.server
import socketserver
import json
import subprocess
import os
import time
import argparse
import signal
import sys
import gzip
import base64
import hashlib
import shutil
import threading
import re
from collections import deque
from io import BytesIO
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import socket
import platform

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ==================== CONFIG ====================
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 60
MAX_OUTPUT_SIZE = 10 * 1024 * 1024   # 10MB
MAX_CONCURRENT_BG = 2                # max simultaneous bg processes
API_KEY = None
START_TIME = time.time()
MISSIONS_DIR = None  # set in main()

# ==================== CONCURRENCY ====================
_bg_semaphore = None  # set in main() after parsing --max-concurrent
_bg_queue = deque()   # track queued commands for position info
_bg_queue_lock = threading.Lock()

def get_queue_position():
    with _bg_queue_lock:
        return len(_bg_queue)

# ==================== AGENT COLORS ====================
AGENT_COLORS = [
    '\033[96m',   # cyan
    '\033[93m',   # yellow
    '\033[92m',   # green
    '\033[95m',   # magenta
    '\033[94m',   # blue
    '\033[91m',   # red
]
_agent_color_map = {}
_agent_color_idx = 0

def agent_tag(agent_id):
    """Return colored agent tag for terminal display."""
    global _agent_color_idx
    if not agent_id:
        return ""
    if agent_id not in _agent_color_map:
        _agent_color_map[agent_id] = AGENT_COLORS[_agent_color_idx % len(AGENT_COLORS)]
        _agent_color_idx += 1
    c = _agent_color_map[agent_id]
    return f"{c}[{agent_id}]{Color.RESET} "

# ==================== STATS ====================
class Stats:
    """Thread-safe global stats tracker."""
    _lock = threading.Lock()
    requests = 0
    commands_run = 0
    bytes_sent = 0
    bytes_received = 0
    errors = 0

    @classmethod
    def inc(cls, **kwargs):
        with cls._lock:
            for k, v in kwargs.items():
                setattr(cls, k, getattr(cls, k) + v)

    @classmethod
    def snapshot(cls):
        with cls._lock:
            return {
                "requests": cls.requests,
                "commands_run": cls.commands_run,
                "bytes_sent": cls.bytes_sent,
                "bytes_received": cls.bytes_received,
                "errors": cls.errors,
                "uptime_seconds": round(time.time() - START_TIME, 1)
            }

# ==================== SESSION LOG ====================
_log_lock = threading.Lock()
LOG_FILE = None  # set in main() via --log

def session_log(entry: dict):
    if not LOG_FILE:
        return
    entry["ts"] = datetime.utcnow().isoformat() + "Z"
    with _log_lock:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

# ==================== BACKGROUND PROCESSES ====================
_bg_lock = threading.Lock()
_bg_processes: dict = {}   # pid (str) -> info dict

def _bg_worker(pid_key: str, command: str, cwd, env):
    """Worker that waits for semaphore slot, then runs the command."""
    run_env = None
    if env:
        run_env = os.environ.copy()
        run_env.update({str(k): str(v) for k, v in env.items()})

    # Mark as queued until we get a slot
    with _bg_lock:
        _bg_processes[pid_key]["status"] = "queued"

    # Wait for semaphore slot (concurrency limiter)
    if _bg_semaphore:
        _bg_semaphore.acquire()

    with _bg_lock:
        if pid_key not in _bg_processes:
            if _bg_semaphore: _bg_semaphore.release()
            return
        _bg_processes[pid_key]["status"] = "running"
        _bg_processes[pid_key]["started_running"] = time.time()

    try:
        proc = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, env=run_env
        )
        with _bg_lock:
            _bg_processes[pid_key]["proc"] = proc
            _bg_processes[pid_key]["real_pid"] = proc.pid

        out, err = proc.communicate()
        with _bg_lock:
            if pid_key in _bg_processes:
                _bg_processes[pid_key]["stdout"] = out
                _bg_processes[pid_key]["stderr"] = err
                _bg_processes[pid_key]["done"] = True
                _bg_processes[pid_key]["status"] = "done"
                _bg_processes[pid_key]["returncode"] = proc.returncode
    except Exception as e:
        with _bg_lock:
            if pid_key in _bg_processes:
                _bg_processes[pid_key]["done"] = True
                _bg_processes[pid_key]["status"] = "error"
                _bg_processes[pid_key]["error"] = str(e)
    finally:
        if _bg_semaphore:
            _bg_semaphore.release()


# ==================== COLORS ====================
class Color:
    BLACK = '\033[30m'; RED = '\033[31m'; GREEN = '\033[32m'
    YELLOW = '\033[33m'; BLUE = '\033[34m'; MAGENTA = '\033[35m'
    CYAN = '\033[36m'; WHITE = '\033[37m'
    BRIGHT_RED = '\033[91m'; BRIGHT_GREEN = '\033[92m'
    BRIGHT_YELLOW = '\033[93m'; BRIGHT_BLUE = '\033[94m'
    BRIGHT_MAGENTA = '\033[95m'; BRIGHT_CYAN = '\033[96m'; BRIGHT_WHITE = '\033[97m'
    BG_RED = '\033[41m'; BG_GREEN = '\033[42m'; BG_YELLOW = '\033[43m'
    BG_BLUE = '\033[44m'; BG_MAGENTA = '\033[45m'; BG_CYAN = '\033[46m'
    BOLD = '\033[1m'; DIM = '\033[2m'; ITALIC = '\033[3m'
    UNDERLINE = '\033[4m'; RESET = '\033[0m'
    TIME = DIM + WHITE
    SUCCESS = BRIGHT_GREEN; ERROR = BRIGHT_RED; WARN = BRIGHT_YELLOW
    INFO = BRIGHT_BLUE; CMD = BRIGHT_MAGENTA
    UPLOAD = BRIGHT_YELLOW; DOWNLOAD = BRIGHT_CYAN

    @staticmethod
    def disable():
        for attr in [a for a in dir(Color) if not a.startswith("_") and not callable(getattr(Color, a))]:
            setattr(Color, attr, '')

    @staticmethod
    def dim(t): return f"{Color.DIM}{t}{Color.RESET}"
    @staticmethod
    def bold(t): return f"{Color.BOLD}{t}{Color.RESET}"
    @staticmethod
    def color(t, c): return f"{c}{t}{Color.RESET}"
    @staticmethod
    def badge(t, c): return f"{Color.BOLD}{c} {t} {Color.RESET}"


# ==================== HELPERS ====================
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"

def detect_shells():
    """Detect available shells on the system."""
    shells = []
    candidates = (
        [("powershell", "powershell -Command echo ok"),
         ("cmd", "cmd /c echo ok"),
         ("pwsh", "pwsh -Command echo ok")]
        if platform.system() == "Windows"
        else [("bash", "bash --version"),
              ("sh", "sh --version"),
              ("zsh", "zsh --version")]
    )
    for name, test_cmd in candidates:
        try:
            r = subprocess.run(test_cmd, shell=True, capture_output=True, timeout=3)
            if r.returncode == 0:
                shells.append(name)
        except Exception:
            pass
    return shells

def format_size(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != 'B' else f"{size}B"
        size /= 1024
    return f"{size:.1f}TB"

def format_duration(s):
    if s < 1: return f"{s*1000:.0f}ms"
    if s < 60: return f"{s:.1f}s"
    return f"{s/60:.1f}m"

def ts(): return time.strftime('%H:%M:%S')


# ==================== BANNER ====================
def print_banner(port: int, auth_enabled: bool):
    ip = get_local_ip()
    auth_s = Color.color("ENABLED", Color.BRIGHT_GREEN) if auth_enabled else Color.color("DISABLED", Color.BRIGHT_RED)
    div = Color.dim('=' * 42)
    print(f"\n  {Color.BRIGHT_CYAN}{Color.BOLD}BRIDGE AGENT{Color.RESET} {Color.dim('v5.0')}")
    print(f"  {div}")
    print(f"  {Color.bold('Status:')}    {Color.badge('ONLINE', Color.BG_GREEN + Color.BLACK)}")
    print(f"  {Color.bold('Address:')}   {Color.BRIGHT_YELLOW}http://{ip}:{port}{Color.RESET}")
    print(f"  {Color.bold('Auth:')}      {auth_s}")
    print(f"  {div}")
    print(f"  {Color.dim('Actions: exec, bg, poll, list, stat, read, write, mkdir, delete, move, upload, download, stats, mission')}")
    print(f"  {div}\n")


# ==================== COMMAND EXECUTOR ====================
class CommandExecutor:
    def execute(self, command, timeout=DEFAULT_TIMEOUT, cwd=None, env=None):
        try:
            run_env = None
            if env:
                run_env = os.environ.copy()
                run_env.update({str(k): str(v) for k, v in env.items()})

            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=cwd, env=run_env
            )
            output = {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
            if cwd:
                output["cwd"] = cwd
            total = len(output["stdout"]) + len(output["stderr"])
            if total > MAX_OUTPUT_SIZE:
                output["stdout"] = output["stdout"][:MAX_OUTPUT_SIZE//2] + "\n... [TRUNCATED]"
                output["stderr"] = output["stderr"][:MAX_OUTPUT_SIZE//4]
                output["truncated"] = True
            return output
        except subprocess.TimeoutExpired:
            return {"error": f"Timeout after {timeout}s", "returncode": -1, "timeout": True}
        except FileNotFoundError:
            return {"error": f"cwd not found: {cwd}", "returncode": -1}
        except Exception as e:
            return {"error": str(e), "returncode": -1}

    def execute_background(self, command, cwd=None, env=None, agent_id=None):
        """Start command in background with concurrency limiting. Returns job_id immediately."""
        import uuid
        job_id = str(uuid.uuid4())[:8]
        with _bg_lock:
            _bg_processes[job_id] = {
                "cmd": command, "proc": None,
                "stdout": "", "stderr": "",
                "done": False, "returncode": None,
                "status": "queued",
                "started": time.time(),
                "started_running": None,
                "agent_id": agent_id,
                "cwd": cwd
            }
        t = threading.Thread(target=_bg_worker, args=(job_id, command, cwd, env), daemon=True)
        t.start()
        return job_id


# ==================== HTTP HANDLER ====================
class BridgeHandler(http.server.BaseHTTPRequestHandler):
    executor = CommandExecutor()
    protocol_version = 'HTTP/1.1'
    _detected_shells = None

    def log_message(self, format, *args):
        pass  # We handle our own logging

    def send_json(self, status: int, data: dict, compress: bool = False):
        try:
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            Stats.inc(bytes_sent=len(body))
            # Only gzip if client accepts it AND response is large enough
            accept_enc = self.headers.get("Accept-Encoding", "") if hasattr(self, 'headers') else ""
            client_accepts_gzip = "gzip" in accept_enc
            if compress and client_accepts_gzip and len(body) > 1024:
                buf = BytesIO()
                with gzip.GzipFile(fileobj=buf, mode='wb') as gz:
                    gz.write(body)
                body = buf.getvalue()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
            else:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            print(f"  {Color.ERROR}SEND ERR{Color.RESET} {e}")

    def check_auth(self):
        if not API_KEY:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            return h[7:] == API_KEY
        return False

    # ---------- GET ----------
    def do_GET(self):
        if not self.check_auth():
            self.send_json(401, {"error": "Unauthorized"}); return

        Stats.inc(requests=1)

        try:
            if BridgeHandler._detected_shells is None:
                BridgeHandler._detected_shells = detect_shells()

            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            params = parse_qs(parsed.query)
            accept = self.headers.get("Accept", "")
            is_browser = "text/html" in accept

            if path == "/mission":
                target = params.get("target", [""])[0]
                self._send_mission_dashboard(target)
            elif path == "/" and is_browser:
                self._send_html_landing()
            elif path == "/":
                print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.SUCCESS}GET{Color.RESET}  {Color.dim('/ - Health Check (JSON)')}")
                self.send_json(200, {
                    "type": "remote-terminal-bridge",
                    "status": "online",
                    "version": "5.0",
                    "timestamp": time.time(),
                    "auth_enabled": API_KEY is not None,
                    "host": {
                        "os": f"{platform.system()} {platform.release()}",
                        "hostname": socket.gethostname(),
                        "cwd": os.getcwd(),
                        "python": sys.version.split()[0],
                        "shells": BridgeHandler._detected_shells
                    },
                    "capabilities": [
                        "exec", "bg", "poll",
                        "list", "stat", "read", "write", "mkdir", "delete", "move",
                        "upload", "download", "stats", "mission"
                    ],
                    "usage": {
                        "exec": {"body": {"action": "exec", "command": "...", "agent_id": "(opt)", "cwd": "(opt)", "env": {}, "timeout": 60}, "description": "Run command synchronously. Returns stdout/stderr/returncode."},
                        "bg":   {"body": {"action": "bg", "command": "...", "agent_id": "(opt)", "cwd": "(opt)"}, "description": "Run command in background. Returns job_id + smart hints (poll_after_seconds, queue status). Use poll to check output."},
                        "poll": {"body": {"action": "poll", "job_id": "<id>", "kill": False}, "description": "Poll background job output. Returns status (queued/running/done), stdout, hint, poll_after_seconds."},
                        "list": {"body": {"action": "list", "path": "."}, "description": "List directory. Returns entries with name/type/size/modified/path."},
                        "stat": {"body": {"action": "stat", "path": "<path>"}, "description": "Get detailed info about a file or directory (size, hash, mtime, etc)."},
                        "read": {"body": {"action": "read", "path": "<path>", "encoding": "utf-8"}, "description": "Read text file content directly (no base64)."},
                        "write": {"body": {"action": "write", "path": "<path>", "content": "...", "mode": "write|append"}, "description": "Write text to file (no base64)."},
                        "mkdir": {"body": {"action": "mkdir", "path": "<path>"}, "description": "Create directory (including parents)."},
                        "delete": {"body": {"action": "delete", "path": "<path>", "recursive": False}, "description": "Delete file or directory."},
                        "move":  {"body": {"action": "move", "src": "<path>", "dst": "<path>"}, "description": "Move or rename file/directory."},
                        "upload":   {"body": {"action": "upload", "filename": "<path>", "data": "<base64>", "mode": "write|append"}, "description": "Upload binary file via base64."},
                        "download": {"body": {"action": "download", "filename": "<path>", "offset": 0, "chunk_size": 1048576}, "description": "Download file as base64 (chunked)."},
                        "stats": {"body": {"action": "stats"}, "description": "Get server stats: uptime, request count, bg queue info."},
                        "mission": {"body": {"action": "mission", "target": "example.com"}, "description": "Create mission workspace with recon/endpoints/vulns/reports/signals/loot subdirectories."}
                    },
                    "hint": "POST to / with JSON body. Add agent_id to tag your requests (multi-agent support)."
                })
            else:
                self.send_json(404, {"error": f"Unknown path: {path}. Try / or /mission?target=example.com"})
        except Exception as e:
            print(f"  {Color.ERROR}GET ERR{Color.RESET} {e}")
            try:
                self.send_json(500, {"error": f"Internal error: {e}"})
            except Exception:
                pass

    def _send_html_landing(self):
        """Return HTML landing page with full bridge instructions."""
        print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.INFO}GET{Color.RESET}  {Color.dim('/ - HTML Landing Page (AI/Browser)')}")
        host_info = f"{socket.gethostname()} &bull; {platform.system()} {platform.release()}"
        cwd = os.getcwd().replace("\\", "\\\\")
        shells = ", ".join(BridgeHandler._detected_shells or ["unknown"])
        host = self.headers.get('Host', 'localhost')
        scheme = 'https' if 'trycloudflare.com' in host else 'http'
        url = f"{scheme}://{host}"
        auth_note = "<b>Auth required:</b> Add header <code>Authorization: Bearer YOUR_KEY</code><br>" if API_KEY else ""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Bridge Agent v5.0</title>
<style>
  body {{ font-family: monospace; background: #0d1117; color: #c9d1d9; margin: 0; padding: 2rem; }}
  h1 {{ color: #58a6ff; margin-bottom: 0; }}
  .sub {{ color: #8b949e; margin-bottom: 2rem; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.2rem 1.5rem; margin-bottom: 1.2rem; }}
  .card h2 {{ color: #3fb950; margin: 0 0 0.8rem 0; font-size: 1rem; }}
  .card h2.warn {{ color: #d29922; }}
  pre {{ background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 1rem; overflow-x: auto; color: #e6edf3; margin: 0.5rem 0; }}
  code {{ color: #ff7b72; }}
  .badge {{ display: inline-block; background: #238636; color: #fff; border-radius: 4px; padding: 2px 8px; font-size: 0.8rem; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; }}
  .action {{ background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 0.6rem 0.8rem; }}
  .action .name {{ color: #58a6ff; font-weight: bold; }}
  .action .desc {{ color: #8b949e; font-size: 0.85rem; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 4px 8px; border-bottom: 1px solid #21262d; }}
  td:first-child {{ color: #8b949e; width: 30%; }}
</style>
</head>
<body>
<h1>&#127757; Bridge Agent <span style="color:#3fb950">v5.0</span></h1>
<p class="sub">Remote Terminal Bridge &bull; <span class="badge">ONLINE</span></p>

<div class="card">
  <h2 class="warn">&#129302; Hey AI! Read this carefully.</h2>
  <p>This is a <b>remote terminal bridge</b> running on the user's local machine.<br>
  You are seeing this in a browser/webpage — but to actually USE this bridge, you need to <b>use curl in your terminal</b>, not browse it.</p>
  {auth_note}
  <b>Quick start — run this in your terminal:</b>
  <pre>curl -s -X POST {url}/ \\
  -H "Content-Type: application/json" \\
  -d '{{"action":"exec","command":"echo hello from bridge"}}'</pre>
  <b>Response will be:</b>
  <pre>{{"stdout": "hello from bridge\\n", "stderr": "", "returncode": 0}}</pre>
</div>

<div class="card">
  <h2>&#128187; Host Info</h2>
  <table>
    <tr><td>Machine</td><td>{host_info}</td></tr>
    <tr><td>Working Dir</td><td>{cwd}</td></tr>
    <tr><td>Shells</td><td>{shells}</td></tr>
    <tr><td>Bridge URL</td><td>{url}/</td></tr>
  </table>
</div>

<div class="card">
  <h2>&#9889; Available Actions</h2>
  <div class="grid">
    <div class="action"><div class="name">exec</div><div class="desc">Run command synchronously (cwd, env, timeout, agent_id)</div></div>
    <div class="action"><div class="name">bg</div><div class="desc">Run in background, returns job_id + poll hints</div></div>
    <div class="action"><div class="name">poll</div><div class="desc">Check bg job output (by job_id)</div></div>
    <div class="action"><div class="name">list</div><div class="desc">List directory contents</div></div>
    <div class="action"><div class="name">stat</div><div class="desc">File info: size, md5, modified date</div></div>
    <div class="action"><div class="name">read</div><div class="desc">Read text file (no base64)</div></div>
    <div class="action"><div class="name">write</div><div class="desc">Write text file (no base64)</div></div>
    <div class="action"><div class="name">mkdir</div><div class="desc">Create directory</div></div>
    <div class="action"><div class="name">delete</div><div class="desc">Delete file or folder</div></div>
    <div class="action"><div class="name">move</div><div class="desc">Move or rename file/folder</div></div>
    <div class="action"><div class="name">upload</div><div class="desc">Upload binary file (base64)</div></div>
    <div class="action"><div class="name">download</div><div class="desc">Download file as base64 (chunked)</div></div>
    <div class="action"><div class="name">stats</div><div class="desc">Server uptime, request count, bg queue</div></div>
    <div class="action"><div class="name">mission</div><div class="desc">Create mission workspace (recon, vulns, signals...)</div></div>
  </div>
</div>

<div class="card">
  <h2>&#128196; Example curl commands</h2>
<pre># Run a command (with agent_id for multi-agent)
curl -s -X POST {url}/ -H "Content-Type: application/json" \\
  -d '{{"action":"exec","command":"whoami","agent_id":"recon"}}'

# Background process + poll (returns job_id)
curl -s -X POST {url}/ -H "Content-Type: application/json" \\
  -d '{{"action":"bg","command":"nmap -sV target.com","agent_id":"recon"}}'
# Response: {{"job_id": "a1b2c3d4", "poll_after_seconds": 10, "hint": "..."}}

curl -s -X POST {url}/ -H "Content-Type: application/json" \\
  -d '{{"action":"poll","job_id":"a1b2c3d4"}}'

# Create mission workspace
curl -s -X POST {url}/ -H "Content-Type: application/json" \\
  -d '{{"action":"mission","target":"example.com"}}'

# Read a file
curl -s -X POST {url}/ -H "Content-Type: application/json" \\
  -d '{{"action":"read","path":"README.md"}}'</pre>
</div>

<div class="card">
  <h2>&#129302; Multi-Agent Mode</h2>
  <p>This bridge supports <b>multiple AI agents</b> working simultaneously. Add <code>agent_id</code> to your requests for tracking.</p>
  <p><b>Quick setup:</b></p>
  <ol>
    <li>Create a mission: <code>{{"action":"mission","target":"example.com"}}</code></li>
    <li>Open the mission dashboard: <a href="{url}/mission?target=example.com">{url}/mission?target=example.com</a></li>
    <li>Copy-paste the ready-made agent prompts into your AI tabs</li>
    <li>Each agent works in its own workspace subdirectory</li>
  </ol>
  <p>Max concurrent background processes: <b>{MAX_CONCURRENT_BG}</b> (configurable via <code>--max-concurrent</code>)</p>
</div>

</body>
</html>"""

        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_content):
        body = html_content.encode('utf-8')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    # ---------- MISSION DASHBOARD ----------
    def _send_mission_dashboard(self, target):
        """Serve mission dashboard with copy-paste agent prompts."""
        # Detect scheme: if via tunnel (trycloudflare), use https
        host = self.headers.get('Host', 'localhost')
        scheme = 'https' if 'trycloudflare.com' in host or 'cloudflare' in host else 'http'
        url = f"{scheme}://{host}"
        auth_header = f' -H "Authorization: Bearer YOUR_KEY"' if API_KEY else ''
        base = MISSIONS_DIR or os.path.join(os.getcwd(), 'missions')
        print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.INFO}GET{Color.RESET}  {Color.dim(f'/mission?target={target} - Dashboard')}")

        if not target:
            # List existing missions with links
            missions = []
            if os.path.exists(base):
                missions = [d for d in sorted(os.listdir(base)) if os.path.isdir(os.path.join(base, d))]
            mission_links = ''.join(f'<li><a href="{url}/mission?target={m}">{m}</a></li>' for m in missions)
            if not mission_links:
                mission_links = '<li>No missions yet. Add <code>?target=example.com</code> to create one.</li>'
            self._send_html(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Mission Control</title>
<style>body{{font-family:monospace;background:#0d1117;color:#c9d1d9;padding:2rem}}
a{{color:#58a6ff}}h1{{color:#58a6ff}}code{{color:#ff7b72}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:1.2rem;margin:1rem 0}}
</style></head><body>
<h1>&#127919; Mission Control</h1>
<div class="card"><h2>Existing Missions</h2><ul>{mission_links}</ul></div>
<div class="card"><h2>Create New Mission</h2>
<p>Visit: <code>{url}/mission?target=YOUR_TARGET</code></p>
<p>Example: <a href="{url}/mission?target=example.com">{url}/mission?target=example.com</a></p>
</div></body></html>""")
            return

        # Create workspace if not exists
        safe_target = re.sub(r'[^a-zA-Z0-9._-]', '_', target)
        mission_path = os.path.join(base, safe_target)
        subdirs = ['recon', 'endpoints', 'vulns', 'reports', 'signals', 'loot']
        for d in subdirs:
            os.makedirs(os.path.join(mission_path, d), exist_ok=True)

        # Build agent prompts with URL and paths baked in
        prompts = {
            'Recon Agent': f"""You are a Reconnaissance specialist. You have access to a remote terminal bridge.

BRIDGE SETUP:
  URL: {url}
  Method: POST JSON to {url}/
  Run commands: {{"action":"exec", "command":"...", "agent_id":"recon"}}
  Background:   {{"action":"bg", "command":"...", "agent_id":"recon"}}
  Poll result:  {{"action":"poll", "job_id":"<id>"}}
  Read file:    {{"action":"read", "path":"..."}}
  Write file:   {{"action":"write", "path":"...", "content":"..."}}

TARGET: {target}
WORKSPACE: {mission_path}/recon/

YOUR TASKS:
1. Subdomain enumeration (subfinder, amass, or manual DNS)
2. Port scanning (nmap)
3. Technology detection (httpx, whatweb)
4. Save all results to {mission_path}/recon/
5. When done, create signal: {{"action":"write", "path":"{mission_path}/signals/recon_done.flag", "content":"done"}}

IMPORTANT: Use "bg" for long-running scans, then "poll" to check results. Do NOT use web search or browse URLs — use curl via the bridge.""",

            'Fuzzer Agent': f"""You are a Fuzzer & Directory Brute-Forcer. You have access to a remote terminal bridge.

BRIDGE SETUP:
  URL: {url}
  Method: POST JSON to {url}/
  Run commands: {{"action":"exec", "command":"...", "agent_id":"fuzzer"}}
  Background:   {{"action":"bg", "command":"...", "agent_id":"fuzzer"}}
  Poll result:  {{"action":"poll", "job_id":"<id>"}}
  Read file:    {{"action":"read", "path":"..."}}
  Write file:   {{"action":"write", "path":"...", "content":"..."}}

TARGET: {target}
WORKSPACE: {mission_path}/endpoints/

BEFORE STARTING:
- Check if recon is done: {{"action":"stat", "path":"{mission_path}/signals/recon_done.flag"}}
- Read recon results: {{"action":"read", "path":"{mission_path}/recon/<filename>"}}

YOUR TASKS:
1. Read recon results from {mission_path}/recon/
2. Directory fuzzing (ffuf, gobuster, or dirsearch)
3. API endpoint discovery
4. JavaScript file analysis
5. Save all found endpoints to {mission_path}/endpoints/
6. When done: {{"action":"write", "path":"{mission_path}/signals/fuzzing_done.flag", "content":"done"}}

IMPORTANT: Use "bg" for long-running scans. Do NOT use web search or browse URLs.""",

            'Vuln Tester': f"""You are a Vulnerability Analyst. You have access to a remote terminal bridge.

BRIDGE SETUP:
  URL: {url}
  Method: POST JSON to {url}/
  Run commands: {{"action":"exec", "command":"...", "agent_id":"vulns"}}
  Background:   {{"action":"bg", "command":"...", "agent_id":"vulns"}}
  Poll result:  {{"action":"poll", "job_id":"<id>"}}
  Read file:    {{"action":"read", "path":"..."}}
  Write file:   {{"action":"write", "path":"...", "content":"..."}}

TARGET: {target}
WORKSPACE: {mission_path}/vulns/

BEFORE STARTING:
- Check if fuzzing is done: {{"action":"stat", "path":"{mission_path}/signals/fuzzing_done.flag"}}
- Read endpoints: {{"action":"list", "path":"{mission_path}/endpoints/"}}

YOUR TASKS:
1. Read discovered endpoints from {mission_path}/endpoints/
2. Test for IDOR, SSRF, XSS, SQLi, auth bypass
3. Write custom Python test scripts as needed
4. For each finding, write a PoC to {mission_path}/vulns/
5. When a vuln is confirmed: {{"action":"write", "path":"{mission_path}/signals/vuln_found.flag", "content":"<vuln_type>"}}

IMPORTANT: Use "bg" for long scans. Write Python scripts for complex tests. Do NOT use web search.""",

            'Report Writer': f"""You are a Bug Bounty Report Writer. You have access to a remote terminal bridge.

BRIDGE SETUP:
  URL: {url}
  Method: POST JSON to {url}/
  Read file:    {{"action":"read", "path":"..."}}
  Write file:   {{"action":"write", "path":"...", "content":"..."}}
  List files:   {{"action":"list", "path":"..."}}

TARGET: {target}
WORKSPACE: {mission_path}/reports/

BEFORE STARTING:
- Check for findings: {{"action":"stat", "path":"{mission_path}/signals/vuln_found.flag"}}
- Read vulns: {{"action":"list", "path":"{mission_path}/vulns/"}}

YOUR TASKS:
1. Monitor {mission_path}/signals/ for vuln_found.flag
2. Read all PoCs from {mission_path}/vulns/
3. Write professional HackerOne-format reports
4. Include: title, severity, description, steps to reproduce, impact, remediation
5. Save reports to {mission_path}/reports/

IMPORTANT: Do NOT use web search. Read files using the bridge."""
        }

        # Build HTML cards for each agent prompt
        cards_html = ''
        colors = ['#58a6ff', '#3fb950', '#d29922', '#f778ba']
        icons = ['&#128269;', '&#128270;', '&#128375;', '&#128221;']
        for i, (name, prompt) in enumerate(prompts.items()):
            escaped_prompt = prompt.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            # Use a unique id for each textarea
            tid = f'prompt_{i}'
            color = colors[i % len(colors)]
            icon = icons[i % len(icons)]
            cards_html += f"""
<div class="card">
  <h2 style="color:{color}">{icon} {name}</h2>
  <textarea id="{tid}" readonly rows="8" style="width:100%;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:0.8rem;font-family:monospace;font-size:0.85rem;resize:vertical">{escaped_prompt}</textarea>
  <button onclick="navigator.clipboard.writeText(document.getElementById('{tid}').value).then(()=>this.textContent='Copied!')" style="margin-top:0.5rem;padding:6px 16px;background:{color};color:#0d1117;border:none;border-radius:4px;cursor:pointer;font-weight:bold">&#128203; Copy Prompt</button>
</div>"""

        self._send_html(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Mission: {target}</title>
<style>
  body {{font-family:monospace;background:#0d1117;color:#c9d1d9;margin:0;padding:2rem}}
  h1 {{color:#58a6ff;margin-bottom:0}}
  .sub {{color:#8b949e;margin-bottom:1.5rem}}
  .card {{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:1.2rem 1.5rem;margin-bottom:1.2rem}}
  .card h2 {{margin:0 0 0.8rem 0;font-size:1rem}}
  .badge {{display:inline-block;background:#238636;color:#fff;border-radius:4px;padding:2px 8px;font-size:0.8rem}}
  a {{color:#58a6ff}}
  code {{color:#ff7b72}}
  .steps {{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:1rem;margin:0.5rem 0}}
  .steps li {{margin:0.3rem 0}}
</style></head><body>
<h1>&#127919; Mission: {target}</h1>
<p class="sub"><span class="badge">ACTIVE</span> &bull; <a href="{url}/">&larr; Back to Bridge</a></p>

<div class="card">
  <h2>&#128640; Quick Setup</h2>
  <ol class="steps">
    <li>Open <b>4 tabs</b> in your AI platform (z.ai, ChatGPT, etc.)</li>
    <li>Copy each agent prompt below and paste into a separate tab</li>
    <li>Each agent will work autonomously in its own workspace</li>
    <li>Agents coordinate via signal files in <code>{mission_path}/signals/</code></li>
  </ol>
  <p>Workspace: <code>{mission_path}</code></p>
</div>

{cards_html}

<div class="card">
  <h2>&#128206; Workspace Structure</h2>
  <pre style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:1rem;color:#e6edf3">{mission_path}/
  recon/        ← Agent 1: subdomain, port scan results
  endpoints/    ← Agent 2: discovered URLs, APIs
  vulns/        ← Agent 3: vulnerability PoCs
  reports/      ← Agent 4: final bug bounty reports
  signals/      ← Coordination flags between agents
  loot/         ← Additional findings</pre>
</div>

</body></html>""")

    # ---------- POST ----------
    def do_POST(self):
        if not self.check_auth():
            self.send_json(401, {"error": "Unauthorized"}); return

        Stats.inc(requests=1)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                self.send_json(400, {"error": "Empty request"}); return
            raw = self.rfile.read(length)
            Stats.inc(bytes_received=length)
            data = json.loads(raw.decode('utf-8'))
            action = data.get("action", "exec")

            dispatch = {
                "exec":     self._handle_exec,
                "bg":       self._handle_bg,
                "poll":     self._handle_poll,
                "list":     self._handle_list,
                "stat":     self._handle_stat,
                "read":     self._handle_read,
                "write":    self._handle_write,
                "mkdir":    self._handle_mkdir,
                "delete":   self._handle_delete,
                "move":     self._handle_move,
                "upload":   self._handle_upload,
                "download": self._handle_download,
                "stats":    self._handle_stats,
                "mission":  self._handle_mission,
            }
            handler = dispatch.get(action)
            if handler:
                handler(data)
            else:
                valid = ", ".join(dispatch.keys())
                self.send_json(400, {"error": f"Unknown action: '{action}'. Valid: {valid}"})

        except json.JSONDecodeError as e:
            Stats.inc(errors=1)
            self.send_json(400, {"error": f"Invalid JSON: {e}"})
        except Exception as e:
            Stats.inc(errors=1)
            self.send_json(500, {"error": str(e)})

    # ---------- EXEC ----------
    def _handle_exec(self, data):
        cmd = data.get("command", "")
        aid = data.get("agent_id", "")
        if not cmd:
            self.send_json(400, {"error": "No command"}); return
        timeout = min(data.get("timeout", DEFAULT_TIMEOUT), 300)
        cwd = data.get("cwd") or None
        env = data.get("env") or None
        cwd_hint = f" {Color.dim('@ ' + cwd)}" if cwd else ""
        print(f"  {Color.TIME}[{ts()}]{Color.RESET} {agent_tag(aid)}{Color.CMD}EXEC{Color.RESET} {Color.bold(cmd)}{cwd_hint}")
        t0 = time.time()
        result = self.executor.execute(cmd, timeout, cwd=cwd, env=env)
        elapsed = time.time() - t0
        Stats.inc(commands_run=1)
        out_size = len(result.get("stdout", "")) + len(result.get("stderr", ""))
        rc = result.get("returncode")
        status = (Color.color("DONE", Color.BRIGHT_GREEN) if rc == 0
                  else Color.color("TIMEOUT", Color.BRIGHT_YELLOW) if result.get("timeout")
                  else Color.color(f"FAIL:{rc}", Color.BRIGHT_RED))
        print(f"           {status} {Color.dim(format_size(out_size))} {Color.dim('|')} {Color.dim(format_duration(elapsed))}")
        session_log({"action": "exec", "cmd": cmd, "cwd": cwd, "rc": rc, "elapsed": round(elapsed, 3), "agent_id": aid})
        self.send_json(200, result, compress=True)

    # ---------- BACKGROUND ----------
    def _handle_bg(self, data):
        cmd = data.get("command", "")
        aid = data.get("agent_id", "")
        if not cmd:
            self.send_json(400, {"error": "No command"}); return
        cwd = data.get("cwd") or None
        env = data.get("env") or None
        cwd_hint = f" {Color.dim('@ ' + cwd)}" if cwd else ""
        print(f"  {Color.TIME}[{ts()}]{Color.RESET} {agent_tag(aid)}{Color.WARN}BG  {Color.RESET} {Color.bold(cmd)}{cwd_hint}")
        try:
            job_id = self.executor.execute_background(cmd, cwd=cwd, env=env, agent_id=aid)
            Stats.inc(commands_run=1)
            # Count running bg processes for hint
            with _bg_lock:
                running = sum(1 for p in _bg_processes.values() if p.get('status') == 'running')
                queued = sum(1 for p in _bg_processes.values() if p.get('status') == 'queued')
            hint = f"This command is running in the background. Poll with action='poll', job_id='{job_id}' to check output."
            if queued > 0:
                hint += f" Currently {running} processes running, {queued} queued (max concurrent: {MAX_CONCURRENT_BG}). Your command may wait before starting."
            hint += f" Suggested: poll again in 10-30 seconds."
            session_log({"action": "bg", "cmd": cmd, "job_id": job_id, "agent_id": aid})
            self.send_json(200, {
                "job_id": job_id,
                "status": "queued",
                "command": cmd,
                "running_count": running,
                "queued_count": queued,
                "max_concurrent": MAX_CONCURRENT_BG,
                "poll_after_seconds": 10,
                "hint": hint
            })
        except Exception as e:
            Stats.inc(errors=1)
            self.send_json(500, {"error": str(e)})

    # ---------- POLL ----------
    def _handle_poll(self, data):
        job_id = str(data.get("job_id", data.get("pid", "")))
        kill = data.get("kill", False)
        aid = data.get("agent_id", "")
        if not job_id:
            self.send_json(400, {"error": "No job_id (or pid)"}); return
        with _bg_lock:
            proc_info = _bg_processes.get(job_id)
        if not proc_info:
            self.send_json(404, {"error": f"No background job '{job_id}'. Use 'bg' action to start one."}); return
        if kill and not proc_info.get("done") and proc_info.get("proc"):
            try:
                proc_info["proc"].terminate()
                print(f"  {Color.TIME}[{ts()}]{Color.RESET} {agent_tag(aid)}{Color.WARN}KILL{Color.RESET} job={job_id}")
            except Exception:
                pass
        status = proc_info.get("status", "unknown")
        done = proc_info.get("done", False)
        elapsed = round(time.time() - proc_info.get("started", time.time()), 2)
        print(f"  {Color.TIME}[{ts()}]{Color.RESET} {agent_tag(aid)}{Color.INFO}POLL{Color.RESET} job={job_id} {status}")
        # Smart hint
        if done:
            hint = "Command finished. You can read the stdout/stderr from this response."
        elif status == "queued":
            with _bg_lock:
                running = sum(1 for p in _bg_processes.values() if p.get('status') == 'running')
            hint = f"Command is queued ({running}/{MAX_CONCURRENT_BG} slots busy). Poll again in 15-30 seconds."
        else:
            hint = f"Command is running ({elapsed}s elapsed). Poll again in 10-20 seconds to check progress."
        self.send_json(200, {
            "job_id": job_id,
            "command": proc_info.get("cmd"),
            "status": status,
            "stdout": proc_info.get("stdout", ""),
            "stderr": proc_info.get("stderr", ""),
            "done": done,
            "returncode": proc_info.get("returncode"),
            "elapsed": elapsed,
            "poll_after_seconds": 0 if done else (20 if status == 'queued' else 10),
            "hint": hint
        }, compress=True)

    # ---------- LIST ----------
    def _handle_list(self, data):
        path = data.get("path", ".")
        abs_path = os.path.abspath(path)
        try:
            if not os.path.exists(abs_path):
                self.send_json(404, {"error": f"Path not found: {abs_path}"}); return
            entries = []
            if os.path.isfile(abs_path):
                s = os.stat(abs_path)
                entries.append({"name": os.path.basename(abs_path), "type": "file",
                                 "size": s.st_size, "modified": s.st_mtime, "path": abs_path})
            else:
                for name in sorted(os.listdir(abs_path)):
                    full = os.path.join(abs_path, name)
                    try:
                        s = os.stat(full)
                        entries.append({"name": name,
                                        "type": "dir" if os.path.isdir(full) else "file",
                                        "size": s.st_size if os.path.isfile(full) else None,
                                        "modified": s.st_mtime, "path": full})
                    except PermissionError:
                        entries.append({"name": name, "type": "unknown", "error": "permission denied"})

            print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.INFO}LIST{Color.RESET} {Color.bold(abs_path)} {Color.dim(f'({len(entries)} entries)')}")
            self.send_json(200, {"path": abs_path, "entries": entries, "count": len(entries)})
        except PermissionError:
            self.send_json(403, {"error": "Permission denied"})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    # ---------- STAT ----------
    def _handle_stat(self, data):
        path = data.get("path", "")
        if not path:
            self.send_json(400, {"error": "No path"}); return
        abs_path = os.path.abspath(path)
        try:
            if not os.path.exists(abs_path):
                self.send_json(404, {"error": f"Not found: {abs_path}"}); return
            s = os.stat(abs_path)
            result = {
                "path": abs_path,
                "exists": True,
                "type": "dir" if os.path.isdir(abs_path) else "file",
                "size": s.st_size,
                "size_human": format_size(s.st_size),
                "modified": s.st_mtime,
                "modified_iso": datetime.utcfromtimestamp(s.st_mtime).isoformat() + "Z",
                "created": getattr(s, 'st_birthtime', s.st_ctime),
            }
            # Compute MD5 for files under 50MB
            if os.path.isfile(abs_path) and s.st_size < 50 * 1024 * 1024:
                h = hashlib.md5()
                with open(abs_path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                result["md5"] = h.hexdigest()
            print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.INFO}STAT{Color.RESET} {Color.bold(abs_path)}")
            self.send_json(200, result)
        except PermissionError:
            self.send_json(403, {"error": "Permission denied"})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    # ---------- READ TEXT ----------
    def _handle_read(self, data):
        path = data.get("path", "")
        if not path:
            self.send_json(400, {"error": "No path"}); return
        encoding = data.get("encoding", "utf-8")
        abs_path = os.path.abspath(path)
        try:
            if not os.path.exists(abs_path):
                self.send_json(404, {"error": f"Not found: {abs_path}"}); return
            size = os.path.getsize(abs_path)
            if size > MAX_OUTPUT_SIZE:
                self.send_json(413, {"error": f"File too large ({format_size(size)}). Use 'download' for binary/large files."}); return
            with open(abs_path, "r", encoding=encoding, errors="replace") as f:
                content = f.read()
            print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.DOWNLOAD}READ{Color.RESET} {Color.bold(abs_path)} {Color.dim(format_size(size))}")
            self.send_json(200, {"path": abs_path, "content": content, "size": size, "encoding": encoding}, compress=True)
        except UnicodeDecodeError:
            self.send_json(400, {"error": "Cannot decode as text. Use 'download' for binary files."})
        except PermissionError:
            self.send_json(403, {"error": "Permission denied"})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    # ---------- WRITE TEXT ----------
    def _handle_write(self, data):
        path = data.get("path", "")
        content = data.get("content", "")
        mode = data.get("mode", "write")
        encoding = data.get("encoding", "utf-8")
        if not path:
            self.send_json(400, {"error": "No path"}); return
        abs_path = os.path.abspath(path)
        try:
            dir_path = os.path.dirname(abs_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            write_mode = "a" if mode == "append" else "w"
            with open(abs_path, write_mode, encoding=encoding) as f:
                f.write(content)
            size = os.path.getsize(abs_path)
            print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.UPLOAD}WRIT{Color.RESET} {Color.bold(abs_path)} {Color.dim(format_size(size))}")
            self.send_json(200, {"success": True, "path": abs_path, "size": size})
        except PermissionError:
            self.send_json(403, {"error": "Permission denied"})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    # ---------- MKDIR ----------
    def _handle_mkdir(self, data):
        path = data.get("path", "")
        if not path:
            self.send_json(400, {"error": "No path"}); return
        abs_path = os.path.abspath(path)
        try:
            os.makedirs(abs_path, exist_ok=True)
            print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.INFO}MKDR{Color.RESET} {Color.bold(abs_path)}")
            self.send_json(200, {"success": True, "path": abs_path})
        except PermissionError:
            self.send_json(403, {"error": "Permission denied"})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    # ---------- DELETE ----------
    def _handle_delete(self, data):
        path = data.get("path", "")
        recursive = data.get("recursive", False)
        if not path:
            self.send_json(400, {"error": "No path"}); return
        abs_path = os.path.abspath(path)
        try:
            if not os.path.exists(abs_path):
                self.send_json(404, {"error": f"Not found: {abs_path}"}); return
            if os.path.isdir(abs_path):
                if recursive:
                    shutil.rmtree(abs_path)
                else:
                    os.rmdir(abs_path)  # will fail if not empty — intentional safety
            else:
                os.remove(abs_path)
            print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.ERROR}DEL {Color.RESET} {Color.bold(abs_path)}")
            self.send_json(200, {"success": True, "path": abs_path})
        except OSError as e:
            self.send_json(400, {"error": str(e) + " (use recursive=true for non-empty dirs)"})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    # ---------- MOVE ----------
    def _handle_move(self, data):
        src = data.get("src", "")
        dst = data.get("dst", "")
        if not src or not dst:
            self.send_json(400, {"error": "Both 'src' and 'dst' required"}); return
        abs_src = os.path.abspath(src)
        abs_dst = os.path.abspath(dst)
        try:
            if not os.path.exists(abs_src):
                self.send_json(404, {"error": f"Not found: {abs_src}"}); return
            shutil.move(abs_src, abs_dst)
            print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.INFO}MOVE{Color.RESET} {Color.bold(abs_src)} {Color.dim('->')} {Color.bold(abs_dst)}")
            self.send_json(200, {"success": True, "src": abs_src, "dst": abs_dst})
        except PermissionError:
            self.send_json(403, {"error": "Permission denied"})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    # ---------- UPLOAD BINARY ----------
    def _handle_upload(self, data):
        filename = data.get("filename", "uploaded_file")
        filedata = data.get("data", "")
        mode = data.get("mode", "write")
        try:
            content = base64.b64decode(filedata)
            write_mode = "ab" if mode == "append" else "wb"
            dir_path = os.path.dirname(filename)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            with open(filename, write_mode) as f:
                f.write(content)
            print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.UPLOAD} UP {Color.RESET} {Color.bold(filename)} {Color.dim(format_size(len(content)))}")
            self.send_json(200, {"success": True, "filename": filename, "size": len(content)})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    # ---------- DOWNLOAD BINARY ----------
    def _handle_download(self, data):
        filename = data.get("filename", "")
        chunk_size = data.get("chunk_size", 1024 * 1024)
        offset = data.get("offset", 0)
        if not filename:
            self.send_json(400, {"error": "No filename"}); return
        try:
            if not os.path.exists(filename):
                self.send_json(404, {"error": "File not found"}); return
            file_size = os.path.getsize(filename)
            with open(filename, "rb") as f:
                f.seek(offset)
                chunk = f.read(chunk_size)
                actual_offset = f.tell()
            progress = f"{format_size(actual_offset)}/{format_size(file_size)}"
            print(f"  {Color.TIME}[{ts()}]{Color.RESET} {Color.DOWNLOAD}DOWN{Color.RESET} {Color.bold(filename)} {Color.dim(progress)}")
            self.send_json(200, {
                "success": True, "filename": filename,
                "data": base64.b64encode(chunk).decode('utf-8'),
                "offset": actual_offset, "total_size": file_size,
                "done": actual_offset >= file_size
            })
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    # ---------- STATS ----------
    def _handle_stats(self, data):
        aid = data.get("agent_id", "") if isinstance(data, dict) else ""
        snap = Stats.snapshot()
        # add bg process info
        with _bg_lock:
            snap["bg_running"] = sum(1 for p in _bg_processes.values() if p.get('status') == 'running')
            snap["bg_queued"] = sum(1 for p in _bg_processes.values() if p.get('status') == 'queued')
            snap["max_concurrent"] = MAX_CONCURRENT_BG
        print(f"  {Color.TIME}[{ts()}]{Color.RESET} {agent_tag(aid)}{Color.INFO}STAT{Color.RESET} {Color.dim('server stats')}")
        self.send_json(200, snap)

    # ---------- MISSION ----------
    def _handle_mission(self, data):
        """Create or list mission workspaces."""
        target = data.get("target", "")
        aid = data.get("agent_id", "")
        if not target:
            # List existing missions
            if MISSIONS_DIR and os.path.exists(MISSIONS_DIR):
                missions = [d for d in os.listdir(MISSIONS_DIR) if os.path.isdir(os.path.join(MISSIONS_DIR, d))]
            else:
                missions = []
            self.send_json(200, {"missions": missions, "missions_dir": MISSIONS_DIR or "not configured"})
            return

        # Sanitize target name for folder
        safe_target = re.sub(r'[^a-zA-Z0-9._-]', '_', target)
        base = MISSIONS_DIR or os.path.join(os.getcwd(), "missions")
        mission_path = os.path.join(base, safe_target)

        # Create workspace structure
        subdirs = ["recon", "endpoints", "vulns", "reports", "signals", "loot"]
        for d in subdirs:
            os.makedirs(os.path.join(mission_path, d), exist_ok=True)

        workspace = {d: os.path.join(mission_path, d) for d in subdirs}
        workspace["root"] = mission_path

        print(f"  {Color.TIME}[{ts()}]{Color.RESET} {agent_tag(aid)}{Color.SUCCESS}MISSION{Color.RESET} {Color.bold(target)} -> {mission_path}")
        self.send_json(200, {
            "target": target,
            "workspace": workspace,
            "hint": f"Workspace created at {mission_path}. Use cwd parameter to run commands inside the workspace. Drop signal files in 'signals/' to coordinate with other agents."
        })

    # ---------- OPTIONS ----------
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()


# ==================== SERVER ====================
class ReuseAddrServer(socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def signal_handler(signum, frame):
    print(f"\n  {Color.WARN}Shutting down bridge...{Color.RESET}\n")
    sys.exit(0)


def main():
    global API_KEY, LOG_FILE, MISSIONS_DIR, MAX_CONCURRENT_BG, _bg_semaphore

    if not sys.stdout.isatty():
        Color.disable()

    parser = argparse.ArgumentParser(description="Bridge Agent v5.0 - Multi-Agent Orchestration")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT)
    parser.add_argument("--api-key", "-k", type=str, default=None)
    parser.add_argument("--log", "-l", type=str, default=None, help="Session log file (.jsonl)")
    parser.add_argument("--max-concurrent", "-c", type=int, default=MAX_CONCURRENT_BG,
                        help=f"Max concurrent bg processes (default: {MAX_CONCURRENT_BG})")
    parser.add_argument("--missions-dir", type=str, default=None, help="Base directory for mission workspaces")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color:
        Color.disable()

    API_KEY = args.api_key or os.environ.get("BRIDGE_API_KEY")
    LOG_FILE = args.log or os.environ.get("BRIDGE_LOG_FILE")
    MAX_CONCURRENT_BG = args.max_concurrent
    _bg_semaphore = threading.Semaphore(MAX_CONCURRENT_BG)
    MISSIONS_DIR = args.missions_dir or os.environ.get("BRIDGE_MISSIONS_DIR") or os.path.join(os.getcwd(), "missions")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print_banner(args.port, API_KEY is not None)

    div = Color.dim('=' * 42)
    print(f"  {Color.bold('OS:')}        {platform.system()} {platform.release()}")
    print(f"  {Color.bold('Python:')}    {sys.version.split()[0]}")
    print(f"  {Color.bold('Directory:')} {Color.dim(os.getcwd())}")
    if LOG_FILE:
        print(f"  {Color.bold('Log:')}       {Color.dim(LOG_FILE)}")
    print(f"  {div}\n")

    port = args.port
    for attempt in range(10):
        try:
            server = ReuseAddrServer(("", port), BridgeHandler)
            if port != args.port:
                print(f"  {Color.WARN}NOTE{Color.RESET}  "
                      f"Port {args.port} was busy, using {Color.BRIGHT_YELLOW}{port}{Color.RESET} instead")
            print(f"  {Color.SUCCESS}READY{Color.RESET} {Color.dim('Listening for connections...')}")
            print(f"  {Color.dim('Press Ctrl+C to stop')}\n")
            server.serve_forever()
            break
        except OSError as e:
            if "Address already in use" in str(e) or "10048" in str(e):
                print(f"  {Color.WARN}BUSY {Color.RESET} Port {port} is in use, trying {port + 1}...")
                port += 1
                if attempt == 9:
                    print(f"  Port {args.port}-{port}: no free port found.")
            else:
                raise
        except KeyboardInterrupt:
            print(f"\n  {Color.WARN}Stopped.{Color.RESET}")
            break


if __name__ == "__main__":
    main()
