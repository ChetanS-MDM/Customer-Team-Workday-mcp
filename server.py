"""
Workday MCP server — production-ready access via SSO.

Login once, then all tools work using saved cookies. No API credentials needed.
Profile/announcements/inbox are instant (pure API). Team absence data uses a
headless (invisible) browser and is cached for 1 hour. Leave requests use a
visible browser so you can verify the submission.
"""

import asyncio
import logging
import sys
from typing import Any, Awaitable, Callable

from mcp.server.fastmcp import FastMCP

import auth
import config as cfg
import workday_client as wd

logging.basicConfig(
    level=cfg.LOG_LEVEL,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("workday_mcp")

mcp = FastMCP(
    "workday",
    host="0.0.0.0" if cfg.MCP_TRANSPORT != "stdio" else "127.0.0.1",
    port=cfg.MCP_PORT,
    instructions=(
        "Access to the user's Workday data via SSO session.\n"
        "If a tool says the user isn't signed in, call workday_login once — "
        "the session persists automatically.\n\n"
        "## Leave Request — Conversational Flow\n"
        "When the user wants to apply for leave, ALWAYS follow this step-by-step flow:\n"
        "1. ASK: 'Would you like short-term time off (casual, sick, privilege) "
        "or long-term leave (paternity, extended)?'\n"
        "2. Based on their answer, SHOW the available leave types for that category:\n"
        "   - Short-term (Time Off): Casual Leave, Sick Leave, Privilege Leave, "
        "Compensatory, Education Leave, Short Term Leave Without Pay, Transfer Leave\n"
        "   - Long-term (Leave): Paternity Leave, and other long-term types\n"
        "   Note: Workday may append a location suffix (e.g. '- Hyderabad'). "
        "The leave_plan parameter does partial matching, so 'Casual Leave' matches "
        "'Casual Leave - Hyderabad'.\n"
        "   ASK: 'Which type of leave?'\n"
        "3. ASK for dates: 'What dates? (start date, and end date if multi-day)'\n"
        "4. ASK: 'Any comments or reason for the leave? (optional)'\n"
        "5. CONFIRM all details with the user before submitting:\n"
        "   'I will submit: [type] from [start] to [end]. Proceed?'\n"
        "6. Only THEN call request_absence with all the collected details.\n\n"
        "Optionally, call get_leave_options to dynamically discover leave "
        "categories and types from Workday (takes ~30s, uses browser).\n\n"
        "NEVER call request_absence without first collecting all details "
        "through conversation. The user must confirm before submission."
    ),
)

_client: wd.WorkdayClient | None = None


def _get_client() -> wd.WorkdayClient:
    global _client
    if _client is None:
        _client = wd.WorkdayClient()
    return _client


async def _run(tool: str, action: Callable[[wd.WorkdayClient], Awaitable[Any]]) -> Any:
    try:
        result = await action(_get_client())
        log.info("%s ok", tool)
        return result
    except auth.NotSignedIn:
        log.info("%s: session expired, attempting auto-login...", tool)
        try:
            login_result = await auth.authenticate()
            if login_result.get("signed_in"):
                result = await action(_get_client())
                log.info("%s ok (after auto-login)", tool)
                return result
        except Exception:
            pass
        return {"signed_in": False, "error": "Session expired. Call workday_login to sign in again."}
    except wd.WorkdayError as exc:
        log.info("%s failed: %s", tool, exc)
        return {"error": str(exc)}
    except Exception:
        log.exception("%s unexpected error", tool)
        return {"error": "Unexpected error. Check the server log."}


# ── Tools ─────────────────────────────────────────────────────────────

@mcp.tool()
async def workday_login(wait_seconds: int = 120) -> dict:
    """Sign in to Workday. Reuses saved session if valid — only opens a browser for first-time SSO.
    After login, team data is pre-loaded in the background for fast queries."""
    try:
        result = await auth.authenticate(wait_seconds)
        if result.get("signed_in"):
            asyncio.get_event_loop().create_task(_get_client().pre_warm())
        return result
    except Exception:
        log.exception("workday_login failed")
        return {"signed_in": False, "error": "Login failed. Check the server log."}


@mcp.tool()
async def workday_status() -> dict:
    """Check whether you're signed in to Workday. Also shows cache status."""
    session = auth.load_session()
    if not session:
        return {"signed_in": False, "message": "Not signed in. Call workday_login."}
    user_info = await auth.validate_session()
    if user_info:
        return {
            "signed_in": True,
            "user": user_info.get("displayName"),
            "worker_id": user_info.get("workerID"),
            "method": session.get("method"),
            "cache": wd.cache_status(),
        }
    return {"signed_in": False, "message": "Session expired. Call workday_login."}


@mcp.tool()
async def workday_sign_out() -> dict:
    """Forget the Workday session and clear all cached data."""
    _get_client().sign_out()
    return {"signed_in": False, "message": "Signed out. All cached data cleared."}


@mcp.tool()
async def get_my_profile() -> dict:
    """Your Workday profile: name, worker ID, greeting, quick actions. Instant — pure API, no browser."""
    return await _run("get_my_profile", lambda c: c.my_profile())


@mcp.tool()
async def get_announcements() -> dict:
    """Company announcements from Workday. Instant — pure API, no browser."""
    return await _run("get_announcements", lambda c: c.announcements())


@mcp.tool()
async def get_inbox() -> dict:
    """Your pending actions, approval requests, and important dates. Instant — pure API, no browser."""
    return await _run("get_inbox", lambda c: c.inbox())


@mcp.tool()
async def get_my_team() -> dict:
    """Who in your team is on leave this month. First call ~15s (headless browser, invisible),
    then cached for 1 hour — repeat calls are instant."""
    return await _run("get_my_team", lambda c: c.team())


@mcp.tool()
async def find_worker(query: str, limit: int = 10) -> dict:
    """Search Workday for people by name or worker ID. Uses headless browser (invisible)."""
    return await _run("find_worker", lambda c: c.find_workers(query, limit))


@mcp.tool()
async def get_direct_reports(worker_id: str) -> dict:
    """Direct reports of a manager (placeholder — use find_worker or get_team_time_off instead)."""
    return await _run("get_direct_reports", lambda c: c.direct_reports(worker_id))


@mcp.tool()
async def get_team_time_off(start_date: str, end_date: str, include_type: bool = False) -> dict:
    """Who in your team has time off between start_date and end_date (YYYY-MM-DD, max 92 days).
    First call ~15s (headless, invisible), then cached for 1 hour."""
    return await _run(
        "get_team_time_off",
        lambda c: c.team_time_off(start_date, end_date, include_type),
    )


@mcp.tool()
async def get_my_time_off(start_date: str, end_date: str) -> dict:
    """Your own time off between start_date and end_date (YYYY-MM-DD, max 92 days)."""
    return await _run("get_my_time_off", lambda c: c.my_time_off(start_date, end_date))


@mcp.tool()
async def get_my_time_off_balances() -> dict:
    """Your current time off balances by leave plan (casual, sick, privilege, etc.)."""
    return await _run("get_my_time_off_balances", lambda c: c.my_balances())


@mcp.tool()
async def get_leave_options(target_date: str = "") -> dict:
    """Discover available leave categories and types from the Workday absence form.
    Returns the two top-level categories (Short-Term Time Off, Long-Term Leave) and
    the specific leave types under each. Uses a headless browser (~30s).
    target_date: Optional YYYY-MM-DD date to navigate to (defaults to today).
    Call this when the user asks to apply leave and you want to show them the
    exact options available in their Workday instance."""
    return await _run(
        "get_leave_options",
        lambda c: c.get_leave_options(target_date or None),
    )


@mcp.tool()
async def request_absence(
    start_date: str,
    end_date: str = "",
    absence_type: str = "Time Off",
    leave_plan: str = "",
    comments: str = "",
) -> dict:
    """Apply for leave / request time off in Workday.
    start_date: First day of leave (YYYY-MM-DD)
    end_date: Last day of leave (YYYY-MM-DD, defaults to start_date for single day)
    absence_type: "Time Off" for short-term (casual, sick, privilege) or "Leave - Long-Term" for extended leave
    leave_plan: Specific leave type — e.g. "Casual Leave", "Privilege Leave", "Sick Leave"
    comments: Optional reason/note for the request"""
    return await _run(
        "request_absence",
        lambda c: c.request_absence(start_date, end_date or None, absence_type, leave_plan, comments),
    )


if cfg.ENABLE_RAW_GET:

    @mcp.tool()
    async def workday_raw_get(path: str, params: dict[str, str] | None = None) -> dict:
        """Dev tool: GET any Workday path with your session cookies."""
        return await _run("workday_raw_get", lambda c: c.raw_get(path, params))


def main() -> None:
    log.info("Starting Workday MCP (transport=%s, port=%s)", cfg.MCP_TRANSPORT, cfg.MCP_PORT)
    mcp.run(transport=cfg.MCP_TRANSPORT)


if __name__ == "__main__":
    main()
