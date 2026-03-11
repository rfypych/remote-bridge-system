#!/usr/bin/env python3
"""
Bridge Launcher - All-in-One Startup Script
Starts bridge_agent.py + cloudflared tunnel with auto-fallbacks.

Usage:
  python launch.py
  python launch.py --port 8765 --api-key mykey --protocol http2
"""

import subprocess
import sys
import os
import time
import signal
import re
import argparse
import shutil
import threading
import platform

# ==================== CONFIG ====================
BRIDGE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_agent.py")
CLOUDFLARED_PROTOCOLS = ["http2", "h2mux"]  # fallback order
CLOUDFLARED_NAMES = ["cloudflared", "cloudflared.exe"]
CLOUDFLARED_INSTALL_HINTS = {
    "Linux":   "sudo apt install cloudflared  OR  brew install cloudflared",
    "Darwin":  "brew install cloudflared",
    "Windows": "winget install Cloudflare.cloudflared  OR  choco install cloudflared",
}

# ANSI colors
G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; C = "\033[96m"; B = "\033[94m"
W = "\033[97m"; DIM = "\033[2m"; BOLD = "\033[1m"; RESET = "\033[0m"

_procs = []  # track subprocesses for cleanup

def log(tag, msg, color=W):
    ts = time.strftime("%H:%M:%S")
    print(f"  {DIM}[{ts}]{RESET} {color}{tag:<6}{RESET} {msg}")

def banner():
    print(f"\n  {C}{BOLD}BRIDGE LAUNCHER{RESET} {DIM}v1.0{RESET}")
    print(f"  {DIM}{'=' * 44}{RESET}\n")

def cleanup(sig=None, frame=None):
    print(f"\n  {Y}Shutting down...{RESET}")
    for p in _procs:
        try:
            p.terminate()
        except Exception:
            pass
    sys.exit(0)

# ==================== DEPENDENCY CHECKS ====================
def check_python():
    v = sys.version_info
    if v < (3, 7):
        log("ERROR", f"Python 3.7+ required, found {v.major}.{v.minor}", R)
        sys.exit(1)
    log("OK   ", f"Python {v.major}.{v.minor}.{v.micro}", G)

def check_bridge_script():
    if not os.path.exists(BRIDGE_SCRIPT):
        log("ERROR", f"bridge_agent.py not found at: {BRIDGE_SCRIPT}", R)
        log("     ", "Make sure launch.py is in the same folder as bridge_agent.py.", Y)
        sys.exit(1)
    log("OK   ", f"bridge_agent.py found", G)

def find_cloudflared():
    """Find cloudflared binary. Returns path or None."""
    for name in CLOUDFLARED_NAMES:
        path = shutil.which(name)
        if path:
            return path
    
    # Common manual install locations
    extra_paths = [
        "/usr/local/bin/cloudflared",
        "/usr/bin/cloudflared",
        os.path.expanduser("~/.local/bin/cloudflared"),
        r"C:\Program Files\cloudflared\cloudflared.exe",
        r"C:\cloudflared\cloudflared.exe",
    ]
    for p in extra_paths:
        if os.path.isfile(p):
            return p
    return None

def check_cloudflared():
    path = find_cloudflared()
    if path:
        try:
            r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
            ver = r.stdout.strip() or r.stderr.strip()
            log("OK   ", f"cloudflared found: {path} ({ver[:40]})", G)
        except Exception:
            log("OK   ", f"cloudflared found: {path}", G)
        return path
    
    # Not found
    log("WARN ", "cloudflared not found!", Y)
    os_name = platform.system()
    hint = CLOUDFLARED_INSTALL_HINTS.get(os_name, "https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/")
    log("     ", f"Install: {hint}", Y)
    log("     ", "Or download: https://github.com/cloudflare/cloudflared/releases", Y)
    return None

# ==================== BRIDGE AGENT ====================
def start_bridge(port, api_key, no_color, log_file):
    cmd = [sys.executable, "-u", BRIDGE_SCRIPT, "--port", str(port)]
    if api_key:
        cmd += ["--api-key", api_key]
    if no_color:
        cmd += ["--no-color"]
    if log_file:
        cmd += ["--log", log_file]

    log("START", f"bridge_agent.py (port {port})", B)
    proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
    _procs.append(proc)
    time.sleep(1.5)
    if proc.poll() is not None:
        log("ERROR", "bridge_agent.py exited unexpectedly!", R)
        sys.exit(1)
    return proc

# ==================== CLOUDFLARED ====================
def start_cloudflared(cf_path, port, protocol):
    """Try to start cloudflared with given protocol. Returns (proc, url) or (None, None)."""
    cmd = [cf_path, "tunnel", "--url", f"http://localhost:{port}", "--protocol", protocol]
    log("START", f"cloudflared (protocol={protocol})", B)
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    _procs.append(proc)

    url = None
    timeout = 45  # seconds to wait for URL
    start = time.time()

    for line in proc.stdout:
        line = line.strip()
        
        # Print relevant lines
        if any(x in line for x in ["ERR", "INF Registered", "INF Requesting", "trycloudflare.com", "failed to dial"]):
            clean = re.sub(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ', '', line)
            if "trycloudflare.com" in line:
                pass  # handled below
            elif "ERR" in line or "failed" in line:
                log("CF   ", clean, Y)
            else:
                log("CF   ", clean, DIM)

        # Extract tunnel URL
        m = re.search(r'https://[a-z0-9\-]+\.trycloudflare\.com', line)
        if m:
            url = m.group(0)
        
        # Check for successful connection
        if "Registered tunnel connection" in line and "protocol=" + protocol in line:
            return proc, url
        
        # Check for quic/protocol failures
        if "failed to dial" in line and "quic" in line.lower():
            log("CF   ", f"Protocol {protocol} failed, will try fallback...", Y)
            proc.terminate()
            _procs.remove(proc)
            return None, None
        
        # Timeout
        if time.time() - start > timeout:
            log("CF   ", f"Timeout waiting for tunnel ({protocol})", Y)
            proc.terminate()
            _procs.remove(proc)
            return None, None

    return None, None

def launch_cloudflared_with_fallback(cf_path, port):
    """Try protocols in order until one works."""
    for protocol in CLOUDFLARED_PROTOCOLS:
        proc, url = start_cloudflared(cf_path, port, protocol)
        if proc:
            return proc, url, protocol
    
    log("ERROR", "All cloudflared protocols failed!", R)
    log("     ", "Check your network/firewall settings.", Y)
    return None, None, None

# ==================== MONITOR ====================
def monitor_processes(bridge_proc, cf_proc):
    """Watch for unexpected process exits."""
    while True:
        time.sleep(3)
        if bridge_proc.poll() is not None:
            log("WARN ", "bridge_agent.py exited! Shutting down.", R)
            cleanup()
        if cf_proc and cf_proc.poll() is not None:
            log("WARN ", "cloudflared exited unexpectedly.", Y)

# ==================== MAIN ====================
def main():
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    parser = argparse.ArgumentParser(description="Bridge Launcher - starts bridge_agent + cloudflared")
    parser.add_argument("--port", "-p", type=int, default=8765)
    parser.add_argument("--api-key", "-k", type=str, default=None)
    parser.add_argument("--protocol", type=str, default=None,
                        help="Force cloudflared protocol (http2 or h2mux). Default: auto-detect")
    parser.add_argument("--log", "-l", type=str, default=None, help="Session log file")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--no-tunnel", action="store_true", help="Start bridge only, no cloudflared")
    args = parser.parse_args()

    banner()

    # --- Dependency checks ---
    print(f"  {BOLD}Checking dependencies...{RESET}")
    check_python()
    check_bridge_script()
    cf_path = None if args.no_tunnel else check_cloudflared()
    print()

    # --- Start bridge ---
    bridge_proc = start_bridge(args.port, args.api_key, args.no_color, args.log)

    # --- Start cloudflared ---
    cf_proc = None
    tunnel_url = None

    if cf_path:
        protocols = [args.protocol] if args.protocol else None
        if protocols:
            # Force specific protocol
            cf_proc, tunnel_url = start_cloudflared(cf_path, args.port, protocols[0])
        else:
            # Auto fallback
            cf_proc, tunnel_url, used_protocol = launch_cloudflared_with_fallback(cf_path, args.port)

        if tunnel_url:
            print(f"\n  {DIM}{'=' * 44}{RESET}")
            print(f"  {G}{BOLD}TUNNEL READY{RESET}")
            print(f"  {BOLD}URL:{RESET}  {C}{BOLD}{tunnel_url}{RESET}")
            print(f"  {DIM}{'=' * 44}{RESET}\n")
            print(f"  Give this to your AI agent:")
            print(f"  {Y}curl -s {tunnel_url}/{RESET}\n")
        else:
            log("WARN ", "Could not establish tunnel. Bridge is still running locally.", Y)
            print(f"\n  {BOLD}Local URL:{RESET} {C}http://localhost:{args.port}{RESET}\n")
    else:
        if not args.no_tunnel:
            log("INFO ", "Running without tunnel (local only)", Y)
        print(f"\n  {BOLD}Local URL:{RESET} {C}http://localhost:{args.port}{RESET}\n")

    # --- Monitor ---
    print(f"  {DIM}Press Ctrl+C to stop everything{RESET}\n")
    monitor_thread = threading.Thread(target=monitor_processes, args=(bridge_proc, cf_proc), daemon=True)
    monitor_thread.start()

    # Keep alive
    try:
        bridge_proc.wait()
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    main()
