# 🌉 Remote Bridge System v3.2

A lightweight, stable system for remote command execution over HTTP. Perfect for remote development, debugging, file transfer, and automation.

## ✨ Features

- **Stable & Reliable** - Auto-retry on connection failures
- **Compression** - Gzip compression for large outputs
- **File Transfer** - Chunked upload/download with progress bars
- **Authentication** - Optional API key protection
- **Interactive REPL** - Command history and built-in commands
- **Cross-Platform** - Works on Linux, Windows (WSL), macOS
- **Beautiful Colors** - Colored output for better readability
- **Full Logging** - Complete command logging with timestamps

## 🚀 Quick Start

### Step 1: Start the Agent

```bash
# Basic usage
python3 bridge_agent.py

# With authentication
python3 bridge_agent.py --api-key your-secret-key

# Custom port
python3 bridge_agent.py --port 9000
```

### Step 2: Expose via Tunnel

```bash
cloudflared tunnel --url http://localhost:8765
```

Output:
```
Your quick Tunnel has been created! Visit it at:
https://xxxx-xxxx-xxxx-xxxx.trycloudflare.com
```

### Step 3: Control Remotely

```bash
# Health check
python3 bridge_controller.py -u https://xxx.trycloudflare.com --cmd "echo hello"

# Interactive mode
python3 bridge_controller.py -u https://xxx.trycloudflare.com -i

# Upload file
python3 bridge_controller.py -u https://xxx.trycloudflare.com --upload myfile.zip

# Download file
python3 bridge_controller.py -u https://xxx.trycloudflare.com --download /remote/file.txt
```

## 📖 Command Reference

### Bridge Agent

```bash
python3 bridge_agent.py [OPTIONS]

Options:
  -p, --port PORT      Port to listen on (default: 8765)
  -k, --api-key KEY    Enable API key authentication
```

Environment variables:
- `BRIDGE_API_KEY` - Alternative way to set API key

### Bridge Controller

```bash
python3 bridge_controller.py -u URL [OPTIONS]

Options:
  -u, --url URL         Bridge URL (required)
  -c, --cmd COMMAND     Execute single command
  -i, --interactive     Interactive REPL mode
  -k, --api-key KEY     API key for authentication
  -t, --timeout SEC     Command timeout (default: 60)
  --upload FILE         Upload file to remote
  --download FILE       Download file from remote
  --remote-path PATH    Remote path for upload/download
  -f, --file FILE       Execute commands from file
```

### Interactive Mode Commands

| Command | Description |
|---------|-------------|
| `!health` | Check bridge status |
| `!upload <local> [remote]` | Upload file |
| `!download <remote> [local]` | Download file |
| `!history` | Show command history |
| `!exit` | Exit interactive mode |

## 📦 File Transfer

### Upload

```bash
# Command line
python3 bridge_controller.py -u $URL --upload local.zip --remote-path /remote/dest.zip

# Interactive mode
bridge> !upload local.zip /remote/dest.zip
```

### Download

```bash
# Command line
python3 bridge_controller.py -u $URL --download /remote/file.txt --remote-path local.txt

# Interactive mode  
bridge> !download /remote/file.txt local.txt
```

## 🔐 Security

### Enable Authentication

**Agent side:**
```bash
python3 bridge_agent.py --api-key my-secret-key-123
# or
BRIDGE_API_KEY=my-secret-key-123 python3 bridge_agent.py
```

**Controller side:**
```bash
python3 bridge_controller.py -u $URL -k my-secret-key-123 -i
```

### Security Tips

1. **Always use HTTPS** - Cloudflared provides this automatically
2. **Use strong API keys** - At least 32 random characters
3. **Close tunnel when done** - Stop cloudflared when not in use
4. **Monitor logs** - Check agent console for executed commands

## 📡 API Reference

### GET / - Health Check

Response:
```json
{
  "status": "online",
  "version": "3.0",
  "timestamp": 1234567890.123,
  "auth_enabled": false
}
```

### POST / - Actions

#### Execute Command
```json
{
  "action": "exec",
  "command": "ls -la",
  "timeout": 60
}
```

Response:
```json
{
  "stdout": "total 0\n...",
  "stderr": "",
  "returncode": 0
}
```

#### Upload File
```json
{
  "action": "upload",
  "filename": "/path/to/file",
  "data": "base64-encoded-content",
  "mode": "write"
}
```

#### Download File
```json
{
  "action": "download",
  "filename": "/path/to/file",
  "offset": 0,
  "chunk_size": 524288
}
```

## 🔧 Use Cases

- **Remote Debugging** - Execute commands on remote machines
- **File Synchronization** - Transfer files between machines
- **CI/CD Integration** - Trigger remote builds/deployments
- **IoT Management** - Control devices behind NAT
- **Bug Bounty** - Test from different network perspectives

## 📋 Requirements

- Python 3.7+
- `requests` library (controller only)

```bash
pip install requests
```

## 🛠 Troubleshooting

### Connection Failed
- Check if bridge agent is running
- Verify tunnel URL is correct
- Try restarting cloudflared tunnel

### Timeout Errors
- Increase timeout with `-t` flag
- Check if command produces large output

### Authentication Error
- Verify API key matches on both sides
- Check Authorization header is being sent

### Port Already in Use
```bash
# Use different port
python3 bridge_agent.py --port 9000
```

## 📄 License

MIT License

---

Made with ❤️ for developers
