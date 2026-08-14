from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests

from services.account_service import account_service
from services.register import mail_provider
from services.register import flaresolverr
from utils.sentinel import (
    build_sentinel_token as _build_sentinel_token,
    build_sentinel_headers_with_sdk as _build_sentinel_headers_with_sdk,
)

base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "providers": [],
    },
    "proxy": "",
    "proxy_list": [],
    "total": 10,
    "threads": 3,
    "flaresolverr_url": "",
    "interval_min": 0,
    "interval_max": 0,
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
try:
    saved_config = json.loads(register_config_file.read_text(encoding="utf-8"))
    config.update({key: saved_config[key] for key in ("mail", "proxy", "proxy_list", "total", "threads", "flaresolverr_url", "interval_min", "interval_max") if key in saved_config})
except Exception:
    pass

# ─── Proxy pool rotation ─────────────────────────────────────────────────────
_proxy_pool_lock = threading.Lock()
_proxy_pool_index = 0


def _normalize_proxy_scheme(proxy: str) -> str:
    """Convert socks5:// to socks5h:// so DNS resolution happens on the proxy side.

    With plain socks5:// curl resolves the hostname locally and hands the proxy an
    IP address. Datacenter/residential proxies often reject or fail to route those
    resolved IPs (curl err 97 host unreachable) even though the proxy itself can
    reach the same host by name. socks5h:// sends the hostname to the proxy and lets
    it resolve, matching how a real browser behaves. This also avoids leaking DNS
    queries to the local resolver.
    """
    proxy = str(proxy or "").strip()
    if proxy.lower().startswith("socks5://"):
        proxy = "socks5h://" + proxy[len("socks5://"):]
    return proxy


def _parse_proxy_list() -> list[str]:
    """Parse proxy_list from config, supporting both list and newline-separated string."""
    raw = config.get("proxy_list") or []
    if isinstance(raw, str):
        raw = [line.strip() for line in raw.splitlines() if line.strip()]
    elif isinstance(raw, list):
        raw = [str(item).strip() for item in raw if str(item).strip()]
    else:
        raw = []
    # Also include the legacy single proxy field if proxy_list is empty
    if not raw:
        single = str(config.get("proxy") or "").strip()
        if single:
            raw = [single]
    return [_normalize_proxy_scheme(item) for item in raw]


def _pick_proxy() -> str:
    """Round-robin pick a proxy from the proxy list."""
    global _proxy_pool_index
    proxies = _parse_proxy_list()
    if not proxies:
        return ""
    with _proxy_pool_lock:
        proxy = proxies[_proxy_pool_index % len(proxies)]
        _proxy_pool_index = (_proxy_pool_index + 1) % len(proxies)
    return proxy


def _detect_real_ip(proxy: str = "", timeout: int = 10) -> str:
    """Detect the real outbound IP address via a public API.

    Tries multiple IP detection services for reliability.
    Returns IP string or raises RuntimeError if detection fails.
    """
    ip_services = [
        "https://api.ipify.org?format=json",
        "https://httpbin.org/ip",
        "https://ipinfo.io/json",
    ]
    kwargs: dict[str, Any] = {"impersonate": "chrome", "verify": False, "timeout": timeout}
    if proxy:
        kwargs["proxy"] = _normalize_proxy_scheme(proxy)

    last_error = ""
    for url in ip_services:
        try:
            resp = requests.get(url, **kwargs)
            if resp.status_code == 200:
                data = resp.json() if "json" in str(resp.headers.get("content-type", "")) else {}
                ip = str(data.get("ip") or data.get("origin") or "").strip()
                if not ip:
                    # Try plain text response
                    ip = resp.text.strip()
                if ip:
                    return ip
        except Exception as e:
            last_error = str(e)
            continue

    raise RuntimeError(f"代理不可用或无法获取出口IP: {last_error}")


def _validate_proxy_and_get_ip(proxy: str, index: int) -> str:
    """Validate proxy is working and return the real IP. Raises on failure."""
    if not proxy:
        # No proxy configured, get direct IP
        try:
            ip = _detect_real_ip("")
            return ip
        except Exception:
            return "direct(unknown)"

    try:
        ip = _detect_real_ip(proxy)
        return ip
    except Exception as e:
        raise RuntimeError(f"代理验证失败 [{proxy}]: {e}")

auth_base = "https://auth.openai.com"
chatgpt_base = "https://chatgpt.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
sec_ch_ua_full_version_list = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None


class RegistrationStopped(Exception):
    """Raised when a registration was cancelled mid-flight (stop_event set)."""


common_headers = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": auth_base,
    "priority": "u=1, i",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _response_debug_detail(resp, limit: int = 800) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    parts = [
        f"url={str(getattr(resp, 'url', '') or '')[:300]}",
        f"content_type={str(getattr(resp, 'headers', {}).get('content-type') or '')}",
    ]
    for key in ("cf-ray", "x-request-id", "openai-processing-ms"):
        value = str(getattr(resp, "headers", {}).get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if data:
        parts.append(f"json={json.dumps(data, ensure_ascii=False)[:limit]}")
    else:
        parts.append(f"body={str(getattr(resp, 'text', '') or '')[:limit]}")
    return ", ".join(parts)


def _is_cloudflare_challenge(resp) -> bool:
    """判定是否为真实的 CF JS 交互式 challenge（不是普通带 CF 头的 200 响应）。

    所有 auth.openai.com 响应都带 `server: cloudflare`，若仅凭该头判定，
    会把限流/error 页误判为 challenge。真实 challenge 的特征是：
    cf_chl_ 指纹、challenge 专用域名、或带 challenge 脚本的 "Just a moment"。
    """
    if resp is None:
        return False
    status = int(getattr(resp, "status_code", 0) or 0)
    text = str(getattr(resp, "text", "") or "").lower()
    headers = getattr(resp, "headers", {}) or {}
    content_type = str(headers.get("content-type") or "").lower()
    # 已成功（200）且 content-type 不是 HTML 的：不是 challenge
    if status == 200 and "text/html" not in content_type:
        return False
    if "challenges.cloudflare.com" in text:
        return True
    if "<title>just a moment" in text or "just a moment" == _strip_title(text) or "cf_chl_" in text:
        return True
    # server 头含 cloudflare 且状态为 429/503 或被 challenge 标记
    if status in (403, 429, 503) and "cloudflare" in (str(headers.get("server") or "").lower()):
        return True
    return False


def _strip_title(text: str) -> str:
    m = re.search(r"<title[^>]*>([^<]*)</title>", str(text or ""), re.I)
    if not m:
        return ""
    return str(m.group(1) or "").strip().lower()


def _url_path(url: str) -> str:
    try:
        return urlparse(str(url or "")).path
    except Exception:
        return ""


def _extract_continue_url(data: dict | None) -> str:
    if not isinstance(data, dict):
        return ""
    value = str(data.get("continue_url") or "").strip()
    if value:
        return value
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    return str(page.get("continue_url") or "").strip()


def create_mailbox(username: str | None = None) -> dict:
    return mail_provider.create_mailbox(config["mail"], username)


def wait_for_code(mailbox: dict) -> str | None:
    return mail_provider.wait_for_code(config["mail"], mailbox)


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> str:
    """返回 openai-sentinel-token header 值（兼容旧调用，忽略 oai-sc cookie）。"""
    value, _ = _build_sentinel_token(session, device_id, flow)
    return value


def build_sentinel_token_with_cookie(session: requests.Session, device_id: str, flow: str) -> tuple[str, str]:
    """返回 (openai-sentinel-token header 值, oai-sc cookie 值)，供新流程设置浏览器 cookie。"""
    return _build_sentinel_token(session, device_id, flow)


def create_session(proxy: str = "") -> Any:
    proxy = _normalize_proxy_scheme(proxy)
    flaresolverr_url = str(config.get("flaresolverr_url") or "").strip()
    if flaresolverr_url:
        session, solution = flaresolverr.create_session_with_clearance(
            flaresolverr_url=flaresolverr_url,
            proxy=proxy,
            target_url=f"{auth_base}/authorize",
        )
        if solution:
            log("FlareSolverr: 已预加载 CF clearance cookies", "green")
        else:
            log("FlareSolverr: 未能获取 clearance，将使用普通会话", "yellow")
        return session
    kwargs = {"impersonate": "chrome", "verify": False}
    if proxy:
        kwargs["proxy"] = _normalize_proxy_scheme(proxy)
    return requests.Session(**kwargs)


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        try:
            return session.request(method.upper(), url, timeout=default_timeout, **kwargs), ""
        except Exception as error:
            last_error = str(error)
            time.sleep(1)
    return None, last_error


def _otp_error_code(resp) -> str:
    data = _response_json(resp) if resp is not None else {}
    error = data.get("error") if isinstance(data, dict) else None
    return str(error.get("code") or "").strip() if isinstance(error, dict) else ""


def _dump_mailbox_for_diagnosis(mailbox: dict[str, Any], index: int) -> None:
    """验证码等待超时诊断：dump 收件箱内容，区分「邮件未投递」vs「邮件到达但被基线/ref 吞」."""
    try:
        address = str(mailbox.get("address") or "?")
        step(index, f"[诊断] 验证码超时，邮箱 {address} 内容:")
        prov = mail_provider._create_provider(config.get("mail") or {})
        messages = []
        fetch_m = getattr(prov, "fetch_recent_messages", None)
        if callable(fetch_m):
            try:
                messages = fetch_m(mailbox) or []
            except Exception:
                messages = []
        if not messages:
            latest = getattr(prov, "fetch_latest_message", None)
            if callable(latest):
                try:
                    latest_msg = latest(mailbox)
                    messages = [latest_msg] if latest_msg else []
                except Exception:
                    messages = []
        try:
            prov.close()
        except Exception:
            pass
        if not messages:
            step(index, "[诊断] 收件箱为空（邮件完全未投递到 inbucket，或 inbucket 无法读取）", "yellow")
            return
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            subject = str(msg.get("subject") or "")
            try:
                ref = mail_provider._message_tracking_ref(msg)
            except Exception:
                ref = "?"
            seen_refs = set(mailbox.get("_seen_code_message_refs") or [])
            hit_seen = ref in seen_refs
            try:
                code = str(mail_provider._extract_code(msg) or "")
            except Exception:
                code = ""
            step(
                index,
                f"[诊断] subject={subject[:50]} code={code or '无'} ref_hit_seen={hit_seen} ref={str(ref)[:40]}",
                "yellow",
            )
    except Exception as exc:
        step(index, f"[诊断] 邮箱内容读取失败: {str(exc)[:160]}", "yellow")


def validate_otp(session: requests.Session, device_id: str, code: str):
    headers = dict(common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    if _otp_error_code(resp) in {"wrong_email_otp_code", "email_otp_invalid", "invalid_code"}:
        return resp, error
    headers["openai-sentinel-token"] = build_sentinel_token(session, device_id, "authorize_continue")
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def request_platform_oauth_token(session: requests.Session, code: str, code_verifier: str) -> dict | None:
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "auth0-client": platform_auth0_client,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": platform_base,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{platform_base}/",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": user_agent,
    }
    resp = session.post(
        f"{auth_base}/api/accounts/oauth/token",
        headers=headers,
        json={
            "client_id": platform_oauth_client_id,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": platform_oauth_redirect_uri,
        },
        verify=False,
        timeout=60,
    )
    if resp.status_code != 200:
        print(resp.text)
        return None
    return _response_json(resp)


def extract_oauth_callback_params_from_consent_session(session: requests.Session, consent_url: str, device_id: str) -> dict[str, str] | None:
    if consent_url.startswith("/"):
        consent_url = f"{auth_base}{consent_url}"
    current_url = consent_url
    for _ in range(10):
        response = session.get(current_url, headers=navigate_headers, verify=False, timeout=30, allow_redirects=False)
        callback_params = extract_oauth_callback_params_from_url(str(response.url)) or extract_oauth_callback_params_from_url(str(response.headers.get("Location") or "").strip())
        if callback_params:
            return callback_params
        location = str(response.headers.get("Location") or "").strip()
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            break
        current_url = f"{auth_base}{location}" if location.startswith("/") else location
    raw = session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or session.cookies.get("oai-client-auth-session")
    if not raw:
        return None
    try:
        first_part = raw.split(".")[0]
        padding = 4 - len(first_part) % 4
        if padding != 4:
            first_part += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(first_part))
        workspace_id = payload["workspaces"][0]["id"]
    except Exception:
        return None
    headers = dict(common_headers)
    headers["referer"] = consent_url
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    ws_resp = session.post(f"{auth_base}/api/accounts/workspace/select", json={"workspace_id": workspace_id}, headers=headers, verify=False, timeout=30, allow_redirects=False)
    callback_params = extract_oauth_callback_params_from_url(str(ws_resp.headers.get("Location") or "").strip())
    if callback_params:
        return callback_params
    ws_data = _response_json(ws_resp)
    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
    if not orgs:
        return None
    org_id = str((orgs[0] or {}).get("id") or "").strip()
    project_id = str(((orgs[0] or {}).get("projects") or [{}])[0].get("id") or "").strip()
    if not org_id:
        return None
    org_headers = dict(common_headers)
    org_headers["referer"] = str(ws_data.get("continue_url") or consent_url)
    org_headers["oai-device-id"] = device_id
    org_headers.update(_make_trace_headers())
    body = {"org_id": org_id}
    if project_id:
        body["project_id"] = project_id
    org_resp = session.post(f"{auth_base}/api/accounts/organization/select", json=body, headers=org_headers, verify=False, timeout=30, allow_redirects=False)
    return extract_oauth_callback_params_from_url(str(org_resp.headers.get("Location") or "").strip())


def exchange_platform_tokens(session: requests.Session, device_id: str, code_verifier: str, consent_url: str) -> dict | None:
    callback_params = extract_oauth_callback_params_from_consent_session(session, consent_url, device_id)
    if not callback_params:
        # 回退方案：直接导航 consent URL（allow_redirects=True），从最终 URL 提取 code
        print(f"[exchange_platform_tokens] 主方案失败，尝试回退方案, continue_url={consent_url[:120]}")
        try:
            r = session.get(consent_url, headers=navigate_headers, allow_redirects=True, verify=False, timeout=30)
            final_url = str(r.url)
            print(f"[exchange_platform_tokens] 回退 final_url={final_url[:120]}")
            callback_params = extract_oauth_callback_params_from_url(final_url)
            if not callback_params:
                for hist in getattr(r, "history", []) or []:
                    loc = str(hist.headers.get("Location") or "")
                    callback_params = extract_oauth_callback_params_from_url(loc)
                    if callback_params:
                        break
        except Exception as e:
            print(f"[exchange_platform_tokens] 回退方案异常: {e}")
    if not callback_params:
        print("[exchange_platform_tokens] 所有方案均无法提取 OAuth code")
        return None
    code = str(callback_params.get("code") or "").strip()
    if not code:
        return None
    resp = create_session(config["proxy"]).post(
        f"{auth_base}/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": platform_oauth_redirect_uri,
        },
        verify=False,
        timeout=60,
    )
    if resp.status_code != 200:
        print(resp.text)
        return None
    return _response_json(resp)


class PlatformRegistrar:
    def __init__(self, proxy: str = "", stop_event: Any = None, mail_config: dict | None = None) -> None:
        self.proxy = proxy
        self.stop_event = stop_event
        self.mail_config = mail_config or config.get("mail") or {}
        self.session = create_session(proxy)
        self.device_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.code_verifier = ""
        self.platform_auth_code = ""
        self.chatgpt_callback_url = ""
        self.chatgpt_authorize_landed_path = ""
        self.password_sentinel_token = ""
        self.authorize_sentinel_token = ""
        self.signup_verification_mode = ""
        self.clearance_user_agent = ""

    def _ensure_active(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise RegistrationStopped()

    def close(self) -> None:
        self.session.close()

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(navigate_headers)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _otp_fetch_headers(self) -> dict[str, str]:
        headers = {
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "en-US,en;q=0.9",
            # curl_cffi otherwise labels an empty POST as form-urlencoded;
            # the browser omits this header, but the API rejects curl's default.
            "content-type": "application/json",
            "origin": auth_base,
            "priority": "u=1, i",
            "referer": f"{auth_base}/email-verification",
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-arch": '"x86_64"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"10.0.0"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self._browser_user_agent(),
        }
        headers.update(_make_trace_headers())
        return headers

    def _browser_user_agent(self) -> str:
        return user_agent

    def _browser_sec_ch_ua(self) -> str:
        return sec_ch_ua

    def _build_sentinel_token(self, flow: str) -> str:
        last = ""
        for attempt in range(2):
            try:
                return build_sentinel_token_with_cookie(self.session, self.device_id, flow)[0]
            except Exception as exc:
                last = str(exc)
                if attempt == 0:
                    time.sleep(1.0)
        raise RuntimeError(f"build_sentinel_token_failed: {last}")

    def _build_sentinel_headers(self, flow: str):
        last = ""
        for attempt in range(2):
            try:
                return _build_sentinel_headers_with_sdk(
                    self.session,
                    self.device_id,
                    flow,
                    user_agent=self._browser_user_agent(),
                    sec_ch_ua=self._browser_sec_ch_ua(),
                )
            except Exception as exc:
                last = str(exc)
                if attempt == 0:
                    time.sleep(1.5)
        raise RuntimeError(f"build_sentinel_headers_failed: {last}")

    def _platform_authorize(self, email: str, index: int, *, screen_hint: str = "signup") -> None:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        self.code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            # 注册流程显式声明 signup：throwaway 域名 OpenAI 会自动当新账号走注册，
            # 但 @outlook.com/@hotmail.com 这类真实消费邮箱会被 login_or_signup 路由到登录分支，
            # 后续 user/register 落在错误的 auth step 上报 invalid_auth_step。
            "screen_hint": screen_hint,
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        authorize_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
        resp, error = request_with_local_retry(self.session, "get", authorize_url, headers=self._navigate_headers(f"{platform_base}/"), allow_redirects=True, verify=False)

        # If CF challenge detected, try FlareSolverr to get clearance then retry
        if (resp is None or resp.status_code != 200) and _is_cloudflare_challenge(resp):
            flaresolverr_url = str(config.get("flaresolverr_url") or "").strip()
            if flaresolverr_url:
                step(index, "检测到 Cloudflare 拦截，正在通过 FlareSolverr 过盾...", "yellow")
                flaresolverr.invalidate_cache()
                solution = flaresolverr.get_cf_clearance(
                    flaresolverr_url=flaresolverr_url,
                    target_url=authorize_url,
                    proxy=_normalize_proxy_scheme(config["proxy"]),
                    force_refresh=True,
                )
                if solution and solution.get("status") == "ok":
                    flaresolverr.apply_cookies_to_session(self.session, solution, domain=".openai.com")
                    step(index, "FlareSolverr 过盾成功，重试 authorize 请求", "green")
                    resp, error = request_with_local_retry(self.session, "get", authorize_url, headers=self._navigate_headers(f"{platform_base}/"), allow_redirects=True, verify=False)
                else:
                    err_msg = solution.get("status", "unknown") if solution else "no response"
                    step(index, f"FlareSolverr 过盾失败: {err_msg}", "red")

        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            if _is_cloudflare_challenge(resp):
                raise RuntimeError("被 Cloudflare 拦截，FlareSolverr 未能解决或未配置，请检查 flaresolverr_url 设置")
            debug = _response_debug_detail(resp)
            status = getattr(resp, "status_code", "unknown")
            raise RuntimeError(error or f"platform_authorize_http_{status}{detail}, {debug}")
        self.platform_auth_code = self._oauth_code_from_response(resp)
        if not self.platform_auth_code:
            body = str(getattr(resp, "text", "") or "")
            match = re.search(r"[?&]code=([A-Za-z0-9._~+/\-]+)", body) or re.search(
                r'"code"\s*:\s*"([^"]+)"', body
            )
            if match:
                self.platform_auth_code = str(match.group(1) or "").strip()
        if screen_hint == "login" and not self.platform_auth_code:
            raise RuntimeError(f"platform_authorize_missing_code: {_response_debug_detail(resp)}")
        step(index, f"platform authorize 完成 url={str(getattr(resp, 'url', '') or '')[:160]}")

    @staticmethod
    def _oauth_code_from_response(resp) -> str:
        candidates: list[str] = []
        for item in [*(getattr(resp, "history", None) or []), resp]:
            candidates.append(str(getattr(item, "url", "") or ""))
            location = str((getattr(item, "headers", {}) or {}).get("location") or "")
            if location:
                candidates.append(location)
        for candidate in reversed(candidates):
            params = extract_oauth_callback_params_from_url(candidate)
            if params and params.get("code"):
                return str(params["code"])
        return ""

    def _refresh_cloudflare_clearance(self, target_url: str, index: int) -> Any:
        """Attempt a Cloudflare clearance refresh via FlareSolverr (if configured)."""
        flaresolverr_url = str(config.get("flaresolverr_url") or "").strip()
        if not flaresolverr_url:
            return None
        step(index, "检测到 Cloudflare 拦截，尝试通过 FlareSolverr 刷新 clearance", "yellow")
        try:
            flaresolverr.invalidate_cache()
            solution = flaresolverr.get_cf_clearance(
                flaresolverr_url=flaresolverr_url,
                target_url=target_url,
                proxy=_normalize_proxy_scheme(config["proxy"]),
                force_refresh=True,
            )
        except Exception as exc:
            step(index, f"FlareSolverr 刷新 clearance 异常: {str(exc)[:180]}", "yellow")
            return None
        if not solution or solution.get("status") != "ok":
            step(index, "FlareSolverr 刷新 clearance 失败", "yellow")
            return None
        flaresolverr.apply_cookies_to_session(self.session, solution, domain=".openai.com")
        ua = str((solution.get("user_agent") or "")).strip()
        if ua:
            self.clearance_user_agent = ua
        step(index, "FlareSolverr clearance 刷新完成，重试请求", "yellow")
        return solution

    def _boot_chatgpt_session(self, index: int) -> None:
        step(index, "开始初始化 ChatGPT 会话")
        resp, error = request_with_local_retry(
            self.session,
            "get",
            chatgpt_base,
            headers=self._navigate_headers(),
            allow_redirects=True,
            verify=False,
        )
        if (resp is None or resp.status_code != 200) and _is_cloudflare_challenge(resp):
            self._refresh_cloudflare_clearance(chatgpt_base, index)
            resp, error = request_with_local_retry(
                self.session,
                "get",
                chatgpt_base,
                headers=self._navigate_headers(),
                allow_redirects=True,
                verify=False,
            )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"chatgpt_boot_http_{getattr(resp, 'status_code', 'unknown')}")
        cookies = self.session.cookies.get_dict()
        self.device_id = str(cookies.get("oai-did") or self.device_id)
        step(index, "ChatGPT 会话初始化完成")

    def _chatgpt_authorize(self, email: str, index: int, *, include_login_hint: bool = False) -> None:
        self._ensure_active()
        self._boot_chatgpt_session(index)
        csrf_url = f"{chatgpt_base}/api/auth/csrf"
        csrf_browser_headers = {
            "accept": "application/json",
            "referer": f"{chatgpt_base}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self._browser_user_agent(),
        }
        csrf_resp, error = request_with_local_retry(
            self.session,
            "get",
            csrf_url,
            headers=csrf_browser_headers,
            verify=False,
        )
        csrf_data = _response_json(csrf_resp) if csrf_resp is not None else {}
        csrf_token = str(csrf_data.get("csrfToken") or "").strip()
        if csrf_resp is None or csrf_resp.status_code != 200 or not csrf_token:
            raise RuntimeError(error or f"chatgpt_csrf_http_{getattr(csrf_resp, 'status_code', 'unknown')}")

        query_params = {
            "prompt": "login",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": self.session_id,
            "ext-passkey-client-capabilities": "0111",
            "screen_hint": "login_or_signup",
        }
        if include_login_hint:
            query_params["login_hint"] = email
        query = urlencode(query_params)
        signin_url = f"{chatgpt_base}/api/auth/signin/openai?{query}"
        signin_headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded",
            "origin": chatgpt_base,
            "referer": f"{chatgpt_base}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self._browser_user_agent(),
        }
        signin_resp, error = request_with_local_retry(
            self.session,
            "post",
            signin_url,
            data={"callbackUrl": f"{chatgpt_base}/", "csrfToken": csrf_token, "json": "true"},
            headers=signin_headers,
            allow_redirects=False,
            verify=False,
        )
        signin_data = _response_json(signin_resp) if signin_resp is not None else {}
        authorize_url = str(signin_data.get("url") or "").strip()
        if signin_resp is None or signin_resp.status_code != 200 or not authorize_url:
            raise RuntimeError(error or f"chatgpt_signin_http_{getattr(signin_resp, 'status_code', 'unknown')}")

        def authorize():
            return request_with_local_retry(
                self.session,
                "get",
                authorize_url,
                headers=self._navigate_headers(f"{chatgpt_base}/auth/login"),
                allow_redirects=True,
                verify=False,
            )

        resp, error = authorize()
        if (resp is None or resp.status_code != 200) and _is_cloudflare_challenge(resp):
            self._refresh_cloudflare_clearance(auth_base, index)
            resp, error = authorize()
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"chatgpt_authorize_http_{getattr(resp, 'status_code', 'unknown')}")
        self.chatgpt_authorize_landed_path = str(_url_path(getattr(resp, "url", "") or ""))
        parsed_authorize = urlparse(authorize_url)
        try:
            cookie_device_id = str(self.session.cookies.get("oai-did", "") or "").strip()
        except Exception:
            cookie_device_id = ""
        query_device_id = str((parse_qs(parsed_authorize.query).get("device_id") or [""])[0]).strip()
        self.device_id = cookie_device_id or query_device_id or self.device_id
        for domain in (".auth.openai.com", "auth.openai.com"):
            try:
                self.session.cookies.set("oai-did", self.device_id, domain=domain)
            except Exception:
                continue
        step(index, f"ChatGPT authorize 完成 url={str(getattr(resp, 'url', '') or '')[:160]}")

    def _follow_authorize_continue(self, continue_url: str, referer: str, index: int) -> None:
        target_url = str(continue_url or "").strip()
        if not target_url:
            return
        if target_url.startswith("/"):
            target_url = f"{auth_base}{target_url}"
        step(index, "开始 authorize continue")
        resp, error = request_with_local_retry(
            self.session,
            "get",
            target_url,
            headers=self._navigate_headers(referer),
            allow_redirects=True,
            verify=False,
        )
        if (resp is None or resp.status_code not in (200, 302)) and _is_cloudflare_challenge(resp):
            self._refresh_cloudflare_clearance(auth_base, index)
            resp, error = request_with_local_retry(
                self.session,
                "get",
                target_url,
                headers=self._navigate_headers(referer),
                allow_redirects=True,
                verify=False,
            )
        if resp is None or resp.status_code not in (200, 302):
            debug = _response_debug_detail(resp)
            raise RuntimeError(error or f"authorize_continue_http_{getattr(resp, 'status_code', 'unknown')}, {debug}")
        step(index, f"authorize continue 完成 url={str(getattr(resp, 'url', '') or '')[:160]}")

    def _authorize_signup(self, email: str, index: int, *, screen_hint: str = "signup") -> tuple[str, str]:
        self._ensure_active()
        step(index, "提交 ChatGPT 注册邮箱")
        url = f"{auth_base}/api/accounts/authorize/continue"

        def submit():
            sentinel_token = self._build_sentinel_token("authorize_continue")
            self.authorize_sentinel_token = sentinel_token
            headers = self._json_headers(f"{auth_base}/create-account")
            headers["openai-sentinel-token"] = sentinel_token
            return request_with_local_retry(
                self.session,
                "post",
                url,
                json={"username": {"value": email, "kind": "email"}, "screen_hint": screen_hint},
                headers=headers,
                allow_redirects=False,
                verify=False,
            )

        resp, error = submit()
        if (resp is None or resp.status_code != 200) and _is_cloudflare_challenge(resp):
            self._refresh_cloudflare_clearance(auth_base, index)
            resp, error = submit()
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"authorize_signup_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(resp, 400)}"
            )

        data = _response_json(resp)
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        page_type = str(page.get("type") or "").strip()
        continue_url = _extract_continue_url(data)
        page_mode = {
            "create_account_password": "password",
            "email_otp_verification": "otp",
        }.get(page_type)
        continue_mode = {
            "/create-account/password": "password",
            "/email-verification": "otp",
        }.get(_url_path(continue_url))
        if page_mode and continue_mode and page_mode != continue_mode:
            raise RuntimeError(
                f"authorize_signup_state_conflict: page_type={page_type}, continue_path={_url_path(continue_url)}"
            )
        mode = page_mode or continue_mode
        if mode not in {"password", "otp"}:
            raise RuntimeError(
                f"authorize_signup_unknown_state: page_type={page_type or '?'}, "
                f"continue_path={_url_path(continue_url) or '?'}"
            )

        verification_mode = str(payload.get("email_verification_mode") or "").strip().lower()
        self.signup_verification_mode = verification_mode
        if mode == "password":
            self._follow_authorize_continue(
                continue_url or f"{auth_base}/create-account/password",
                f"{auth_base}/create-account",
                index,
            )
        step(
            index,
            f"ChatGPT 注册邮箱状态 mode={mode}, verification_mode={verification_mode or 'none'}",
        )
        return mode, verification_mode

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        url = f"{auth_base}/api/accounts/user/register"
        headers = self._json_headers(f"{auth_base}/create-account/password")
        self.password_sentinel_token = self._build_sentinel_token("username_password_create")
        headers["openai-sentinel-token"] = self.password_sentinel_token
        resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=False)
        if (resp is None or resp.status_code != 200) and _is_cloudflare_challenge(resp):
            self._refresh_cloudflare_clearance(auth_base, index)
            headers = self._json_headers(f"{auth_base}/create-account/password")
            self.password_sentinel_token = self._build_sentinel_token("username_password_create")
            headers["openai-sentinel-token"] = self.password_sentinel_token
            resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=False)
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        data = _response_json(resp)
        self._follow_authorize_continue(str(data.get("continue_url") or "").strip(), f"{auth_base}/create-account/password", index)
        step(index, "提交注册密码完成")

    def _send_otp(self, index: int, mailbox: dict | None = None) -> None:
        self._ensure_active()
        step(index, "开始发送验证码")
        if mailbox is not None:
            try:
                mail_provider.prepare_code_baseline(self.mail_config, mailbox)
                step(index, "发送验证码前邮箱基线已记录")
            except Exception as exc:
                step(index, f"邮箱基线记录失败，继续发送验证码: {str(exc)[:160]}", "yellow")
        url = f"{auth_base}/api/accounts/email-otp/send"
        if not self.password_sentinel_token:
            raise RuntimeError("send_otp_missing_password_sentinel")
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = self.password_sentinel_token
        resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=False)
        if (resp is None or resp.status_code not in (200, 302)) and _is_cloudflare_challenge(resp):
            self._refresh_cloudflare_clearance(auth_base, index)
            headers = self._json_headers(f"{auth_base}/create-account/password")
            headers["openai-sentinel-token"] = self.password_sentinel_token
            resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(self, code: str, index: int) -> None:
        step(index, f"开始校验验证码 {code}")
        resp, error = validate_otp(self.session, self.device_id, code)
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        step(index, "验证码校验完成")

    def _create_account(self, name: str, birthdate: str, index: int) -> None:
        step(index, "开始创建账号资料")
        url = f"{auth_base}/api/accounts/create_account"
        headers = self._json_headers(f"{auth_base}/about-you")
        sentinel_headers = self._build_sentinel_headers("oauth_create_account")
        if not sentinel_headers.so_token:
            raise RuntimeError(f"create_account_missing_sentinel_so_token: {sentinel_headers.log_summary()}")
        headers.update(sentinel_headers.as_headers())
        step(index, f"create_account sentinel: {json.dumps(sentinel_headers.log_summary(), ensure_ascii=False)}")
        resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if (resp is None or resp.status_code not in (200, 302)) and _is_cloudflare_challenge(resp):
            self._refresh_cloudflare_clearance(auth_base, index)
            headers = self._json_headers(f"{auth_base}/about-you")
            sentinel_headers = self._build_sentinel_headers("oauth_create_account")
            if not sentinel_headers.so_token:
                raise RuntimeError(f"create_account_missing_sentinel_so_token: {sentinel_headers.log_summary()}")
            headers.update(sentinel_headers.as_headers())
            step(index, f"create_account sentinel retry: {json.dumps(sentinel_headers.log_summary(), ensure_ascii=False)}")
            resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        data = _response_json(resp)
        self.chatgpt_callback_url = str(data.get("continue_url") or "").strip()
        callback_params = extract_oauth_callback_params_from_url(self.chatgpt_callback_url)
        self.platform_auth_code = str((callback_params or {}).get("code") or "").strip()
        step(index, "创建账号资料完成")

    def _finish_chatgpt_registration(self, index: int) -> dict[str, str]:
        callback_url = str(self.chatgpt_callback_url or "").strip()
        if not callback_url:
            raise RuntimeError("chatgpt_callback_url_missing")
        resp, error = request_with_local_retry(
            self.session,
            "get",
            callback_url,
            headers=self._navigate_headers(f"{auth_base}/about-you"),
            allow_redirects=True,
            verify=False,
        )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"chatgpt_callback_http_{getattr(resp, 'status_code', 'unknown')}")

        session_url = f"{chatgpt_base}/api/auth/session"
        session_headers = {
            "accept": "application/json",
            "referer": f"{chatgpt_base}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self._browser_user_agent(),
        }
        session_resp, error = request_with_local_retry(
            self.session,
            "get",
            session_url,
            headers=session_headers,
            verify=False,
        )
        data = _response_json(session_resp) if session_resp is not None else {}
        access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
        if session_resp is None or session_resp.status_code != 200 or not access_token:
            raise RuntimeError(error or f"chatgpt_session_http_{getattr(session_resp, 'status_code', 'unknown')}")
        cookies = self.session.cookies.get_dict()
        cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items() if name and value)
        step(index, f"ChatGPT session token 获取完成 token_len={len(access_token)} cookie_count={len(cookies)}")
        return {
            "access_token": access_token,
            "session_token": str(data.get("sessionToken") or data.get("session_token") or "").strip(),
            "cookie": cookie_header,
        }

    def _account_environment(self) -> dict[str, Any]:
        """Capture the browser and egress identity used by this registration."""
        headers = {
            str(key).lower(): str(value)
            for key, value in self._json_headers(f"{auth_base}/").items()
        }
        fp_keys = (
            "user-agent",
            "accept-language",
            "sec-ch-ua",
            "sec-ch-ua-arch",
            "sec-ch-ua-bitness",
            "sec-ch-ua-full-version-list",
            "sec-ch-ua-mobile",
            "sec-ch-ua-model",
            "sec-ch-ua-platform",
            "sec-ch-ua-platform-version",
        )
        fingerprint = {key: headers[key] for key in fp_keys if headers.get(key)}
        chrome_version = re.search(r"Chrome/([0-9.]+)", fingerprint.get("user-agent", ""))
        if chrome_version:
            fingerprint["sec-ch-ua-full-version"] = f'"{chrome_version.group(1)}"'
        return {"browser_env": fingerprint}

    def _prepare_signup_code_baseline(self, mailbox: dict[str, Any], index: int) -> None:
        try:
            mail_provider.prepare_code_baseline(self.mail_config, mailbox)
            step(index, "提交注册邮箱前邮箱基线已记录")
        except Exception as exc:
            step(index, f"邮箱基线记录失败，继续提交注册邮箱: {str(exc)[:160]}", "yellow")

    def _resend_signup_otp(self, index: int, mailbox: dict[str, Any]) -> None:
        self._ensure_active()
        try:
            mail_provider.prepare_code_baseline(self.mail_config, mailbox)
            step(index, "重发验证码前邮箱基线已记录")
        except Exception as exc:
            step(index, f"邮箱基线记录失败，继续重发验证码: {str(exc)[:160]}", "yellow")

        step(index, "开始重发注册验证码")
        url = f"{auth_base}/api/accounts/email-otp/resend"

        def submit():
            headers = self._otp_fetch_headers()
            return request_with_local_retry(
                self.session,
                "post",
                url,
                retry_attempts=1,
                headers=headers,
                allow_redirects=False,
                verify=False,
            )

        resp, error = submit()
        if (resp is None or resp.status_code != 200) and _is_cloudflare_challenge(resp):
            self._refresh_cloudflare_clearance(auth_base, index)
            resp, error = submit()
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"resend_otp_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(resp, 400)}"
            )

        data = _response_json(resp)
        error_data = data.get("error") if isinstance(data, dict) else None
        if isinstance(error_data, dict):
            error_message = str(error_data.get("message") or error_data.get("code") or "").strip()
        else:
            error_message = str(error_data or "").strip()
        if not error_message and data.get("success") is False:
            error_message = str(data.get("message") or "unknown_error").strip()
        if error_message:
            raise RuntimeError(f"resend_otp_rejected: {error_message}")
        step(index, "重发注册验证码完成")

    def _otp_validation_retryable(self, error_code: str) -> bool:
        return error_code in {"wrong_email_otp_code", "email_otp_invalid", "invalid_code"}

    def _retry_signup_otp_delivery(self, mailbox: dict[str, Any], index: int, reason: str) -> None:
        """Request a fresh OTP between polling windows without hiding hard rate limits."""
        try:
            self._resend_signup_otp(index, mailbox)
            step(index, f"{reason}，已主动重发验证码")
        except RegistrationStopped:
            raise
        except Exception as exc:
            detail = str(exc)
            if "429" in detail:
                raise
            step(index, f"{reason}，主动重发失败，继续等待现有邮件: {detail[:180]}", "yellow")

    def _validate_mailbox_otp(self, mailbox: dict[str, Any], index: int) -> None:
        max_attempts = 4
        last_detail = ""
        for attempt in range(1, max_attempts + 1):
            self._ensure_active()
            step(index, f"开始等待注册验证码（第 {attempt}/{max_attempts} 次）")
            code = mail_provider.wait_for_code(
                self.mail_config,
                mailbox,
                stop_event=self.stop_event,
            )
            self._ensure_active()
            if not code:
                if attempt >= max_attempts:
                    # 诊断：超时前 dump 邮箱内容，区分「邮件没投递」vs「邮件到了但被基线/ref 吞」
                    _dump_mailbox_for_diagnosis(mailbox, index)
                    raise RuntimeError(last_detail or "等待注册验证码超时")
                step(index, f"第 {attempt}/{max_attempts} 次等待未观察到验证码邮件，准备主动重发", "yellow")
                self._retry_signup_otp_delivery(mailbox, index, "本轮邮箱接口未观察到验证码")
                continue
            mail_provider.mark_verification_code_received(mailbox)
            step(index, "收到注册验证码")
            step(index, "开始校验验证码")
            resp, error = self._request_otp_validation(code, index)
            if resp is not None and resp.status_code == 200:
                step(index, "验证码校验完成")
                return
            error_code = _otp_error_code(resp)
            body = ""
            try:
                body = str(resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            last_detail = error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}"
            retryable = self._otp_validation_retryable(error_code)
            if not retryable or attempt >= max_attempts:
                raise RuntimeError(last_detail)
            mail_provider.mark_verification_code_rejected(mailbox, code)
            step(index, f"验证码被上游拒绝({error_code})，忽略该验证码并请求新邮件", "yellow")
            self._retry_signup_otp_delivery(mailbox, index, "验证码已失效或不匹配")
            time.sleep(1.5)

        raise RuntimeError(last_detail or "验证码校验失败")

    def _request_otp_validation(self, code: str, index: int):
        return validate_otp(self.session, self.device_id, code)

    def _login_otp_and_authorize(self, email: str, mailbox: dict[str, Any], index: int) -> dict:
        """在已注册的 ChatGPT 会话上用邮箱登录码（无需密码）补全 Platform OAuth token。

        passwordless 注册的账号没有密码；_platform_authorize(screen_hint="login")
        已用主 session 完成 authorize 并落地 /email-verification，主 session 已持有
        登录状态的会话上下文（不再需要重新发起 authorize，否则会触发第三次
        rate_limit_exceeded）。本方法：
          1. 用主 session 等待第二封登录验证码邮件（validate_otp）
          2. 拿 continue_url 换 Platform OAuth token
        返回 token dict（access_token/refresh_token/id_token），失败抛异常由调用方
        决定降级保留 ChatGPT session 账号。
        """
        step(index, "平台授权落到 email-verification，用主 session + 登录码换 OAuth token", "yellow")
        code_verifier = self.code_verifier or _generate_pkce()[0]
        try:
            # 等待第二封登录验证码（注册码已在 _validate_mailbox_otp 消费，同邮箱新邮件）
            step(index, "等待登录验证码邮件（第二封）")
            code = mail_provider.wait_for_code(self.mail_config, mailbox, stop_event=self.stop_event)
            self._ensure_active()
            if not code:
                raise RuntimeError("登录验证码等待超时")
            step(index, f"收到登录验证码: {code}")
            resp, reason = validate_otp(self.session, self.device_id, code)
            if resp is None or resp.status_code != 200:
                data = _response_json(resp) if resp is not None else {}
                message = str((data.get("error") or {}).get("message") or data.get("message") or "").strip()
                raise RuntimeError(reason or f"登录验证码校验失败{': ' + message if message else ''}")
            otp_payload = _response_json(resp)
            continue_url = str(otp_payload.get("continue_url") or "").strip()
            step(index, "登录验证码校验完成")
            available = {
                key: self.session.cookies.get(key, domain=".auth.openai.com") or ""
                for key in ("oai-client-auth-session",)
            }
            step(
                index,
                f"login continue_url={continue_url[:120] or '(空)'}, "
                f"oai-client-auth-session={'有' if available.get('oai-client-auth-session') else '无'}",
            )

            # 优先用 continue_url 内嵌的 OAuth code 直接换 token（passwordless 登录后
            # continue_url 就是 platform callback 带 code）；否则走 consent 交互链路。
            code = ""
            callback_params = extract_oauth_callback_params_from_url(continue_url)
            if callback_params:
                code = str(callback_params.get("code") or "").strip()
            if not code:
                tokens = exchange_platform_tokens(self.session, self.device_id, code_verifier, continue_url)
                if not tokens:
                    raise RuntimeError("token换取失败")
            else:
                tokens = request_platform_oauth_token(self.session, code, code_verifier)
                if not tokens:
                    raise RuntimeError("token换取失败")
            step(index, "登录链路 token 换取完成", "green")
            return tokens
        finally:
            pass

    def _login_and_exchange_tokens(self, email: str, password: str, mailbox: dict, index: int) -> dict:
        step(index, "开始独立登录换 token")
        login_session = create_session(config["proxy"])
        login_device_id = str(uuid.uuid4())
        login_session.cookies.set("oai-did", login_device_id, domain=".auth.openai.com")
        login_session.cookies.set("oai-did", login_device_id, domain="auth.openai.com")
        code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": login_device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }

        def _login_nav_headers(referer: str = "") -> dict[str, str]:
            h = dict(navigate_headers)
            if referer:
                h["referer"] = referer
            return h

        def _login_json_headers(referer: str) -> dict[str, str]:
            h = dict(common_headers)
            h["referer"] = referer
            h["oai-device-id"] = login_device_id
            h.update(_make_trace_headers())
            return h

        resp, error = request_with_local_retry(
            login_session, "get",
            f"{auth_base}/api/accounts/authorize?{urlencode(params)}",
            headers=_login_nav_headers(f"{platform_base}/"),
            allow_redirects=True, verify=False
        )
        # If CF challenge detected during login, try FlareSolverr
        if (resp is None or resp.status_code != 200) and _is_cloudflare_challenge(resp):
            flaresolverr_url = str(config.get("flaresolverr_url") or "").strip()
            if flaresolverr_url:
                step(index, "登录 authorize 被 CF 拦截，通过 FlareSolverr 过盾...", "yellow")
                flaresolverr.invalidate_cache()
                solution = flaresolverr.get_cf_clearance(
                    flaresolverr_url=flaresolverr_url,
                    target_url=f"{auth_base}/api/accounts/authorize?{urlencode(params)}",
                    proxy=_normalize_proxy_scheme(config["proxy"]),
                    force_refresh=True,
                )
                if solution and solution.get("status") == "ok":
                    flaresolverr.apply_cookies_to_session(login_session, solution, domain=".openai.com")
                    step(index, "FlareSolverr 过盾成功，重试登录 authorize", "green")
                    resp, error = request_with_local_retry(
                        login_session, "get",
                        f"{auth_base}/api/accounts/authorize?{urlencode(params)}",
                        headers=_login_nav_headers(f"{platform_base}/"),
                        allow_redirects=True, verify=False
                    )
                else:
                    step(index, "FlareSolverr 过盾失败", "red")
        if resp is None:
            raise RuntimeError(error or "platform_login_authorize_failed")
        if _is_cloudflare_challenge(resp):
            raise RuntimeError("登录被 Cloudflare 拦截，FlareSolverr 未能解决或未配置")
        step(index, "登录 authorize 完成")

        # 提交邮箱（原样，不带 state）
        def _do_authorize_continue():
            h = _login_json_headers(f"{auth_base}/log-in?usernameKind=email")
            h["openai-sentinel-token"] = build_sentinel_token(login_session, login_device_id, "authorize_continue")
            return request_with_local_retry(
                login_session, "post",
                f"{auth_base}/api/accounts/authorize/continue",
                json={"username": {"kind": "email", "value": email}},
                headers=h,
                allow_redirects=False,
                verify=False
            )

        step(index, "开始提交邮箱")
        resp, error = _do_authorize_continue()
        if resp is not None and resp.status_code == 409:
            step(index, "邮箱提交 invalid_state，重新 authorize 后重试")
            # 再次清除 cookie 并重新 authorize
            for cookie in list(login_session.cookies):
                if 'auth.openai.com' in cookie.domain:
                    login_session.cookies.clear(domain=cookie.domain, path=cookie.path, name=cookie.name)
            login_session.cookies.set("oai-did", login_device_id, domain=".auth.openai.com")
            login_session.cookies.set("oai-did", login_device_id, domain="auth.openai.com")
            resp, error = request_with_local_retry(
                login_session, "get",
                f"{auth_base}/api/accounts/authorize?{urlencode(params)}",
                headers=_login_nav_headers(f"{platform_base}/"),
                allow_redirects=True, verify=False
            )
            if resp is None:
                raise RuntimeError(error or "platform_login_authorize_retry_failed")
            resp, error = _do_authorize_continue()

        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            detail = json.dumps(data, ensure_ascii=False) if data else ""
            raise RuntimeError(
                error or f"email_submit_http_{getattr(resp, 'status_code', 'unknown')}"
                + (f": {detail}" if detail else "")
            )
        step(index, "邮箱提交完成")

        # 密码验证
        step(index, "开始密码校验")
        headers = _login_json_headers(f"{auth_base}/log-in/password")
        headers["openai-sentinel-token"] = build_sentinel_token(
            login_session, login_device_id, "password_verify"
        )
        resp, error = request_with_local_retry(
            login_session, "post",
            f"{auth_base}/api/accounts/password/verify",
            json={"password": password},
            headers=headers,
            allow_redirects=False,
            verify=False
        )
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"password_verify_http_{getattr(resp, 'status_code', '')}_body={body}")
        step(index, "密码校验完成")

        payload = _response_json(resp)
        continue_url = str(payload.get("continue_url") or "").strip()
        page_type = str(((payload.get("page") or {}).get("type")) or "")

        if page_type == "email_otp_verification" or "email-verification" in continue_url or "email-otp" in continue_url:
            step(index, "独立登录需要邮箱验证码")
            code = wait_for_code(mailbox)
            if not code:
                login_session.close()
                raise RuntimeError("独立登录等待验证码超时")
            step(index, f"收到登录验证码: {code}")
            resp, reason = validate_otp(login_session, login_device_id, code)
            if resp is None or resp.status_code != 200:
                print("独立登录验证码校验失败响应:", resp.text if resp is not None else "None")
                data = _response_json(resp) if resp is not None else {}
                message = str((data.get("error") or {}).get("message") or data.get("message") or "").strip()
                login_session.close()
                raise RuntimeError(reason or f"独立登录验证码校验失败{': ' + message if message else ''}")
            otp_payload = _response_json(resp)
            continue_url = str(otp_payload.get("continue_url") or continue_url).strip()
            step(index, "独立登录验证码校验完成")

        if not continue_url:
            continue_url = f"{auth_base}/sign-in-with-chatgpt/codex/consent"
        tokens = exchange_platform_tokens(login_session, login_device_id, code_verifier, continue_url)
        login_session.close()
        if not tokens:
            raise RuntimeError("token换取失败")
        step(index, "token 换取完成")
        return tokens

    def _exchange_registered_tokens(self, index: int) -> dict:
        step(index, "开始换 token")
        tokens = request_platform_oauth_token(self.session, self.platform_auth_code, self.code_verifier)
        if not tokens:
            raise RuntimeError("token换取失败")
        step(index, "token 换取完成")
        return tokens

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = create_mailbox()
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        try:
            password = ""
            first_name, last_name = _random_name()
            self._ensure_active()
            # 先抓邮箱基线：邮箱刚创建，inbucket 收件箱为空，抓基线不会吞掉后续邮件。
            # 若在 _chatgpt_authorize 之后再抓基线，login_hint 已触发 OTP 投递，
            # 在途邮件会被基线标记为 seen，wait_for_code 将永远跳过它导致超时。
            self._prepare_signup_code_baseline(mailbox, index)
            self._ensure_active()
            # login_hint 会让 chatgpt 的 authorize 直接进入 /email-verification 状态，
            # 由 OAuth authorize 本身触发 OTP 发送；否则 authorize/continue 只返回
            # email_otp_verification 页面但从不下发验证码邮件（服务端只在 authorize
            # 带 login_hint 时投递 OTP）。
            self._chatgpt_authorize(email, index, include_login_hint=True)
            self._ensure_active()
            if self.chatgpt_authorize_landed_path == "/email-verification":
                signup_mode, verification_mode = "otp", "passwordless_signup"
                self.signup_verification_mode = verification_mode
                step(index, "authorize 已进入验证码页，直接等待验证码邮件")
            else:
                # 其余落地（含 /create-account/password）：必须先 authorize/continue 提交邮箱
                # 建立正确的注册会话，再判定密码/无密码模式；直接调 user/register 会因
                # 缺少 signup 会话上下文返回 400 account_creation_failed（并发下偶发落地此页）。
                signup_mode, verification_mode = self._authorize_signup(email, index)
                if signup_mode == "password":
                    password = _random_password()
                    self._ensure_active()
                    self._register_user(email, password, index)
                    self._ensure_active()
                    self._send_otp(index, mailbox)
                else:
                    if verification_mode == "passwordless_login":
                        raise RuntimeError("signup_email_already_registered")
                    if verification_mode != "passwordless_signup":
                        raise RuntimeError(
                            "signup_otp_mode_unconfirmed: "
                            f"email_verification_mode={verification_mode or 'unknown'}"
                        )
                    step(index, "提交邮箱已触发验证码，直接等待首封邮件")
            self._validate_mailbox_otp(mailbox, index)
            self._ensure_active()
            self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
            self._ensure_active()
            chatgpt_session = self._finish_chatgpt_registration(index)
            tokens: dict = {}
            try:
                self._platform_authorize(email, index, screen_hint="login")
                tokens = self._exchange_registered_tokens(index)
            except Exception as oauth_error:
                # 首次 _platform_authorize 拿不到 code（通常落到 /email-verification），
                # 尝试独立登录 OTP 链路补全 refresh_token；仍失败才降级保留 ChatGPT session。
                try:
                    step(
                        index,
                        f"platform authorize 未获取到 code（{str(oauth_error)[:160]}），尝试登录 OTP 链路",
                        "yellow",
                    )
                    tokens = self._login_otp_and_authorize(email, mailbox, index)
                except Exception as login_error:
                    # The ChatGPT session is already usable. Keep the account when
                    # the optional Platform OAuth refresh-token flow is intermittent.
                    step(
                        index,
                        f"Platform OAuth refresh token 获取失败，保留 ChatGPT session 账号: {str(login_error)[:240]}",
                        "yellow",
                    )
        except Exception as error:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise
        mail_provider.mark_mailbox_result(mailbox, success=True)
        return {
            "email": email,
            "password": password,
            "access_token": str(chatgpt_session.get("access_token") or "").strip(),
            "platform_access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "session_token": str(chatgpt_session.get("session_token") or "").strip(),
            "cookie": str(chatgpt_session.get("cookie") or "").strip(),
            "source_type": "web",
            **self._account_environment(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


def worker(index: int) -> dict:
    start = time.time()
    # Pick a proxy from the pool (round-robin)
    proxy = _pick_proxy()
    proxy_display = proxy if proxy else "(直连)"

    # Validate proxy and detect real IP before starting registration
    try:
        real_ip = _validate_proxy_and_get_ip(proxy, index)
        step(index, f"代理: {proxy_display} | 出口IP: {real_ip}", "green")
    except Exception as e:
        step(index, f"代理验证失败，停止注册: {e}", "red")
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        return {"ok": False, "index": index, "error": str(e)}

    registrar = PlatformRegistrar(proxy)
    try:
        # Check FlareSolverr connectivity if configured
        flaresolverr_url = str(config.get("flaresolverr_url") or "").strip()
        if flaresolverr_url:
            ok, msg = flaresolverr.check_health(flaresolverr_url)
            if not ok:
                step(index, f"FlareSolverr 连接检查失败，停止注册: {msg}", "red")
                with stats_lock:
                    stats["done"] += 1
                    stats["fail"] += 1
                return {"ok": False, "index": index, "error": msg}
            step(index, f"FlareSolverr 连接正常: {flaresolverr_url}", "green")

        step(index, "任务启动")
        result = registrar.register(index)
        cost = time.time() - start
        access_token = str(result["access_token"])
        account_service.add_account_items([result])
        refresh_result = account_service.refresh_accounts([access_token])
        if refresh_result.get("errors"):
            step(index, f"账号已保存，刷新状态暂未成功，稍后可重试: {refresh_result['errors']}", "yellow")
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            baseline = stats["start_time"] if stats["start_time"] else start
            avg = (time.time() - baseline) / stats["success"]
        log(f'{result["email"]} 注册成功 [IP:{real_ip}]，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except Exception as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败 [IP:{real_ip}]，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e)}
    finally:
        registrar.close()
