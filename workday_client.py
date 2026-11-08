"""
Workday client — production-ready, cookie-based.

Architecture:
- Profile/status/announcements/inbox: pure REST + GraphQL (instant, no browser)
- Team absence/balances: headless browser + 1-hour cache (invisible to user)
- Search: headless browser + 10-min cache (invisible to user)
- Request absence: headless browser automation
- Session validated before every browser scrape
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any, Mapping

import httpx

import auth
import config as cfg

log = logging.getLogger("workday_mcp")

MAX_RANGE_DAYS = 92
HTTP_TIMEOUT = 30
ABSENCE_URL = f"{cfg.BASE_URL}/{cfg.TENANT}/d/task/2997$18982.htmld"

_LOGIN_MARKERS = ("microsoftonline", "login.do", "saml", "invalid-url")
_CALENDAR_READY = '[data-automation-id="calendarToolbar"]'


# ─── Errors ──────────────────────────────────────────────────────────

class WorkdayError(Exception):
    pass

class NoAccess(WorkdayError):
    pass

class NotFound(WorkdayError):
    pass


# ─── In-memory cache with TTL ────────────────────────────────────────

_cache: dict[str, tuple[float, Any]] = {}

CACHE_TTL = {
    "profile": 1800,
    "team": 3600,
    "calendar": 3600,
    "search": 600,
    "announcements": 1800,
    "inbox": 900,
    "leave_options": 86400,
}


def _cache_get(key: str) -> Any | None:
    if key in _cache:
        ts, data = _cache[key]
        ttl = CACHE_TTL.get(key.split(":")[0], 3600)
        if time.time() - ts < ttl:
            return data
        del _cache[key]
    return None


def _cache_set(key: str, data: Any) -> None:
    _cache[key] = (time.time(), data)


def cache_clear() -> None:
    _cache.clear()


def cache_status() -> dict:
    now = time.time()
    entries = []
    for key, (ts, _) in _cache.items():
        ttl = CACHE_TTL.get(key.split(":")[0], 3600)
        age = int(now - ts)
        entries.append({"key": key, "age_seconds": age, "ttl": ttl, "expires_in": max(0, ttl - age)})
    return {"entries": entries, "count": len(entries)}


# ─── Helpers ─────────────────────────────────────────────────────────

def _compact(record: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if v not in (None, "", [], {})}


def parse_range(start_date: str, end_date: str) -> tuple[str, str]:
    try:
        s, e = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except (TypeError, ValueError):
        raise WorkdayError("Dates must be YYYY-MM-DD.") from None
    if e < s:
        raise WorkdayError("end_date is before start_date.")
    if (e - s).days + 1 > MAX_RANGE_DAYS:
        raise WorkdayError(f"Max {MAX_RANGE_DAYS} days per request.")
    return s.isoformat(), e.isoformat()


# ─── Client ──────────────────────────────────────────────────────────

class WorkdayClient:

    def __init__(self) -> None:
        self._me_cache: dict[str, Any] | None = None
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(verify=False, timeout=HTTP_TIMEOUT)
        return self._http

    # ── HTTP helpers ─────────────────────────────────────────────────

    async def _api(self, method: str, url: str, **kwargs) -> Any:
        headers = auth.get_headers()
        c = await self._client()
        r = await c.request(method, url, headers=headers, **kwargs)
        if r.status_code == 401:
            raise auth.NotSignedIn("Session expired — call workday_login.")
        if r.status_code == 403:
            raise NoAccess("Workday says your account can't see this.")
        if r.status_code >= 400:
            raise WorkdayError(f"Workday returned HTTP {r.status_code}.")
        return r.json() if r.content else None

    def _url(self, path: str) -> str:
        return f"{cfg.BASE_URL}/{path.lstrip('/')}"

    # ── GraphQL ──────────────────────────────────────────────────────

    async def _graphql(self, operation: str, query: str, variables: dict | None = None) -> Any:
        url = self._url(f"/wday/pex/graphql/graphql?operation={operation}")
        body = [{"operationName": operation, "query": query, "variables": variables or {}}]
        headers = auth.get_headers()
        headers.update({
            "Content-Type": "application/json",
            "x-wd-tenant": cfg.TENANT,
            "x-wd-pex-graphql-client": "spaceflavor-web",
        })
        c = await self._client()
        r = await c.post(url, headers=headers, json=body)
        if r.status_code == 401:
            raise auth.NotSignedIn("GraphQL 401 — call workday_login.")
        if r.status_code >= 400:
            raise WorkdayError(f"GraphQL HTTP {r.status_code}.")
        result = r.json()
        if isinstance(result, list) and result:
            return result[0].get("data") if isinstance(result[0], dict) else result
        return result.get("data", result) if isinstance(result, dict) else result

    # ── Profile (pure API — instant) ─────────────────────────────────

    async def me(self) -> dict[str, Any]:
        if self._me_cache:
            return self._me_cache
        data = await self._api("POST", cfg.USERINIT_URL)
        if not isinstance(data, dict) or not data.get("userID"):
            raise WorkdayError("Workday didn't return your profile.")
        self._me_cache = data
        return data

    async def my_profile(self) -> dict[str, Any]:
        cached = _cache_get("profile")
        if cached:
            return cached
        me = await self.me()
        greeting, quick_actions = {}, []
        try:
            gql = await self._graphql(
                "WelcomeMini",
                "query WelcomeMini($input: WelcomeInput!) { home { welcome(input: $input) { dateMessage greeting { message } } } }",
                variables={"input": {"hour": datetime.now().hour}},
            )
            greeting = gql.get("home", {}).get("welcome", {})
        except Exception:
            pass
        try:
            gql = await self._graphql(
                "QuickActions",
                "query QuickActions { home { quickActions { visibleTasks { title } dropdownTasks { title } } } }",
            )
            acts = gql.get("home", {}).get("quickActions", {})
            quick_actions = [a["title"] for a in acts.get("visibleTasks", []) if a.get("title")]
        except Exception:
            pass
        result = _compact({
            "name": me.get("displayName") or me.get("name"),
            "worker_id": me.get("workerID"),
            "user_id": me.get("userID"),
            "locale": me.get("locale"),
            "tenant": me.get("tenant"),
            "greeting": greeting.get("greeting", {}).get("message"),
            "date": greeting.get("dateMessage"),
            "quick_actions": quick_actions or None,
        })
        _cache_set("profile", result)
        return result

    # ── Announcements & inbox (pure API — instant) ───────────────────

    async def announcements(self) -> dict[str, Any]:
        cached = _cache_get("announcements")
        if cached:
            return cached
        items = []
        try:
            gql = await self._graphql(
                "Announcements",
                "query Announcements { home { announcements { title } } }",
            )
            items = gql.get("home", {}).get("announcements", [])
        except Exception:
            pass
        result = {"announcements": [a for a in items if a.get("title")], "count": len(items)}
        _cache_set("announcements", result)
        return result

    async def inbox(self) -> dict[str, Any]:
        cached = _cache_get("inbox")
        if cached:
            return cached
        actions_count = 0
        try:
            me = await self.me()
            actions_count = me.get("awaitingActionsCount", 0)
        except Exception:
            pass
        result = {"awaiting_actions_count": actions_count}
        _cache_set("inbox", result)
        return result

    # ── Browser engine ───────────────────────────────────────────────

    @asynccontextmanager
    async def _open_page(self, headless: bool = True):
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless)
        try:
            if cfg.STORAGE_STATE_FILE.exists():
                try:
                    ctx = await browser.new_context(
                        storage_state=str(cfg.STORAGE_STATE_FILE),
                        viewport={"width": 1400, "height": 900},
                    )
                    yield await ctx.new_page()
                    return
                except Exception:
                    pass
            ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
            cookies = auth.load_cookies()
            if cookies:
                try:
                    await ctx.add_cookies(cookies)
                except Exception:
                    pass
            yield await ctx.new_page()
        finally:
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass

    async def _wait_for_calendar(self, pg) -> None:
        try:
            await pg.wait_for_selector(_CALENDAR_READY, timeout=15000)
        except Exception:
            pass

    def _is_login_page(self, url: str) -> bool:
        low = url.lower()
        return any(x in low for x in _LOGIN_MARKERS)

    async def _navigate_to_absence(self, pg) -> None:
        try:
            await pg.goto(ABSENCE_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await self._wait_for_calendar(pg)
        if self._is_login_page(pg.url):
            raise auth.NotSignedIn("Session expired — call workday_login.")
        await self._dismiss_dialog(pg)

    async def _dismiss_dialog(self, pg) -> None:
        try:
            ok = pg.locator('[data-automation-id="wd-CommandButton_uic_okButton"]')
            if await ok.count() > 0 and await ok.first.is_visible():
                await ok.first.click(timeout=2000)
                await pg.wait_for_timeout(500)
        except Exception:
            pass

    async def _enable_team_view(self, pg) -> None:
        try:
            toolbar = pg.locator('[data-automation-id="calendarToolbar"]')
            dropdown = toolbar.locator('[data-automation-id="dropdownArrow"]')
            await dropdown.click(timeout=3000)
            await pg.wait_for_timeout(500)
        except Exception:
            try:
                await pg.locator('[data-automation-id="dropDownCommandButton"]').first.click(timeout=3000)
                await pg.wait_for_timeout(500)
            except Exception:
                return

        vt = pg.get_by_role("option", name="View Teams")
        if await vt.count() > 0:
            await vt.first.click(timeout=3000)
            try:
                ok = pg.locator('[data-automation-id="wd-CommandButton_uic_okButton"]')
                await ok.wait_for(state="visible", timeout=8000)
                await ok.first.click(timeout=3000)
                await self._wait_for_calendar(pg)
            except Exception:
                pass
        else:
            await pg.keyboard.press("Escape")

    async def _go_to_month(self, pg, year: int, month: int) -> None:
        import calendar
        names = list(calendar.month_name)
        target = f"{names[month]} {year}"
        for _ in range(24):
            header = await pg.evaluate("""() => {
                for (const el of document.querySelectorAll('*')) {
                    const t = el.textContent.trim();
                    if (/^(January|February|March|April|May|June|July|August|September|October|November|December) \\d{4}$/.test(t)
                        && el.children.length === 0) return t;
                }
                return null;
            }""")
            if not header:
                break
            if header == target:
                return
            parts = header.split()
            cm, cy = names.index(parts[0]), int(parts[1])
            btn_id = "nextMonthButton" if (year, month) > (cy, cm) else "prevMonthButton"
            await pg.locator(f'[data-automation-id="{btn_id}"]').click(timeout=3000)
            # Wait for header to change instead of fixed sleep
            try:
                await pg.wait_for_function(
                    f'() => {{ for (const el of document.querySelectorAll("*")) {{ const t = el.textContent.trim(); if (/^(January|February|March|April|May|June|July|August|September|October|November|December) \\\\d{{4}}$/.test(t) && el.children.length === 0 && t !== "{header}") return true; }} return false; }}',
                    timeout=3000,
                )
            except Exception:
                await pg.wait_for_timeout(800)

    async def _extract_calendar(self, pg) -> dict[str, Any]:
        return await pg.evaluate(r"""() => {
            const result = { month: '', absences: [], holidays: [], balances: [] };

            for (const el of document.querySelectorAll('*')) {
                const t = el.textContent.trim();
                if (/^(January|February|March|April|May|June|July|August|September|October|November|December) \d{4}$/.test(t)
                    && el.children.length === 0) { result.month = t; break; }
            }

            const holidayMap = {};
            document.querySelectorAll('[aria-label]').forEach(el => {
                const a = el.getAttribute('aria-label') || '';
                if (!a.includes('Holiday')) return;
                const parts = a.split('|').map(p => p.trim());
                let name = '', dateStr = '';
                for (const p of parts) {
                    if (/^\d+ events?$/.test(p) || p === 'Holiday' || /^Holiday\s/.test(p)) {
                        if (/^Holiday\s/.test(p)) dateStr = p.replace('Holiday', '').trim();
                        continue;
                    }
                    if (/\d{4}$/.test(p)) { dateStr = dateStr || p; continue; }
                    name = p;
                }
                if (name && !holidayMap[name]) holidayMap[name] = dateStr || '';
            });
            for (const [name, d] of Object.entries(holidayMap))
                result.holidays.push(d ? (name + ' — ' + d) : name);

            const pat = /^(.+?)\s*\((?:On Leave\)\s*\()?([\d]+)\)$/;
            const lines = document.body.innerText.split('\n').map(l => l.trim()).filter(l => l);
            const people = {};
            let headerNameId = null;
            for (let i = 0; i < lines.length; i++) {
                if (lines[i] === 'Manage Absence' && i + 1 < lines.length) {
                    const m = lines[i + 1].match(pat);
                    if (m && m[2].length >= 5) headerNameId = m[2];
                    break;
                }
            }
            for (const line of lines) {
                const m = line.match(pat);
                if (m && m[2].length >= 5) {
                    if (m[2] === headerNameId) continue;
                    const name = m[1].replace(/\(On Leave\)/, '').replace(/\([^)]*\)$/, '').trim();
                    const id = m[2];
                    const key = name + '|' + id;
                    if (!people[key]) people[key] = { name, worker_id: id, on_leave: false, appearances: 0 };
                    people[key].appearances += 1;
                    if (line.includes('On Leave')) people[key].on_leave = true;
                }
            }
            result.absences = Object.values(people);

            const bsParts = document.body.innerText.split('Balances as of');
            const bs = bsParts.length > 1 ? bsParts[bsParts.length - 1] : null;
            if (bs) {
                const bLines = bs.split('\n').map(l => l.trim()).filter(l => l);
                let plan = '';
                for (const bl of bLines) {
                    if (bl === 'Request Absence') break;
                    if (bl.startsWith('•')) continue;
                    const isBalance = /^\d[\d.,]*\s*(Days?|Hours?)$/.test(bl);
                    if (isBalance && plan) {
                        result.balances.push({ plan, balance: bl });
                        plan = '';
                    } else if (/Leave|Compensatory|Education|Paternity|Transfer|Privilege|Sick|Casual|PTO|Vacation/.test(bl) && !isBalance) {
                        plan = bl;
                    }
                }
            }
            return result;
        }""")

    # ── Team tools (headless + cached) ───────────────────────────────

    async def _scrape_absence_page(self, year: int, month: int, team_view: bool = True) -> dict[str, Any]:
        session_check = await auth.validate_session()
        if not session_check:
            raise auth.NotSignedIn("Session expired — call workday_login.")

        async with self._open_page() as pg:
            await self._navigate_to_absence(pg)
            if team_view:
                await self._enable_team_view(pg)
            await self._go_to_month(pg, year, month)
            await pg.wait_for_timeout(1000)
            return await self._extract_calendar(pg)

    async def team(self) -> dict[str, Any]:
        today = date.today()
        key = f"team:{today.year}:{today.month}"
        cached = _cache_get(key)
        if cached:
            return cached
        data = await self._scrape_absence_page(today.year, today.month, team_view=True)
        result = _compact({
            "month": data.get("month"),
            "team_absences": data.get("absences", []),
            "holidays": data.get("holidays", []),
        })
        _cache_set(key, result)
        return result

    async def team_time_off(self, start_date: str, end_date: str, include_type: bool = False) -> dict[str, Any]:
        s, e = parse_range(start_date, end_date)
        t = date.fromisoformat(s)
        key = f"team:{t.year}:{t.month}"
        cached = _cache_get(key)
        if not cached:
            data = await self._scrape_absence_page(t.year, t.month, team_view=True)
            cached = _compact({
                "month": data.get("month"),
                "team_absences": data.get("absences", []),
                "holidays": data.get("holidays", []),
            })
            _cache_set(key, cached)
        return _compact({"from": s, "to": e, **cached})

    async def my_time_off(self, start_date: str, end_date: str) -> dict[str, Any]:
        s, e = parse_range(start_date, end_date)
        t = date.fromisoformat(s)
        key = f"calendar:{t.year}:{t.month}"
        cached = _cache_get(key)
        if not cached:
            data = await self._scrape_absence_page(t.year, t.month, team_view=False)
            cached = _compact({
                "month": data.get("month"),
                "events": data.get("absences", []),
                "holidays": data.get("holidays", []),
                "balances": data.get("balances", []),
            })
            _cache_set(key, cached)
        return _compact({"from": s, "to": e, **cached})

    async def my_balances(self) -> dict[str, Any]:
        today = date.today()
        key = f"calendar:{today.year}:{today.month}"
        cached = _cache_get(key)
        if not cached:
            data = await self._scrape_absence_page(today.year, today.month, team_view=False)
            cached = _compact({"month": data.get("month"), "balances": data.get("balances", [])})
            _cache_set(key, cached)
        return cached

    # ── Search (headless + cached) ───────────────────────────────────

    async def find_workers(self, query: str, limit: int = 10) -> dict[str, Any]:
        query = (query or "").strip()
        if len(query) < 2:
            raise WorkdayError("Search needs at least 2 characters.")
        limit = max(1, min(limit, 25))
        key = f"search:{query.lower()}"
        cached = _cache_get(key)
        if cached:
            return cached

        session_check = await auth.validate_session()
        if not session_check:
            raise auth.NotSignedIn("Session expired — call workday_login.")

        async with self._open_page() as pg:
            await pg.goto(cfg.HOME_URL, wait_until="domcontentloaded", timeout=30000)
            try:
                await pg.wait_for_selector('[data-automation-id="globalSearchInput"]', timeout=8000)
            except Exception:
                pass
            if self._is_login_page(pg.url):
                raise auth.NotSignedIn("Session expired — call workday_login.")
            sb = pg.locator('[data-automation-id="globalSearchInput"]')
            await sb.first.click()
            await sb.first.fill(query)
            await pg.wait_for_timeout(2000)
            results = await pg.evaluate("""() => {
                const items = [];
                document.querySelectorAll('[role="option"]').forEach(el => {
                    const t = el.textContent.trim();
                    if (t && t !== 'View More') items.push(t.substring(0, 300));
                });
                return items;
            }""")
            result = {"query": query, "count": len(results), "results": results[:limit]}
            _cache_set(key, result)
            return result

    async def direct_reports(self, worker_id: str) -> dict[str, Any]:
        return {"note": "Use find_worker to search by name, or get_team_time_off for leave info."}

    # ── Leave options discovery (cached 24h) ─────────────────────────

    async def get_leave_options(self, target_date: str | None = None) -> dict[str, Any]:
        cached = _cache_get("leave_options")
        if cached:
            return cached

        d = date.today()
        if target_date:
            try:
                d = date.fromisoformat(target_date)
            except (TypeError, ValueError):
                pass

        session_check = await auth.validate_session()
        if not session_check:
            raise auth.NotSignedIn("Session expired — call workday_login.")

        async with self._open_page() as pg:
            await self._navigate_to_absence(pg)
            await self._go_to_month(pg, d.year, d.month)

            cell_id = f"calendarDateCell-{d.month - 1}-{d.day}"
            cell = pg.locator(f'[data-automation-id="{cell_id}"]')
            if await cell.count() > 0:
                await cell.first.click(timeout=3000)
                await pg.wait_for_timeout(500)

            btn = pg.locator('[data-automation-id="wd-CommandButton_calendarSubmitButton"]')
            await btn.click(force=True, timeout=5000)
            await pg.wait_for_selector('[data-automation-id="promptIcon"]', timeout=8000)

            prompt_icon = pg.locator('[data-automation-id="promptIcon"]')
            await prompt_icon.first.click(timeout=3000)
            await pg.wait_for_selector('[data-automation-id="promptOption"]', timeout=5000)

            categories = await pg.evaluate("""() => {
                const cats = [];
                document.querySelectorAll('[data-automation-id="promptOption"]').forEach(el => {
                    if (el.offsetParent !== null) {
                        const t = el.textContent.trim();
                        if (t.includes('Absence') || t.includes('Leave') || t.includes('Time Off'))
                            cats.push(t);
                    }
                });
                return cats;
            }""")

            result = {"categories": []}
            for cat_name in categories:
                cat_entry = {"name": cat_name, "leave_types": []}
                cat_opt = pg.locator(f'[data-automation-id="promptOption"]:has-text("{cat_name}")')
                if await cat_opt.count() > 0:
                    await cat_opt.first.click(timeout=3000)
                    await pg.wait_for_selector('[data-automation-id="promptOption"]', timeout=5000)

                    sub_opts = await pg.evaluate("""() => {
                        const opts = [];
                        document.querySelectorAll('[data-automation-id="promptOption"]').forEach(el => {
                            if (el.offsetParent !== null) {
                                const t = el.textContent.trim();
                                if (!t.includes('Sonawane') && !t.includes('Absence'))
                                    opts.push(t);
                            }
                        });
                        return opts;
                    }""")
                    cat_entry["leave_types"] = sub_opts

                    back = pg.locator('[data-automation-id="backButton"]')
                    if await back.count() > 0:
                        await back.first.click(timeout=2000)
                        await pg.wait_for_timeout(500)

                result["categories"].append(cat_entry)

            cancel = pg.locator('[data-automation-id="cancelButton"]')
            if await cancel.count() > 0:
                await cancel.first.click(timeout=2000)

            _cache_set("leave_options", result)
            return result

    # ── Request absence (browser write operation) ────────────────────

    async def request_absence(
        self,
        start_date: str,
        end_date: str | None = None,
        absence_type: str = "Time Off",
        leave_plan: str = "",
        comments: str = "",
    ) -> dict[str, Any]:
        try:
            s = date.fromisoformat(start_date)
        except (TypeError, ValueError):
            raise WorkdayError("start_date must be YYYY-MM-DD.") from None
        if end_date:
            try:
                e = date.fromisoformat(end_date)
            except (TypeError, ValueError):
                raise WorkdayError("end_date must be YYYY-MM-DD.") from None
            if e < s:
                raise WorkdayError("end_date is before start_date.")
        else:
            e = s

        session_check = await auth.validate_session()
        if not session_check:
            raise auth.NotSignedIn("Session expired — call workday_login.")

        log.info("request_absence: %s to %s, type=%s, plan=%s", s, e, absence_type, leave_plan)

        async with self._open_page() as pg:
            await self._navigate_to_absence(pg)
            await self._go_to_month(pg, s.year, s.month)

            # Click start date
            cell_id = f"calendarDateCell-{s.month - 1}-{s.day}"
            cell = pg.locator(f'[data-automation-id="{cell_id}"]')
            if await cell.count() == 0:
                raise WorkdayError(f"Could not find calendar cell for {s.isoformat()}.")
            await cell.first.click(timeout=3000)
            await pg.wait_for_timeout(500)

            # Shift-click end date if multi-day same month
            if e != s and e.month == s.month:
                end_cell = pg.locator(f'[data-automation-id="calendarDateCell-{e.month - 1}-{e.day}"]')
                if await end_cell.count() > 0:
                    await end_cell.first.click(modifiers=["Shift"], timeout=3000)
                    await pg.wait_for_timeout(500)

            # Click "Request Absence"
            btn = pg.locator('[data-automation-id="wd-CommandButton_calendarSubmitButton"]')
            await btn.click(force=True, timeout=5000)
            await pg.wait_for_selector('[data-automation-id="promptIcon"]', timeout=8000)

            # Open Type of Absence dropdown
            prompt_icon = pg.locator('[data-automation-id="promptIcon"]')
            if await prompt_icon.count() == 0:
                raise WorkdayError("Type of Absence dropdown not found.")
            await prompt_icon.first.click(timeout=3000)
            await pg.wait_for_selector('[data-automation-id="promptOption"]', timeout=5000)

            # Select category
            type_label = "Time Off - Short-Term Absence"
            if "leave" in absence_type.lower() and "long" in absence_type.lower():
                type_label = "Leave - Long-Term Absence"

            cat = pg.locator(f'[data-automation-id="promptOption"]:has-text("{type_label}")')
            if await cat.count() == 0:
                raise WorkdayError(f"Category '{type_label}' not found.")
            await cat.first.click(timeout=3000)
            await pg.wait_for_selector('[data-automation-id="promptOption"]', timeout=5000)

            # Select leave plan
            if leave_plan:
                sub = pg.locator(f'[data-automation-id="promptOption"]:has-text("{leave_plan}")')
                if await sub.count() > 0:
                    await sub.first.click(timeout=3000)
                    log.info("Selected: %s", leave_plan)
                    await pg.wait_for_timeout(500)
                else:
                    search = pg.locator('[data-automation-id="searchBox"]')
                    if await search.count() > 0 and await search.first.is_visible():
                        await search.first.fill(leave_plan)
                        await pg.wait_for_timeout(1500)
                        filtered = pg.locator(f'[data-automation-id="promptOption"]:has-text("{leave_plan}")')
                        if await filtered.count() > 0:
                            await filtered.first.click(timeout=3000)
                            await pg.wait_for_timeout(500)
                        else:
                            raise WorkdayError(f"Leave plan '{leave_plan}' not found.")

            # Close dropdown
            close = pg.locator('[data-automation-id="closeButton"]')
            if await close.count() > 0 and await close.first.is_visible():
                await close.first.click(timeout=2000)
                await pg.wait_for_timeout(500)

            # Check for pre-submit errors
            errors = await pg.evaluate("""() => {
                const e = [];
                document.querySelectorAll('[data-automation-id*="error"], [data-automation-id*="Error"], [role="alert"]').forEach(el => {
                    if (el.offsetParent !== null) e.push(el.textContent.trim().substring(0, 200));
                });
                return e;
            }""")
            if errors:
                cancel = pg.locator('[data-automation-id="cancelButton"]')
                if await cancel.count() > 0:
                    await cancel.first.click(timeout=2000)
                return {"submitted": False, "error": "Form has errors", "errors": errors}

            # Submit
            submit = pg.locator('[data-automation-id="submitButton"]')
            if await submit.count() == 0:
                return {"submitted": False, "error": "Submit button not found."}
            await submit.first.click(timeout=5000)

            # Wait for result — look for either success or error
            try:
                await pg.wait_for_function(
                    """() => {
                        const t = document.body.innerText.toLowerCase();
                        return t.includes('submitted') || t.includes('successfully')
                            || t.includes('has been sent') || t.includes('error');
                    }""",
                    timeout=10000,
                )
            except Exception:
                pass

            page_text = await pg.evaluate("() => document.body.innerText.substring(0, 3000)")
            is_success = any(x in page_text.lower() for x in ("submitted", "successfully", "has been sent"))

            post_errors = await pg.evaluate("""() => {
                const e = [];
                document.querySelectorAll('[data-automation-id*="error"], [data-automation-id*="Error"], [role="alert"]').forEach(el => {
                    if (el.offsetParent !== null) e.push(el.textContent.trim().substring(0, 200));
                });
                return e;
            }""")

            if post_errors and not is_success:
                return {"submitted": False, "error": "Submission failed", "errors": post_errors}

            # Invalidate calendar caches
            for k in [k for k in _cache if k.startswith(("calendar:", "team:"))]:
                del _cache[k]

            days = (e - s).days + 1
            return {
                "submitted": True,
                "start_date": s.isoformat(),
                "end_date": e.isoformat(),
                "days": days,
                "absence_type": type_label,
                "leave_plan": leave_plan or "not specified",
                "message": f"Leave request submitted for {days} day(s) from {s.isoformat()} to {e.isoformat()}.",
            }

    # ── Utility ──────────────────────────────────────────────────────

    async def raw_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        path = (path or "").strip().lstrip("/")
        if not path or "://" in path:
            raise WorkdayError("Path must be relative.")
        return await self._api("GET", self._url(path), params=dict(params or {}))

    async def pre_warm(self) -> None:
        try:
            today = date.today()
            if _cache_get(f"team:{today.year}:{today.month}") is None:
                log.info("Pre-warming team data cache...")
                await self.team()
        except Exception as exc:
            log.info("Pre-warm skipped: %s", exc)

    def sign_out(self) -> None:
        self._me_cache = None
        cache_clear()
        auth.clear_session()

    async def shutdown(self) -> None:
        self._me_cache = None
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    @property
    def signed_in(self) -> bool:
        return auth.load_session() is not None
