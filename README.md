# Workday MCP Server

Access your Workday data and submit leave requests from Claude Desktop or Claude Code, signed in as you via SSO. No API credentials needed.

## What you can do

| Ask Claude... | Tool |
|---|---|
| "Sign me in to Workday" | `workday_login` |
| "What's my Workday profile?" | `get_my_profile` |
| "Who in my team is on leave this month?" | `get_my_team` |
| "Who's off between Dec 1-15?" | `get_team_time_off` |
| "Search for Apoorva in Workday" | `find_worker` |
| "What are my leave balances?" | `get_my_time_off_balances` |
| "Am I on leave next week?" | `get_my_time_off` |
| "Any announcements?" | `get_announcements` |
| "What's in my inbox?" | `get_inbox` |
| "What leave types can I apply for?" | `get_leave_options` |
| "Apply for casual leave Dec 2-4" | `request_absence` |

## Setup

### Prerequisites

- **Python 3.10+** with pip
- **Claude Desktop** or **Claude Code**

### Step 1: Install

**Mac/Linux:**
```bash
cd workday-mcp
chmod +x setup.sh
./setup.sh
```

**Windows (PowerShell or CMD):**
```cmd
cd workday-mcp
setup.bat
```

The script creates a virtual environment, installs dependencies, and downloads a Chromium browser for the SSO login flow.

### Step 2: Configure Claude

**Claude Desktop** — open **Settings > Developer > Edit Config** and add:

```json
{
  "mcpServers": {
    "workday": {
      "command": "/full/path/to/workday-mcp/.venv/bin/python",
      "args": ["/full/path/to/workday-mcp/server.py"],
      "env": {
        "WORKDAY_BASE_URL": "https://wd115.myworkday.com",
        "WORKDAY_TENANT": "lilly"
      }
    }
  }
}
```

**Claude Code** — add to `~/.claude/settings.json` under `mcpServers`:

```json
"workday": {
  "command": "/full/path/to/workday-mcp/.venv/bin/python3",
  "args": ["/full/path/to/workday-mcp/server.py"],
  "env": {
    "WORKDAY_BASE_URL": "https://wd115.myworkday.com",
    "WORKDAY_TENANT": "lilly"
  }
}
```

Use **absolute paths**. The setup script prints the exact config to copy. Restart Claude after saving.

### Step 3: Sign in

Tell Claude: **"Sign me in to Workday"**

A Chromium browser opens for Entra SSO. Complete login there. The session is saved to `~/.workday-mcp/` and survives restarts — re-login only when SSO expires.

## How it works

| Layer | Method | Speed |
|---|---|---|
| Profile, greeting, inbox, announcements | REST + GraphQL APIs | Instant |
| Team absence, balances, calendar | Headless browser + 1h cache | ~12s first call, instant after |
| Leave options discovery | Headless browser + 24h cache | ~20s first call, instant after |
| Leave requests | Headless browser automation | ~15-20s |
| Worker search | Headless browser + 10min cache | ~8s first call, instant after |

All browser operations run headless (invisible). The leave request flow automates: navigate to absence calendar → select dates → open request form → pick leave type → submit.

## Leave request flow

Claude follows a conversational flow before submitting:

1. Ask: short-term time off or long-term leave?
2. Show available types for that category
3. Collect dates
4. Collect optional comments
5. Confirm all details
6. Submit via `request_absence`

## Settings

| Variable | Required | Default | Description |
|---|---|---|---|
| `WORKDAY_BASE_URL` | Yes | — | e.g. `https://wd115.myworkday.com` |
| `WORKDAY_TENANT` | Yes | — | e.g. `lilly` |
| `WORKDAY_AUTH_TIMEOUT_SECONDS` | No | `300` | SSO browser window timeout |
| `WORKDAY_LOG_LEVEL` | No | `INFO` | Logging verbosity |
| `WORKDAY_CA_BUNDLE` | No | — | Corporate proxy CA cert path |
| `WORKDAY_ENABLE_RAW_GET` | No | `0` | Set `1` for dev raw GET tool |
| `MCP_TRANSPORT` | No | `stdio` | Set `streamable-http` for cloud |
| `MCP_PORT` | No | `8000` | HTTP port when running cloud transport |

## Cloud Deployment

Share the MCP server with your team so they connect with just a URL — no local install needed.

### Option A: Free tunnel from your Mac (zero cost)

Run the MCP server locally and expose it with a free Cloudflare tunnel:

```bash
brew install cloudflared   # one-time
./start-cloud.sh
```

This starts the MCP server + a public tunnel. Share the `https://...trycloudflare.com` URL with your team. Works while your Mac is on.

### Option B: Docker (any server)

```bash
cp .env.example .env
docker compose up -d --build
```

### Option C: Azure VM (~$25-36/month)

```bash
az login
./deploy-azure.sh
```

The script creates a B2s VM, installs Docker, builds the container, and prints the connection URLs.

### Option D: Oracle Cloud (always free)

Oracle offers a permanently free ARM VM (4 CPU, 24 GB RAM):
1. Sign up at [cloud.oracle.com](https://cloud.oracle.com)
2. Create an Ampere A1 instance (Ubuntu 22.04)
3. SSH in, install Docker, clone the project, run `docker compose up -d`

### Architecture (Options B/C/D)

```
Cloud server
├── Workday MCP server (port 8000)  ← Claude Desktop/Code connects here
│   └── Streamable HTTP transport
├── Playwright + headless Chromium
└── noVNC (port 6080)              ← one-time SSO login via browser
```
cp .env.example .env    # edit with your Workday URL/tenant
docker compose up -d --build
```

### After deployment

1. **Initial SSO login**: Open `http://<vm-ip>:6080/vnc.html` in your browser
2. Tell Claude: "Sign me in to Workday" — complete Entra SSO in the noVNC window
3. Once signed in, cookies are saved — all future calls work automatically

### Team members connect with:

**Claude Desktop** (Settings > Developer > Edit Config):
```json
{
  "mcpServers": {
    "workday": {
      "url": "http://<vm-ip>:8000/mcp"
    }
  }
}
```

**Claude Code** (`~/.claude/settings.json`):
```json
{
  "mcpServers": {
    "workday": {
      "url": "http://<vm-ip>:8000/mcp"
    }
  }
}
```

No `command` or `args` needed — just the URL. Restart Claude after saving.

### Security

- Restrict port 8000 to your corporate IP range via Azure NSG rules
- Use SSH tunnel for noVNC: `ssh -L 6080:localhost:6080 azureuser@<vm-ip>`
- Cookies are stored on the VM with owner-only permissions
- No secrets in the Docker image — env vars at runtime

### Cost

| Resource | Monthly |
|---|---|
| B2s VM (2 vCPU, 4 GB) | ~$30 |
| Standard SSD 32 GB | ~$2.40 |
| Public IP | ~$3.65 |
| **Total** | **~$36/month** |

1-year reserved: ~$25/month. Auto-shutdown (business hours only): ~$12-15/month.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Not signed in" | Say "Sign me in to Workday" |
| SSO page hangs | Complete MFA/password in the Chromium window |
| SSL errors | Set `WORKDAY_CA_BUNDLE` to your proxy CA PEM |
| Server not in Claude | Check JSON syntax, use absolute paths, restart Claude |

## Files

| File | Purpose |
|---|---|
| `server.py` | MCP server — 14 tools with conversational leave request flow |
| `workday_client.py` | Workday API + browser automation (signal-based waits, cached) |
| `auth.py` | SSO login, cookie capture, session persistence |
| `config.py` | Environment variables and paths |
| `requirements.txt` | Python dependencies |
| `setup.sh` / `setup.bat` | One-command local install |
| `Dockerfile` | Container image for cloud deployment |
| `docker-compose.yml` | MCP server + noVNC services |
| `deploy-azure.sh` | One-command Azure VM deployment |
| `start-cloud.sh` | Free tunnel mode — MCP + Cloudflare tunnel |
| `.env.example` | Template env vars for cloud |
| `.claude/skills/workday.md` | `/workday` skill for guided usage |
