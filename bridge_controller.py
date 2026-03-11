#!/usr/bin/env python3
"""
Bridge Controller v3.2 - Beautiful & Stable
Control remote bridge agent with retry logic and file transfer support.

Features:
- Auto-retry on connection failures
- Compression support
- Chunked file transfer
- Progress indicators
- Interactive REPL with history
- Beautiful colored output
"""

import requests
import json
import argparse
import sys
import time
import os
import base64
from typing import Optional, Dict, Any
from pathlib import Path

# Defaults
DEFAULT_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_DELAY = 2
CHUNK_SIZE = 1024 * 512  # 512KB chunks


# ==================== COLORS ====================
class Color:
    """ANSI Color codes."""
    BLACK = '\033[30m'
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    BLUE = '\033[34m'
    MAGENTA = '\033[35m'
    CYAN = '\033[36m'
    WHITE = '\033[37m'
    
    BRIGHT_RED = '\033[91m'
    BRIGHT_GREEN = '\033[92m'
    BRIGHT_YELLOW = '\033[93m'
    BRIGHT_BLUE = '\033[94m'
    BRIGHT_MAGENTA = '\033[95m'
    BRIGHT_CYAN = '\033[96m'
    BRIGHT_WHITE = '\033[97m'
    
    BG_RED = '\033[41m'
    BG_GREEN = '\033[42m'
    BG_BLUE = '\033[44m'
    
    BOLD = '\033[1m'
    DIM = '\033[2m'
    ITALIC = '\033[3m'
    UNDERLINE = '\033[4m'
    
    RESET = '\033[0m'
    
    # Shortcuts
    SUCCESS = BRIGHT_GREEN
    ERROR = BRIGHT_RED
    WARN = BRIGHT_YELLOW
    INFO = BRIGHT_BLUE
    CMD = BRIGHT_MAGENTA
    
    @staticmethod
    def disable():
        """Disable colors."""
        for attr in dir(Color):
            if not attr.startswith('_') and attr.isupper():
                setattr(Color, attr, '')


def format_size(size: int) -> str:
    """Format bytes to human readable."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != 'B' else f"{size}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def format_duration(seconds: float) -> str:
    """Format duration."""
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    elif seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds/60:.1f}m"


class BridgeController:
    """Controller with retry logic and file transfer support."""
    
    def __init__(self, base_url: str, timeout: int = DEFAULT_TIMEOUT, 
                 api_key: Optional[str] = None, max_retries: int = MAX_RETRIES):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.max_retries = max_retries
        
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip"
        })
        
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"
    
    def _request(self, payload: dict, timeout: Optional[int] = None) -> dict:
        """Make request with retry logic."""
        last_error = None
        
        for attempt in range(self.max_retries):
            try:
                response = self.session.post(
                    f"{self.base_url}/",
                    json=payload,
                    timeout=(timeout or self.timeout) + 30
                )
                
                if response.status_code == 401:
                    return {"error": "Unauthorized - check API key", "returncode": -1}
                
                if response.status_code != 200:
                    return {"error": f"HTTP {response.status_code}", "returncode": -1}
                
                return response.json()
                
            except requests.exceptions.Timeout:
                last_error = f"Timeout after {timeout or self.timeout}s"
            except requests.exceptions.ConnectionError:
                last_error = "Connection failed"
                if attempt < self.max_retries - 1:
                    print(f"{Color.DIM}  Retry {attempt + 2}/{self.max_retries}...{Color.RESET}")
                    time.sleep(RETRY_DELAY)
            except json.JSONDecodeError:
                last_error = "Invalid JSON response"
            except Exception as e:
                last_error = str(e)
        
        return {"error": last_error, "returncode": -1}
    
    def check_health(self) -> bool:
        """Check bridge status with retry."""
        try:
            for attempt in range(self.max_retries):
                try:
                    r = self.session.get(f"{self.base_url}/", timeout=5)
                    if r.status_code == 200:
                        data = r.json()
                        version = data.get('version', '?')
                        auth = f"{Color.SUCCESS}auth enabled{Color.RESET}" if data.get('auth_enabled') else f"{Color.DIM}no auth{Color.RESET}"
                        print(f"{Color.SUCCESS}✓{Color.RESET} Bridge online {Color.DIM}(v{version}){Color.RESET} [{auth}]")
                        return True
                except:
                    if attempt < self.max_retries - 1:
                        time.sleep(1)
            print(f"{Color.ERROR}✗{Color.RESET} Bridge unreachable")
            return False
        except Exception as e:
            print(f"{Color.ERROR}✗{Color.RESET} Health check failed: {e}")
            return False
    
    def execute(self, command: str, timeout: Optional[int] = None) -> dict:
        """Execute command on remote bridge."""
        return self._request({
            "action": "exec",
            "command": command,
            "timeout": timeout or self.timeout
        }, timeout)
    
    def upload_file(self, local_path: str, remote_path: str, 
                    show_progress: bool = True) -> dict:
        """Upload file with chunked transfer."""
        try:
            file_size = os.path.getsize(local_path)
            filename = os.path.basename(local_path)
            remote_path = remote_path or filename
            
            if show_progress:
                print(f"{Color.INFO}▲{Color.RESET} Uploading {Color.BRIGHT_CYAN}{local_path}{Color.RESET}")
                print(f"  {Color.DIM}→ {remote_path} ({format_size(file_size)}){Color.RESET}")
            
            offset = 0
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    
                    result = self._request({
                        "action": "upload",
                        "filename": remote_path,
                        "data": base64.b64encode(chunk).decode('utf-8'),
                        "mode": "append" if offset > 0 else "write"
                    })
                    
                    if "error" in result and "returncode" in result:
                        return result
                    
                    offset += len(chunk)
                    
                    if show_progress:
                        pct = (offset / file_size) * 100
                        bar = f"{Color.SUCCESS}█{Color.RESET}" * int(pct / 5) + f"{Color.DIM}░{Color.RESET}" * (20 - int(pct / 5))
                        print(f"\r  [{bar}] {pct:.0f}%", end="", flush=True)
            
            if show_progress:
                print(f"\n{Color.SUCCESS}✓{Color.RESET} Upload complete!")
            
            return {"success": True, "filename": remote_path, "size": offset}
            
        except FileNotFoundError:
            return {"error": f"File not found: {local_path}", "returncode": -1}
        except Exception as e:
            return {"error": str(e), "returncode": -1}
    
    def download_file(self, remote_path: str, local_path: str,
                      show_progress: bool = True) -> dict:
        """Download file with chunked transfer."""
        try:
            if show_progress:
                print(f"{Color.INFO}▼{Color.RESET} Downloading {Color.BRIGHT_CYAN}{remote_path}{Color.RESET}")
            
            offset = 0
            total_size = 0
            first_chunk = True
            
            with open(local_path, "wb") as f:
                while True:
                    result = self._request({
                        "action": "download",
                        "filename": remote_path,
                        "offset": offset,
                        "chunk_size": CHUNK_SIZE
                    })
                    
                    if "error" in result:
                        return result
                    
                    if first_chunk:
                        total_size = result.get("total_size", 0)
                        if show_progress:
                            print(f"  {Color.DIM}→ {local_path} ({format_size(total_size)}){Color.RESET}")
                        first_chunk = False
                    
                    chunk_data = base64.b64decode(result["data"])
                    f.write(chunk_data)
                    offset = result.get("offset", offset + len(chunk_data))
                    
                    if show_progress and total_size > 0:
                        pct = (offset / total_size) * 100
                        bar = f"{Color.SUCCESS}█{Color.RESET}" * int(pct / 5) + f"{Color.DIM}░{Color.RESET}" * (20 - int(pct / 5))
                        print(f"\r  [{bar}] {pct:.0f}%", end="", flush=True)
                    
                    if result.get("done", False):
                        break
            
            if show_progress:
                print(f"\n{Color.SUCCESS}✓{Color.RESET} Download complete!")
            
            return {"success": True, "filename": local_path, "size": offset}
            
        except Exception as e:
            return {"error": str(e), "returncode": -1}
    
    def print_result(self, result: dict):
        """Pretty print execution result."""
        if "error" in result:
            print(f"{Color.ERROR}✗ {result['error']}{Color.RESET}")
        
        if result.get("stdout"):
            print(result["stdout"], end='' if result["stdout"].endswith('\n') else '\n')
        
        if result.get("stderr"):
            print(f"{Color.WARN}[stderr]{Color.RESET} {result['stderr']}", end='')
        
        if result.get("returncode", 0) not in [0, None]:
            print(f"{Color.DIM}[exit: {result.get('returncode', '?')}]{Color.RESET}")
        
        if result.get("truncated"):
            print(f"{Color.WARN}! Output truncated{Color.RESET}")


def interactive_mode(ctrl: BridgeController):
    """Interactive REPL with command history."""
    history = []
    
    print(f"""
{Color.BRIGHT_CYAN}╔═══════════════════════════════════════════════════════════════╗
║{Color.BRIGHT_WHITE}           🎮 BRIDGE CONTROLLER - Interactive Mode{Color.BRIGHT_CYAN}             ║
╠═══════════════════════════════════════════════════════════════╣{Color.RESET}
{Color.BRIGHT_CYAN}║{Color.RESET}  Commands:                                                     {Color.BRIGHT_CYAN}║{Color.RESET}
{Color.BRIGHT_CYAN}║{Color.RESET}    {Color.SUCCESS}!health{Color.RESET}              Check bridge status              {Color.BRIGHT_CYAN}║{Color.RESET}
{Color.BRIGHT_CYAN}║{Color.RESET}    {Color.INFO}!upload <file>{Color.RESET}       Upload file                      {Color.BRIGHT_CYAN}║{Color.RESET}
{Color.BRIGHT_CYAN}║{Color.RESET}    {Color.INFO}!download <file>{Color.RESET}     Download file                    {Color.BRIGHT_CYAN}║{Color.RESET}
{Color.BRIGHT_CYAN}║{Color.RESET}    {Color.DIM}!history{Color.RESET}             Show command history             {Color.BRIGHT_CYAN}║{Color.RESET}
{Color.BRIGHT_CYAN}║{Color.RESET}    {Color.WARN}!exit{Color.RESET}               Quit                             {Color.BRIGHT_CYAN}║{Color.RESET}
{Color.BRIGHT_CYAN}╚═══════════════════════════════════════════════════════════════╝{Color.RESET}
""")
    
    while True:
        try:
            cmd = input(f"{Color.BRIGHT_MAGENTA}bridge{Color.RESET}{Color.DIM}❯{Color.RESET} ").strip()
            
            if not cmd:
                continue
            
            if cmd not in ["!history", "!exit"]:
                history.append(cmd)
                if len(history) > 100:
                    history.pop(0)
            
            if cmd == "!exit":
                print(f"{Color.DIM}👋 Bye!{Color.RESET}")
                break
            elif cmd == "!health":
                ctrl.check_health()
            elif cmd == "!history":
                for i, h in enumerate(history[-20:], 1):
                    print(f"  {Color.DIM}{i:2}.{Color.RESET} {h[:60]}{'...' if len(h)>60 else ''}")
            elif cmd.startswith("!upload "):
                parts = cmd.split(maxsplit=2)
                local = parts[1]
                remote = parts[2] if len(parts) > 2 else os.path.basename(local)
                result = ctrl.upload_file(local, remote)
                if "error" in result:
                    print(f"{Color.ERROR}✗ {result['error']}{Color.RESET}")
            elif cmd.startswith("!download "):
                parts = cmd.split(maxsplit=2)
                remote = parts[1]
                local = parts[2] if len(parts) > 2 else os.path.basename(remote)
                result = ctrl.download_file(remote, local)
                if "error" in result:
                    print(f"{Color.ERROR}✗ {result['error']}{Color.RESET}")
            else:
                start = time.time()
                result = ctrl.execute(cmd)
                elapsed = time.time() - start
                ctrl.print_result(result)
                print(f"{Color.DIM}[{format_duration(elapsed)}]{Color.RESET}")
                
        except KeyboardInterrupt:
            print(f"\n{Color.WARN}Use !exit to quit{Color.RESET}")
        except EOFError:
            break


def main():
    # Disable colors if not TTY
    if not sys.stdout.isatty():
        Color.disable()
    
    parser = argparse.ArgumentParser(
        description="Bridge Controller v3.2 - Remote Command Client",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", "-u", required=True, help="Bridge URL")
    parser.add_argument("--cmd", "-c", help="Execute command")
    parser.add_argument("--timeout", "-t", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--interactive", "-i", action="store_true", help="REPL mode")
    parser.add_argument("--api-key", "-k", help="API key for auth")
    parser.add_argument("--upload", type=str, help="Upload file")
    parser.add_argument("--download", type=str, help="Download file")
    parser.add_argument("--remote-path", type=str, help="Remote path for upload/download")
    parser.add_argument("--file", "-f", type=str, help="Execute commands from file")
    parser.add_argument("--no-color", action="store_true", help="Disable colors")
    args = parser.parse_args()
    
    if args.no_color:
        Color.disable()
    
    ctrl = BridgeController(args.url, args.timeout, args.api_key)
    
    if not ctrl.check_health():
        sys.exit(1)
    
    if args.upload:
        result = ctrl.upload_file(args.upload, args.remote_path)
        sys.exit(0 if result.get("success") else 1)
    
    if args.download:
        local = args.remote_path or os.path.basename(args.download)
        result = ctrl.download_file(args.download, local)
        sys.exit(0 if result.get("success") else 1)
    
    if args.cmd:
        result = ctrl.execute(args.cmd)
        ctrl.print_result(result)
        sys.exit(result.get("returncode", 0))
    
    if args.file:
        try:
            with open(args.file) as f:
                commands = [l.strip() for l in f if l.strip() and not l.startswith('#')]
            print(f"{Color.INFO}▶{Color.RESET} Running {len(commands)} commands from {args.file}\n")
            for i, cmd in enumerate(commands, 1):
                print(f"{Color.DIM}[{i}/{len(commands)}]{Color.RESET} {cmd}")
                result = ctrl.execute(cmd)
                ctrl.print_result(result)
                print()
        except FileNotFoundError:
            print(f"{Color.ERROR}✗{Color.RESET} File not found: {args.file}")
            sys.exit(1)
        sys.exit(0)
    
    if args.interactive:
        interactive_mode(ctrl)
        sys.exit(0)
    
    parser.print_help()


if __name__ == "__main__":
    main()
