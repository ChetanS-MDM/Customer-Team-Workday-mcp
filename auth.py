"""
SSO session management for the Workday MCP server.

Login once via Playwright, then reuse cookies for all subsequent API/browser
calls. The browser only opens when cookies are missing or truly expired.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import time
from typing import Any

import httpx

import config as cfg

log = logging.getLogger("workday_mcp")


class AuthError(Exception):
    pass


class NotSignedIn(AuthError):
    pass


_WORKDAY_DOMAINS = (".myworkday.com", ".myworkdaycdn.com")
_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(verify=False, timeout=30.0)
    return _http


def _cookie_applies_to_workday(cookie: dict) -> bool:
    domain = (cookie.get("domain") or "").lower().lstrip(".")
    return any(domain.endswith(wd.lstrip(".")) for wd in _WORKDAY_DOMAINS)


def _build_cookie_header(cookies: list[dict]) -> str:
    return "; ".join(
        f"{c['name']}={c['value']}" for c in cookies if _cookie_applies_to_workday(c)
    )


# ── Persistence ───────────────────────────────────────────────────────

def _write_json(path, data: Any) -> None:
    cfg.ensure_data_dir()
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    if platform.system() != "Windows":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _read_json(path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_cookies(cookies: list[dict]) -> None:
    _write_json(cfg.COOKIES_FILE, cookies)


def load_cookies() -> list[dict] | None:
    data = _read_json(cfg.COOKIES_FILE)
    return data if isinstance(data, list) else None


def save_session(session: dict) -> None:
    _write_json(cfg.SESSION_FILE, session)


def load_session() -> dict | None:
    data = _read_json(cfg.SESSION_FILE)
    return data if isinstance(data, dict) else None


def clear_session() -> None:
    for path in (cfg.COOKIES_FILE, cfg.SESSION_FILE, cfg.STORAGE_STATE_FILE):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# ── Session validation (pure HTTP, no browser) ───────────────────────

_last_validated: float = 0.0
_VALIDATION_INTERVAL = 300  # re-validate every 5 minutes at most

async def _session_is_live(cookies: list[dict], user_agent: str) -> dict | None:
    cookie_header = _build_cookie_header(cookies)
    if not cookie_header:
        return None
    headers = {
        "Cookie": cookie_header,
        "User-Agent": user_agent,
        "Accept": "application/json",
    }
    try:
        client = _get_http()
        resp = await client.post(cfg.USERINIT_URL, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) and data.get("userID") else None
    except Exception as exc:
        log.debug("Liveness check failed: %s", exc)
        return None


async def validate_session() -> dict | None:
    cookies = load_cookies()
    session = load_session()
    if not cookies or not session:
        return None
    ua = session.get("user_agent", "")
    return await _session_is_live(cookies, ua)


async def ensure_authenticated() -> None:
    """Ensure we have a valid session. Raises NotSignedIn if we don't.

    Avoids hitting the API on every single tool call by caching the
    validation result for up to 5 minutes.
    """
    global _last_validated
    now = time.time()
    if now - _last_validated < _VALIDATION_INTERVAL:
        cookies = load_cookies()
        session = load_session()
        if cookies and session:
            return

    user_info = await validate_session()
    if user_info:
        _last_validated = now
        session = load_session()
        if session:
            session["validated_at"] = now
            save_session(session)
        return

    # Try silent refresh before giving up
    result = await _try_silent_refresh()
    if result and result.get("signed_in"):
        _last_validated = now
        return

    raise NotSignedIn(
        "Workday session expired. Call workday_login to re-authenticate."
    )


# ── Headers for API calls ────────────────────────────────────────────

def get_headers() -> dict[str, str]:
    cookies = load_cookies()
    session = load_session()
    if not cookies or not session:
        raise NotSignedIn("Not signed in to Workday. Call workday_login first.")
    cookie_header = _build_cookie_header(cookies)
    if not cookie_header:
        raise NotSignedIn("No Workday cookies found. Call workday_login first.")
    return {
        "Cookie": cookie_header,
        "User-Agent": session.get("user_agent", ""),
        "Accept": "application/json",
    }


# ── Authentication flow ──────────────────────────────────────────────

async def authenticate(wait_seconds: int | None = None) -> dict:
    """Sign in to Workday. Uses saved session if valid, otherwise opens browser."""
    global _last_validated
    timeout = min(wait_seconds or cfg.AUTH_TIMEOUT_SECONDS, cfg.AUTH_TIMEOUT_SECONDS)

    user_info = await validate_session()
    if user_info:
        _last_validated = time.time()
        session = load_session()
        if session:
            session["validated_at"] = time.time()
            save_session(session)
        log.info("Existing session valid for %s", user_info.get("displayName"))
        return {
            "signed_in": True,
            "user": user_info.get("displayName"),
            "method": "cached_cookies",
        }

    result = await _try_silent_refresh()
    if result:
        _last_validated = time.time()
        return result

    result = await _interactive_login(timeout)
    if result.get("signed_in"):
        _last_validated = time.time()
    return result


async def _try_silent_refresh() -> dict | None:
    if not cfg.STORAGE_STATE_FILE.exists():
        return None
    log.info("Attempting silent session refresh...")
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                storage_state=str(cfg.STORAGE_STATE_FILE)
            )
            page = await context.new_page()
            try:
                await page.goto(
                    cfg.HOME_URL, wait_until="domcontentloaded", timeout=30000
                )
                await page.wait_for_timeout(3000)
            except Exception:
                await browser.close()
                return None
            cookies = await context.cookies()
            user_agent = await page.evaluate("() => navigator.userAgent")
            user_info = await _session_is_live(cookies, user_agent)
            if user_info:
                save_cookies(cookies)
                await context.storage_state(path=str(cfg.STORAGE_STATE_FILE))
                save_session(
                    {
                        "user_id": user_info.get("userID"),
                        "worker_id": user_info.get("workerID"),
                        "display_name": user_info.get("displayName"),
                        "user_agent": user_agent,
                        "authenticated_at": time.time(),
                        "method": "silent_refresh",
                    }
                )
                log.info("Silent refresh succeeded")
                await browser.close()
                return {
                    "signed_in": True,
                    "user": user_info.get("displayName"),
                    "method": "silent_refresh",
                }
            await browser.close()
            return None
    except Exception as exc:
        log.info("Silent refresh failed: %s", exc)
        return None


async def _interactive_login(timeout_seconds: int) -> dict:
    from playwright.async_api import async_playwright

    cfg.ensure_data_dir()
    cfg.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(cfg.BROWSER_PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(
                cfg.HOME_URL, wait_until="domcontentloaded", timeout=60000
            )
        except Exception:
            pass
        try:
            await page.bring_to_front()
        except Exception:
            pass

        log.info("Browser open for SSO — waiting up to %ds", timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        user_info = None

        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            try:
                url_lower = page.url.lower()
                if any(
                    x in url_lower
                    for x in ("microsoftonline", "login.do", "samlrequest")
                ):
                    continue
                cookies = await context.cookies()
                user_agent = await page.evaluate("() => navigator.userAgent")
                user_info = await _session_is_live(cookies, user_agent)
                if user_info:
                    break
            except Exception:
                continue

        if not user_info:
            await context.close()
            return {
                "signed_in": False,
                "error": "SSO login timed out. Try workday_login again.",
            }

        save_cookies(cookies)
        try:
            await context.storage_state(path=str(cfg.STORAGE_STATE_FILE))
        except Exception:
            pass
        save_session(
            {
                "user_id": user_info.get("userID"),
                "worker_id": user_info.get("workerID"),
                "display_name": user_info.get("displayName"),
                "user_agent": user_agent,
                "authenticated_at": time.time(),
                "method": "interactive",
            }
        )
        log.info("Interactive login succeeded")
        await context.close()
        return {
            "signed_in": True,
            "user": user_info.get("displayName"),
            "method": "interactive",
        }
