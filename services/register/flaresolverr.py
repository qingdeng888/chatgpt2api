"""FlareSolverr integration for bypassing Cloudflare challenges.

FlareSolverr is a proxy server that solves Cloudflare's anti-bot challenges
using a headless browser. This module provides helpers to:
1. Pre-solve CF challenges and extract cookies/user-agent
2. Apply those cookies to curl_cffi sessions so subsequent requests pass through
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from curl_cffi import requests as cffi_requests


_lock = threading.Lock()
_cached_solution: dict[str, Any] | None = None
_cached_at: float = 0.0
_CACHE_TTL = 120  # seconds - CF cookies usually valid for ~5 min, refresh at 2 min


def solve_challenge(
    flaresolverr_url: str,
    target_url: str = "https://auth.openai.com",
    proxy: str = "",
    timeout: int = 60,
    session_id: str = "",
) -> dict[str, Any]:
    """Call FlareSolverr to solve a Cloudflare challenge.

    Returns dict with keys:
        - cookies: list of cookie dicts [{name, value, domain, path, ...}]
        - user_agent: str - the user agent used by the headless browser
        - status: str - "ok" or error info
    """
    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": target_url,
        "maxTimeout": timeout * 1000,
    }
    if proxy:
        payload["proxy"] = {"url": proxy}
    if session_id:
        payload["session"] = session_id

    try:
        resp = cffi_requests.post(
            f"{flaresolverr_url.rstrip('/')}/v1",
            json=payload,
            timeout=timeout + 10,
            verify=False,
        )
        data = resp.json() if resp.status_code == 200 else {}
    except Exception as e:
        return {"status": f"error: {e}", "cookies": [], "user_agent": ""}

    if not isinstance(data, dict):
        return {"status": "error: invalid response", "cookies": [], "user_agent": ""}

    status = str(data.get("status") or "").strip()
    solution = data.get("solution") or {}

    if status != "ok":
        message = str(data.get("message") or status)
        return {"status": f"error: {message}", "cookies": [], "user_agent": ""}

    cookies = solution.get("cookies") or []
    user_agent = str(solution.get("userAgent") or "").strip()

    return {
        "status": "ok",
        "cookies": cookies if isinstance(cookies, list) else [],
        "user_agent": user_agent,
        "response": solution.get("response") or "",
    }


def get_cf_clearance(
    flaresolverr_url: str,
    target_url: str = "https://auth.openai.com",
    proxy: str = "",
    timeout: int = 60,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """Get cached or fresh CF clearance cookies.

    Returns the solution dict or None on failure.
    Uses a simple TTL cache to avoid hammering FlareSolverr.
    """
    global _cached_solution, _cached_at

    if not flaresolverr_url:
        return None

    now = time.time()
    with _lock:
        if not force_refresh and _cached_solution and (now - _cached_at) < _CACHE_TTL:
            return _cached_solution

    result = solve_challenge(flaresolverr_url, target_url, proxy, timeout)

    if result.get("status") == "ok" and result.get("cookies"):
        with _lock:
            _cached_solution = result
            _cached_at = time.time()
        return result

    return None


def apply_cookies_to_session(
    session: Any,
    solution: dict[str, Any],
    domain: str = ".openai.com",
) -> None:
    """Apply FlareSolverr cookies to a curl_cffi session.

    Injects cf_clearance and other Cloudflare cookies into the session.
    """
    cookies = solution.get("cookies") or []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        cookie_domain = str(cookie.get("domain") or domain).strip()
        if name and value:
            # Apply to both with and without leading dot
            session.cookies.set(name, value, domain=cookie_domain)
            if cookie_domain.startswith("."):
                session.cookies.set(name, value, domain=cookie_domain[1:])
            else:
                session.cookies.set(name, value, domain=f".{cookie_domain}")


def get_solved_user_agent(solution: dict[str, Any] | None) -> str | None:
    """Extract user-agent from a FlareSolverr solution."""
    if not solution:
        return None
    ua = str(solution.get("user_agent") or "").strip()
    return ua if ua else None


def create_session_with_clearance(
    flaresolverr_url: str,
    proxy: str = "",
    target_url: str = "https://auth.openai.com",
    timeout: int = 60,
) -> tuple[Any, dict[str, Any] | None]:
    """Create a curl_cffi session pre-loaded with CF clearance cookies.

    Returns (session, solution) where solution is None if FlareSolverr failed.
    The session is always created regardless of FlareSolverr success.
    """
    solution = get_cf_clearance(flaresolverr_url, target_url, proxy, timeout)

    kwargs: dict[str, Any] = {"impersonate": "chrome", "verify": False}
    if proxy:
        kwargs["proxy"] = proxy
    session = cffi_requests.Session(**kwargs)

    if solution:
        apply_cookies_to_session(session, solution)

    return session, solution


def invalidate_cache() -> None:
    """Force cache invalidation (e.g., after detecting a new CF challenge)."""
    global _cached_solution, _cached_at
    with _lock:
        _cached_solution = None
        _cached_at = 0.0



def check_health(flaresolverr_url: str, timeout: int = 10) -> tuple[bool, str]:
    """Check if FlareSolverr service is reachable and responding.

    Returns (ok, message) tuple.
    """
    if not flaresolverr_url:
        return False, "FlareSolverr URL 未配置"

    try:
        resp = cffi_requests.get(
            f"{flaresolverr_url.rstrip('/')}/health",
            timeout=timeout,
            verify=False,
        )
        if resp.status_code == 200:
            return True, "FlareSolverr 服务正常"
    except Exception:
        pass

    # Some FlareSolverr versions don't have /health, try /v1 with a simple test
    try:
        resp = cffi_requests.post(
            f"{flaresolverr_url.rstrip('/')}/v1",
            json={"cmd": "sessions.list"},
            timeout=timeout,
            verify=False,
        )
        if resp.status_code == 200:
            return True, "FlareSolverr 服务正常"
        return False, f"FlareSolverr 返回异常状态码: {resp.status_code}"
    except Exception as e:
        return False, f"FlareSolverr 连接失败: {e}"
