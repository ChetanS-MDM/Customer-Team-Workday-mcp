# Workday MCP Server

A Model Context Protocol server that gives Claude access to Workday via SSO cookies. 14 tools: profile, team absences, leave balances, leave requests, worker search, announcements, inbox.

## Quick start

```bash
./setup.sh            # Install deps + Chromium
# Add MCP config to Claude (see README.md)
# Tell Claude: "Sign me in to Workday"
```

## Architecture

- **Instant tools** (REST/GraphQL): `get_my_profile`, `get_announcements`, `get_inbox`
- **Cached browser tools** (headless, invisible): `get_my_team`, `get_team_time_off`, `get_my_time_off`, `get_my_time_off_balances`, `find_worker`, `get_leave_options`
- **Write tool** (headless browser automation): `request_absence`

All waits are signal-based (`wait_for_selector` / `wait_for_function`), not fixed timeouts.

## Key patterns

- `_open_page()` context manager handles all browser lifecycle (launch → cleanup)
- `_cache` dict with TTL per key prefix (team: 1h, calendar: 1h, search: 10m, leave_options: 24h)
- Leave type selection: must click `promptIcon` first to open dropdown, then `promptOption` for category, then sub-type
- Workday appends location suffixes (e.g. "Casual Leave - Hyderabad") — partial matching handles this

## Testing

```bash
WORKDAY_BASE_URL=https://wd115.myworkday.com WORKDAY_TENANT=lilly .venv/bin/python3 -c "import server; print(f'{len(server.mcp._tool_manager.list_tools())} tools OK')"
```

## Skill

Use `/workday` to invoke the Workday skill for guided leave requests and team queries.
