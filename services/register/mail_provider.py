from __future__ import annotations

import hashlib
import json
import random
import re
import string
import time
from datetime import datetime, timedelta, timezone
from email import message_from_string, policy
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any, Callable, TypeVar

from curl_cffi import requests


from services.config import DATA_DIR

DDG_ALIASES_FILE = DATA_DIR / "ddg_aliases.json"
_ddg_aliases_lock = Lock()


def _load_ddg_aliases() -> set[str]:
    try:
        if DDG_ALIASES_FILE.exists():
            data = json.loads(DDG_ALIASES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(item).strip().lower() for item in data if str(item).strip()}
    except Exception:
        pass
    return set()


def _save_ddg_aliases(aliases: set[str]) -> None:
    DDG_ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    DDG_ALIASES_FILE.write_text(json.dumps(sorted(aliases), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_ddg_alias_duplicate(address: str) -> bool:
    target = str(address or "").strip().lower()
    if not target:
        return False
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        return target in used


def _record_ddg_alias(address: str) -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        used.add(target)
        _save_ddg_aliases(used)


from services.config import DATA_DIR

DDG_ALIASES_FILE = DATA_DIR / "ddg_aliases.json"
_ddg_aliases_lock = Lock()


def _load_ddg_aliases() -> set[str]:
    try:
        if DDG_ALIASES_FILE.exists():
            data = json.loads(DDG_ALIASES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(item).strip().lower() for item in data if str(item).strip()}
    except Exception:
        pass
    return set()


def _save_ddg_aliases(aliases: set[str]) -> None:
    DDG_ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    DDG_ALIASES_FILE.write_text(json.dumps(sorted(aliases), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_ddg_alias_duplicate(address: str) -> bool:
    target = str(address or "").strip().lower()
    if not target:
        return False
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        return target in used


def _record_ddg_alias(address: str) -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        used.add(target)
        _save_ddg_aliases(used)


ResultT = TypeVar("ResultT")
domain_lock = Lock()
provider_lock = Lock()
domain_index = 0
provider_index = 0
cloudmail_token_lock = Lock()
cloudmail_token_cache: dict[str, tuple[str, float]] = {}


def _config(mail_config: dict) -> dict:
    return {
        "request_timeout": float(mail_config.get("request_timeout") or 30),
        "wait_timeout": float(mail_config.get("wait_timeout") or 30),
        "wait_interval": float(mail_config.get("wait_interval") or 2),
        "user_agent": str(mail_config.get("user_agent") or "Mozilla/5.0"),
        "proxy": str(mail_config.get("proxy") or "").strip(),
    }


def _random_mailbox_name() -> str:
    return f"{''.join(random.choices(string.ascii_lowercase, k=5))}{''.join(random.choices(string.digits, k=random.randint(1, 3)))}{''.join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))}"


def _random_subdomain_label() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 10)))


def _next_domain(domains: list[str]) -> str:
    global domain_index
    domains = [str(item).strip() for item in domains if str(item).strip()]
    if not domains:
        raise RuntimeError("mail.domain 不能为空")
    if len(domains) == 1:
        return domains[0]
    with domain_lock:
        value = domains[domain_index % len(domains)]
        domain_index = (domain_index + 1) % len(domains)
        return value


def _expand_wildcard_domain(domain: str) -> str:
    """把域名里的 ``*`` 替换成随机子域标签，实现泛域名。

    例如 ``*.qingai.cfd`` -> ``a1b2c3d4.qingai.cfd``；每个 ``*`` 独立展开，
    嵌套子域也能用（``*.*.example.com`` -> 两级随机子域）。不含 ``*`` 时原样返回。
    """
    text = str(domain or "").strip().lstrip("@").strip(".")
    if "*" not in text:
        return text
    return re.sub(r"\*", lambda _m: _random_subdomain_label(), text)


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _create_session(conf: dict, provider_proxy: str | None = None):
    """Create a curl_cffi session.

    Args:
        conf: Global mail config dict (may contain a "proxy" key).
        provider_proxy: Per-provider proxy override.
            - None: use global conf["proxy"] (default, backward compatible)
            - "": explicitly no proxy (skip global)
            - "http://...": use this specific proxy
    """
    if provider_proxy is not None:
        proxy = provider_proxy.strip()
    else:
        proxy = str(conf.get("proxy") or "").strip()
    kwargs = {"impersonate": "chrome", "verify": False}
    if proxy:
        kwargs["proxy"] = proxy
    return requests.Session(**kwargs)


def _parse_received_at(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        date = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        date = parsedate_to_datetime(text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_content(data: dict[str, Any]) -> tuple[str, str]:
    text_content = str(data.get("text_content") or data.get("text") or data.get("body") or data.get("content") or "")
    html_content = str(data.get("html_content") or data.get("html") or data.get("html_body") or data.get("body_html") or "")
    if text_content or html_content:
        return text_content, html_content
    raw = data.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return "", ""
    try:
        parsed = message_from_string(raw, policy=policy.default)
    except Exception:
        return raw, ""
    plain: list[str] = []
    html: list[str] = []
    for part in parsed.walk() if parsed.is_multipart() else [parsed]:
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_content()
        except Exception:
            payload = ""
        if not payload:
            continue
        if part.get_content_type() == "text/html":
            html.append(str(payload))
        else:
            plain.append(str(payload))
    return "\n".join(plain).strip(), "\n".join(html).strip()


def _extract_text_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key in ("address", "email", "name", "value"):
            if value.get(key):
                out.extend(_extract_text_candidates(value.get(key)))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_extract_text_candidates(item))
        return out
    return []


def _message_matches_email(data: dict[str, Any], email: str) -> bool:
    target = str(email or "").strip().lower()
    candidates: list[str] = []
    for key in ("to", "mailTo", "receiver", "receivers", "address", "email", "envelope_to"):
        if key in data:
            candidates.extend(_extract_text_candidates(data.get(key)))
    return not target or not candidates or any(target in str(item).strip().lower() for item in candidates if str(item).strip())


def _extract_code(message: dict[str, Any]) -> str | None:
    content = f"{message.get('subject', '')}\n{message.get('text_content', '')}\n{message.get('html_content', '')}".strip()
    if not content:
        return None
    match = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>", content, re.I)
    if match:
        return match.group(1)
    match = re.search(r"(?:Verification code|code is|代码为|验证码)[:\s]*(\d{6})", content, re.I)
    if match and match.group(1) != "177010":
        return match.group(1)
    for code in re.findall(r">\s*(\d{6})\s*<|(?<![#&])\b(\d{6})\b", content):
        value = code[0] or code[1]
        if value and value != "177010":
            return value
    return None


def _message_tracking_ref(message: dict[str, Any]) -> str:
    provider = str(message.get("provider") or "").strip()
    mailbox = str(message.get("mailbox") or "").strip()
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        return f"id:{provider}:{mailbox}:{message_id}"
    received_at = message.get("received_at")
    received_value = received_at.isoformat() if isinstance(received_at, datetime) else str(received_at or "")
    content = "\n".join(str(message.get(key) or "") for key in ("subject", "sender", "text_content", "html_content"))
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    return f"content:{provider}:{mailbox}:{received_value}:{digest}"


def mark_verification_code_rejected(mailbox: dict[str, Any], code: str) -> None:
    normalized = re.sub(r"\D", "", str(code or ""))[:6]
    if not normalized:
        return
    values = mailbox.setdefault("_rejected_verification_codes", [])
    if not isinstance(values, list):
        values = []
        mailbox["_rejected_verification_codes"] = values
    if normalized not in values:
        values.append(normalized)


def mark_verification_code_received(mailbox: dict[str, Any]) -> None:
    """注册流程收到并消费了验证码时调用（当前为占位，预留后续邮箱池状态记录）。"""
    pass


def _rejected_code_set(mailbox: dict[str, Any]) -> set[str]:
    values = mailbox.setdefault("_rejected_verification_codes", [])
    if not isinstance(values, list):
        values = []
        mailbox["_rejected_verification_codes"] = values
    return {str(value) for value in values if str(value)}


def _verification_code_rejected(mailbox: dict[str, Any], code: str | None) -> bool:
    return bool(code and code in _rejected_code_set(mailbox))


def _message_before_code_boundary(mailbox: dict[str, Any], message: dict[str, Any]) -> bool:
    boundary = mailbox.get("_code_not_before")
    received_at = message.get("received_at")
    if not isinstance(boundary, datetime) or not isinstance(received_at, datetime):
        return False
    if not received_at.tzinfo:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at < boundary


class BaseMailProvider:
    name = "unknown"

    def __init__(self, conf: dict, provider_ref: str = ""):
        self.conf = conf
        self.provider_ref = provider_ref
        self.stop_event: Any = None

    def _stopped(self) -> bool:
        return bool(self.stop_event and self.stop_event.is_set())

    def _poll_wait(self) -> bool:
        seconds = max(0.2, self.conf["wait_interval"])
        if self.stop_event is not None:
            return self.stop_event.wait(seconds)
        time.sleep(seconds)
        return False

    def wait_for(self, mailbox: dict[str, Any], on_message: Callable[[dict[str, Any]], ResultT | None]) -> ResultT | None:
        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline and not self._stopped():
            message = self.fetch_latest_message(mailbox)
            if message:
                result = on_message(message)
                if result is not None:
                    return result
            if self._poll_wait():
                break
        return None

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}

        def extract_unseen_code(message: dict[str, Any]) -> str | None:
            if _message_before_code_boundary(mailbox, message):
                return None
            ref = _message_tracking_ref(message)
            if ref in seen_refs:
                return None
            code = _extract_code(message)
            if code and not _verification_code_rejected(mailbox, code):
                seen_value.append(ref)
                seen_refs.add(ref)
                return code
            return None

        return self.wait_for(mailbox, extract_unseen_code)

    def prepare_code_baseline(self, mailbox: dict[str, Any]) -> None:
        messages: list[dict[str, Any]] = []
        fetch_recent = getattr(self, "fetch_recent_messages", None)
        if callable(fetch_recent):
            recent = fetch_recent(mailbox)
            if isinstance(recent, list):
                messages.extend(message for message in recent if isinstance(message, dict))
        else:
            latest = self.fetch_latest_message(mailbox)
            if isinstance(latest, dict):
                messages.append(latest)

        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(value) for value in seen_value}
        for message in messages:
            ref = _message_tracking_ref(message)
            if ref not in seen_refs:
                seen_value.append(ref)
                seen_refs.add(ref)
            code = _extract_code(message)
            if code:
                mark_verification_code_rejected(mailbox, code)
        # Provider and host clocks can differ slightly; baseline refs/codes still
        # prevent pre-existing mail from being selected inside this skew window.
        mailbox["_code_not_before"] = datetime.now(timezone.utc) - timedelta(minutes=2)

    def close(self) -> None:
        pass


class CloudflareTempMailProvider(BaseMailProvider):
    name = "cloudflare_temp_email"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.admin_password = str(entry["admin_password"]).strip()
        self.domain = entry.get("domain") or []
        self.session = _create_session(conf)

    def _request(self, method: str, path: str, headers: dict | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers={"Content-Type": "application/json", "User-Agent": self.conf["user_agent"], **(headers or {})}, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"CloudflareTempMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        data = self._request("POST", "/admin/new_address", headers={"x-admin-auth": self.admin_password}, payload={"enablePrefix": True, "name": username or _random_mailbox_name(), "domain": _next_domain(self.domain)})
        address = str(data.get("address") or "").strip()
        token = str(data.get("jwt") or "").strip()
        if not address or not token:
            raise RuntimeError("CloudflareTempMail 缺少 address 或 jwt")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/api/mails", headers={"Authorization": f"Bearer {mailbox['token']}"}, params={"limit": 10, "offset": 0})
        raw = list(data.get("results") or []) if isinstance(data, dict) else data if isinstance(data, list) else []
        messages = [item for item in raw if isinstance(item, dict) and _message_matches_email(item, str(mailbox.get("address") or ""))]
        if not messages:
            return None
        item = messages[0]
        text_content, html_content = _extract_content(item)
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or item.get("_id") or ""), "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

    def close(self) -> None:
        self.session.close()


class DDGMailProvider(BaseMailProvider):
    name = "ddg_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.ddg_token = str(entry["ddg_token"]).strip()
        self.cf_api_base = str(entry.get("api_base") or entry.get("cf_api_base") or "").rstrip("/")
        self.cf_inbox_jwt = str(entry.get("cf_inbox_jwt") or "").strip()
        self.cf_admin_password = str(entry.get("admin_password") or "").strip()
        self.cf_api_key = str(entry.get("cf_api_key") or "").strip()
        self.cf_auth_mode = str(entry.get("cf_auth_mode") or "none").strip().lower()
        self.cf_domain = entry.get("cf_domain") or []
        self.cf_create_path = str(entry.get("cf_create_path") or "/api/new_address").strip()
        self.cf_messages_path = str(entry.get("cf_messages_path") or "/api/mails").strip()
        self.session = _create_session(conf)

    def _cf_build_headers(self, content_type: bool = False) -> dict:
        headers = {"Content-Type": "application/json"} if content_type else {}
        if self.cf_api_key:
            if self.cf_auth_mode == "x-api-key":
                headers["X-API-Key"] = self.cf_api_key
            elif self.cf_auth_mode != "none":
                headers["Authorization"] = f"Bearer {self.cf_api_key}"
        return headers

    def _cf_request(self, method: str, path: str, headers: dict | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)) -> dict:
        merged_headers = {**self._cf_build_headers(True), **(headers or {}), "User-Agent": self.conf["user_agent"]}
        if self.cf_admin_password and method.upper() in ("POST",):
            merged_headers["x-admin-auth"] = self.cf_admin_password
        if self.cf_api_key and self.cf_auth_mode == "query-key":
            params = {**(params or {}), "key": self.cf_api_key}
        resp = self.session.request(method.upper(), f"{self.cf_api_base}{path}", headers=merged_headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"DDGMail CF请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def _ddg_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        resp = self.session.request(method.upper(), f"https://quack.duckduckgo.com{path}", headers={"Authorization": f"Bearer {self.ddg_token}", "Content-Type": "application/json", "User-Agent": self.conf["user_agent"]}, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"DDG API请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return resp.json()

    def _cf_list_payload(self, data: Any) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "hydra:member", "data", "messages"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
                if isinstance(value, dict) and isinstance(value.get("messages"), list):
                    return value["messages"]
        return []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        ddg_data = self._ddg_request("POST", "/api/email/addresses", payload={})
        ddg_address_part = str(ddg_data.get("address") or "").strip()
        if not ddg_address_part:
            raise RuntimeError("DDG API 返回无 address 字段")
        ddg_address = f"{ddg_address_part}@duck.com"

        if _is_ddg_alias_duplicate(ddg_address):
            raise RuntimeError(f"[{self.label}] DDG日上限已达，别名 {ddg_address} 已存在，自动切换邮箱提供商")

        _record_ddg_alias(ddg_address)

        if not self.cf_inbox_jwt:
            raise RuntimeError("DDGMail 需要 cf_inbox_jwt（DDG 转发目标的固定收件箱 JWT），请在邮箱配置中填写 CF Inbox JWT")

        return {"provider": self.name, "provider_ref": self.provider_ref, "address": ddg_address, "token": self.cf_inbox_jwt, "label": self.label}

    def _parse_raw_recipient(self, raw_text: str) -> str:
        if not raw_text:
            return ""
        match = re.search(r"^To:\s*(.+?)$", raw_text, re.MULTILINE | re.IGNORECASE)
        if match:
            addr = match.group(1).strip()
            addr = re.sub(r"\s*<[^>]*>", "", addr)
            return addr.strip().lower()
        try:
            parsed = message_from_string(raw_text, policy=policy.default)
            return str(parsed.get("To") or "").strip().lower()
        except Exception:
            return ""

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        target_address = str(mailbox.get("address") or "").strip().lower()
        data = self._cf_request("GET", self.cf_messages_path, headers={"Authorization": f"Bearer {mailbox['token']}"}, params={"limit": 30, "offset": 0})
        raw_list = self._cf_list_payload(data)
        messages = [item for item in raw_list if isinstance(item, dict)]
        if not messages:
            return None

        for item in messages:
            message_id = str(item.get("id") or item.get("msgid") or item.get("_id") or "")
            raw_text = str(item.get("raw") or "")
            raw_recipient = self._parse_raw_recipient(raw_text)
            if target_address and raw_recipient and target_address not in raw_recipient:
                continue
            text_content, html_content = _extract_content(item)
            subject = str(item.get("subject") or "")
            sender = item.get("from") or item.get("sender") or item.get("source") or ""
            if isinstance(sender, dict):
                sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
            if raw_text and (not subject or not sender or subject == sender == ""):
                try:
                    parsed = message_from_string(raw_text, policy=policy.default)
                    if not subject:
                        subject = str(parsed.get("Subject") or "")
                    if not sender:
                        sender = str(parsed.get("From") or "")
                except Exception:
                    pass
            return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": subject, "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

        return None

    def close(self) -> None:
        self.session.close()


class CloudMailGenProvider(BaseMailProvider):
    name = "cloudmail_gen"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.admin_email = str(entry.get("admin_email") or "").strip()
        self.admin_password = str(entry.get("admin_password") or "").strip()
        self.domain = _normalize_string_list(entry.get("domain"))
        self.subdomain = _normalize_string_list(entry.get("subdomain"))
        self.email_prefix = str(entry.get("email_prefix") or "").strip()
        self.session = _create_session(conf)

    def _request(
        self,
        method: str,
        path: str,
        headers: dict | None = None,
        params: dict | None = None,
        payload: dict | None = None,
        expected: tuple[int, ...] = (200,),
    ):
        resp = self.session.request(
            method.upper(),
            f"{self.api_base}{path}",
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.conf["user_agent"],
                **(headers or {}),
            },
            params=params,
            json=payload,
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        if resp.status_code not in expected:
            raise RuntimeError(f"CloudMailGen 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def _cache_key(self) -> str:
        return f"{self.api_base}|{self.admin_email}"

    def _get_token(self) -> str:
        if not self.admin_email or not self.admin_password:
            raise RuntimeError("CloudMailGen 缺少 admin_email 或 admin_password")
        cache_key = self._cache_key()
        now = time.time()
        with cloudmail_token_lock:
            cached = cloudmail_token_cache.get(cache_key)
            if cached and now < cached[1] - 300:
                return cached[0]
        data = self._request(
            "POST",
            "/api/public/genToken",
            payload={"email": self.admin_email, "password": self.admin_password},
        )
        token = ""
        if isinstance(data, dict) and data.get("code") == 200:
            token = str((data.get("data") or {}).get("token") or "").strip()
        if not token:
            raise RuntimeError(f"CloudMailGen genToken 返回异常: {data}")
        with cloudmail_token_lock:
            cloudmail_token_cache[cache_key] = (token, now + 24 * 3600)
        return token

    def _resolve_address(self, username: str | None = None) -> str:
        domain = _next_domain(self.domain)
        if self.subdomain:
            domain = f"{random.choice(self.subdomain)}.{domain}"
        if username:
            local_part = username
        elif self.email_prefix:
            local_part = f"{self.email_prefix}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
        else:
            local_part = _random_mailbox_name()
        return f"{local_part}@{domain}"

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.domain:
            raise RuntimeError("CloudMailGen 需要至少配置一个 domain")
        address = self._resolve_address(username)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        address = str(mailbox.get("address") or "").strip()
        if not address:
            raise RuntimeError("CloudMailGen 缺少 address")
        token = self._get_token()
        data = self._request(
            "POST",
            "/api/public/emailList",
            headers={"Authorization": token},
            payload={"toEmail": address, "size": 20, "timeSort": "desc"},
        )
        items = (data.get("data") or []) if isinstance(data, dict) and data.get("code") == 200 else []
        messages = [item for item in items if isinstance(item, dict) and _message_matches_email(item, address)]
        if not messages:
            return None
        item = messages[0]
        text_content, html_content = _extract_content(item)
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": str(item.get("id") or item.get("_id") or item.get("messageId") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": str(item.get("from") or item.get("sender") or ""),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(
                item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")
            ),
            "to": item.get("to") or item.get("toEmail") or item.get("mailTo"),
            "raw": item,
        }

    def close(self) -> None:
        self.session.close()


class TempMailLolProvider(BaseMailProvider):
    name = "tempmail_lol"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry.get("api_key") or "").strip()
        self.domain = [str(item).strip() for item in (entry.get("domain") or []) if str(item).strip()]
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    @staticmethod
    def _resolve_domain(domain: str) -> tuple[str, bool]:
        text = str(domain or "").strip().lower()
        if text.startswith("*.") and len(text) > 2:
            return f"{_random_subdomain_label()}.{text[2:]}", True
        return text, False

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"https://api.tempmail.lol/v2{path}", params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"TempMail.lol 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"TempMail.lol {method} {path} 返回结构不是对象")
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.domain:
            domain, force_random_prefix = self._resolve_domain(random.choice(self.domain))
            payload["domain"] = domain
            if force_random_prefix:
                payload["prefix"] = _random_mailbox_name()
        if username and "prefix" not in payload:
            payload["prefix"] = username
        data = self._request("POST", "/inbox/create", payload=payload, expected=(200, 201))
        address = str(data.get("address") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address or not token:
            raise RuntimeError("TempMail.lol 缺少 address 或 token")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/inbox", params={"token": mailbox["token"]})
        items = data.get("emails") or data.get("messages") or []
        messages = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if not messages:
            return None
        item = max(messages, key=lambda value: ((_parse_received_at(value.get("created_at") or value.get("createdAt") or value.get("date") or value.get("received_at") or value.get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(), str(value.get("id") or value.get("token") or "")))
        text_content, html_content = _extract_content(item)
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or item.get("token") or ""), "subject": str(item.get("subject") or ""), "sender": str(item.get("from") or item.get("from_address") or ""), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("created_at") or item.get("createdAt") or item.get("date") or item.get("received_at") or item.get("timestamp")), "raw": item}

    def close(self) -> None:
        self.session.close()


class DuckMailProvider(BaseMailProvider):
    name = "duckmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry["api_key"]).strip()
        self.default_domain = str(entry.get("default_domain") or "duckmail.sbs").strip() or "duckmail.sbs"
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, token: str = "", use_api_key: bool = False, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200, 201, 204)):
        headers = {"Authorization": f"Bearer {self.api_key if use_api_key else token}"} if use_api_key or token else {}
        resp = self.session.request(method.upper(), f"https://api.duckmail.sbs{path}", headers=headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"DuckMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    @staticmethod
    def _items(data):
        return data if isinstance(data, list) else data.get("hydra:member") or data.get("member") or data.get("data") or []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        password = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        address = f"{username or _random_mailbox_name()}@{self.default_domain}"
        payload = {"address": address, "password": password}
        account = self._request("POST", "/accounts", use_api_key=True, payload=payload)
        token_data = self._request("POST", "/token", use_api_key=True, payload=payload)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": str(token_data.get("token") or ""), "password": password, "account_id": str(account.get("id") or "")}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/messages", token=str(mailbox.get("token") or ""), params={"page": 1})
        items = self._items(data)
        if not items:
            return None
        item = items[0]
        message_id = str(item.get("id") or item.get("@id") or "").replace("/messages/", "")
        if message_id:
            item = self._request("GET", f"/messages/{message_id}", token=str(mailbox.get("token") or ""))
        sender = item.get("from") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("name") or ""
        html_content = item.get("html") or ""
        if isinstance(html_content, list):
            html_content = "".join(str(value) for value in html_content)
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": str(item.get("text") or item.get("text_content") or ""), "html_content": str(html_content), "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date")), "raw": item}

    def close(self) -> None:
        self.session.close()


class GptMailProvider(BaseMailProvider):
    name = "gptmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry["api_key"]).strip()
        self.default_domain = str(entry.get("default_domain") or "").strip()
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json", "X-API-Key": self.api_key})

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None):
        query = dict(params or {})
        resp = self.session.request(method.upper(), f"https://mail.chatgpt.org.uk{path}", params=query, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code != 200:
            raise RuntimeError(f"GPTMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        return data["data"] if isinstance(data, dict) and "data" in data else data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload = {key: value for key, value in {"prefix": username, "domain": self.default_domain}.items() if value}
        data = self._request("POST" if payload else "GET", "/api/generate-email", payload=payload or None)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": str(data["email"])}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/api/emails", params={"email": mailbox["address"]})
        emails = data if isinstance(data, list) else data.get("emails") or []
        if not emails:
            return None
        item = max(emails, key=lambda value: (float(value.get("timestamp") or 0), str(value.get("id") or "")))
        if item.get("id"):
            item = self._request("GET", f"/api/email/{item['id']}")
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or ""), "subject": str(item.get("subject") or ""), "sender": str(item.get("from_address") or ""), "text_content": str(item.get("content") or ""), "html_content": str(item.get("html_content") or ""), "received_at": _parse_received_at(item.get("timestamp") or item.get("created_at")), "raw": item}

    def close(self) -> None:
        self.session.close()


class MoEmailProvider(BaseMailProvider):
    name = "moemail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.api_key = str(entry["api_key"]).strip()
        raw_domains = entry.get("domain") or []
        if isinstance(raw_domains, list):
            self.domain = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            self.domain = [str(raw_domains).strip()] if str(raw_domains).strip() else []
        self.expiry_time = int(entry.get("expiry_time") or 0)
        self.session = _create_session(conf)

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers={"X-API-Key": self.api_key, "Content-Type": "application/json", "User-Agent": self.conf["user_agent"]}, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"MoEmail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"MoEmail {method} {path} 返回结构不是对象")
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        data = self._request("POST", "/api/emails/generate", payload={"name": username or _random_mailbox_name(), "expiryTime": self.expiry_time, "domain": _next_domain(self.domain)}, expected=(200, 201))
        address = str(data.get("email") or "").strip()
        email_id = str(data.get("id") or data.get("email_id") or "").strip()
        if not address or not email_id:
            raise RuntimeError("MoEmail 缺少 email 或 id")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "email_id": email_id}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        email_id = str(mailbox.get("email_id") or "").strip()
        if not email_id:
            raise RuntimeError("MoEmail 缺少 email_id")
        data = self._request("GET", f"/api/emails/{email_id}")
        items = data.get("messages") or []
        messages = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if not messages:
            return None
        _, item = max(enumerate(messages), key=lambda pair: (((_parse_received_at(pair[1].get("createdAt") or pair[1].get("created_at") or pair[1].get("receivedAt") or pair[1].get("date") or pair[1].get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp()), pair[0]))
        message_id = str(item.get("id") or item.get("message_id") or item.get("_id") or "").strip()
        detail = self._request("GET", f"/api/emails/{email_id}/{message_id}") if message_id else {"message": item}
        message = detail.get("message") if isinstance(detail.get("message"), dict) else detail
        text_content, html_content = _extract_content(message)
        sender = message.get("from") or message.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(message.get("subject") or item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(message.get("createdAt") or message.get("created_at") or message.get("receivedAt") or message.get("date") or message.get("timestamp") or item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": detail}

    def close(self) -> None:
        self.session.close()


class InbucketMailProvider(BaseMailProvider):
    name = "inbucket"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.api_key = str(entry.get("api_key") or "").strip()
        raw_domains = entry.get("domain") or []
        if isinstance(raw_domains, list):
            self.domain = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            self.domain = [str(raw_domains).strip()] if str(raw_domains).strip() else []
        self.random_subdomain = bool(entry.get("random_subdomain", True))
        # Inbucket is typically self-hosted; use per-provider proxy if set, otherwise NO proxy
        provider_proxy = str(entry.get("proxy") or "").strip()
        self.session = _create_session(conf, provider_proxy=provider_proxy if provider_proxy else "")
        self.session.headers.update({
            "User-Agent": conf["user_agent"],
            "Accept": "application/json",
        })
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    def _request(self, method: str, path: str, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(
            method.upper(),
            f"{self.api_base}{path}",
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        if resp.status_code not in expected:
            raise RuntimeError(f"Inbucket 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        if resp.status_code == 204:
            return {}
        content_type = str(resp.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            return resp.json()
        return resp.text

    def _resolve_domain(self) -> str:
        if self.domain:
            return _expand_wildcard_domain(_next_domain(self.domain))
        raise RuntimeError("Inbucket 需要至少配置一个 domain")

    def _mailbox_name(self, address: str) -> str:
        local_part, _, _ = str(address or "").partition("@")
        return local_part.strip()

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        local_part = username or _random_mailbox_name()
        domain = self._resolve_domain()
        address = f"{local_part}@{domain}"
        mailbox_name = self._mailbox_name(address)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "base_domain": domain,
            "mailbox_name": mailbox_name,
        }

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        mailbox_name = str(mailbox.get("mailbox_name") or self._mailbox_name(str(mailbox.get("address") or ""))).strip()
        if not mailbox_name:
            raise RuntimeError("Inbucket 缺少 mailbox_name")
        data = self._request("GET", f"/api/v1/mailbox/{mailbox_name}")
        items = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        if not items:
            return None
        items.sort(
            key=lambda value: (
                (_parse_received_at(value.get("date")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(),
                str(value.get("id") or ""),
            ),
            reverse=True,
        )
        address = str(mailbox.get("address") or "").strip()
        for item in items:
            message_id = str(item.get("id") or "").strip()
            if not message_id:
                continue
            detail = self._request("GET", f"/api/v1/mailbox/{mailbox_name}/{message_id}")
            if not isinstance(detail, dict):
                continue
            header = detail.get("header") if isinstance(detail.get("header"), dict) else {}
            body = detail.get("body") if isinstance(detail.get("body"), dict) else {}
            normalized = {
                "provider": self.name,
                "mailbox": mailbox_name,
                "message_id": message_id,
                "subject": str(detail.get("subject") or item.get("subject") or ""),
                "sender": str(detail.get("from") or item.get("from") or ""),
                "text_content": str(body.get("text") or ""),
                "html_content": str(body.get("html") or ""),
                "received_at": _parse_received_at(detail.get("date") or item.get("date")),
                "to": header.get("To") if isinstance(header, dict) else None,
                "raw": detail,
            }
            if _message_matches_email(normalized, address):
                return normalized
        return None

    def close(self) -> None:
        self.session.close()


class YydsMailProvider(BaseMailProvider):
    name = "yyds_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(entry["api_key"]).strip()
        self.domain = [str(item).strip() for item in (entry.get("domain") or []) if str(item).strip()]
        self.subdomain = str(entry.get("subdomain") or "").strip()
        self.wildcard = bool(entry.get("wildcard"))
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, token: str = "", params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200, 201, 204)):
        headers = {"Authorization": f"Bearer {token}"} if token else {"X-API-Key": self.api_key}
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers=headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"YYDSMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        if resp.status_code == 204:
            return {}
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"YYDSMail 请求失败: {data.get('errorCode') or data.get('error')}")
        return data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)) else data

    @staticmethod
    def _items(data):
        return data if isinstance(data, list) else data.get("items") or data.get("messages") or data.get("data") or []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload = {"localPart": username or _random_mailbox_name()}
        if self.domain:
            payload["domain"] = _next_domain(self.domain)
        if self.subdomain:
            payload["subdomain"] = self.subdomain
        data = self._request("POST", "/accounts/wildcard" if self.wildcard else "/accounts", payload=payload)
        address = str(data.get("address") or data.get("email") or "").strip()
        token = str(data.get("token") or data.get("temp_token") or data.get("tempToken") or data.get("access_token") or "").strip()
        if not address or not token:
            raise RuntimeError("YYDSMail 缺少 address 或 token")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token, "account_id": str(data.get("id") or "")}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/messages", token=str(mailbox.get("token") or ""), params={"address": mailbox["address"]})
        messages = [item for item in self._items(data) if isinstance(item, dict)]
        if not messages:
            return None
        item = max(messages, key=lambda value: ((_parse_received_at(value.get("createdAt") or value.get("created_at") or value.get("receivedAt") or value.get("date") or value.get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(), str(value.get("id") or "")))
        message_id = str(item.get("id") or item.get("message_id") or "").strip()
        if message_id:
            item = self._request("GET", f"/messages/{message_id}", token=str(mailbox.get("token") or ""), params={"address": mailbox["address"]})
        text_content, html_content = _extract_content(item)
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

    def close(self) -> None:
        self.session.close()


def _entries(mail_config: dict) -> list[dict]:
    result: list[dict] = []
    counters: dict[str, int] = {}
    for item in mail_config["providers"]:
        idx = len(result) + 1
        t = item.get("type", "")
        cnt = counters.get(t, 0) + 1
        counters[t] = cnt
        label = f"DDG-{cnt}" if t == "ddg_mail" else f"{t}#{idx}"
        result.append({**item, "provider_ref": f"{item['type']}#{idx}", "label": label})
    return result


def _enabled_entries(mail_config: dict) -> list[dict]:
    items = [item for item in _entries(mail_config) if item.get("enable")]
    if not items:
        raise RuntimeError("mail.providers 没有启用的 provider")
    return items


def _next_entry(mail_config: dict) -> dict:
    global provider_index
    items = _enabled_entries(mail_config)
    if len(items) == 1:
        return dict(items[0])
    with provider_lock:
        value = dict(items[provider_index % len(items)])
        provider_index = (provider_index + 1) % len(items)
        return value


def _create_provider(mail_config: dict, provider: str = "", provider_ref: str = "") -> BaseMailProvider:
    entry = next((dict(item) for item in _entries(mail_config) if provider_ref and item["provider_ref"] == provider_ref), None)
    entry = entry or next((dict(item) for item in _enabled_entries(mail_config) if provider and item["type"] == provider), None) or _next_entry(mail_config)
    conf = _config(mail_config)
    if entry["type"] == "cloudmail_gen":
        return CloudMailGenProvider(entry, conf)
    if entry["type"] == "cloudflare_temp_email":
        return CloudflareTempMailProvider(entry, conf)
    if entry["type"] == "ddg_mail":
        return DDGMailProvider(entry, conf)
    if entry["type"] == "tempmail_lol":
        return TempMailLolProvider(entry, conf)
    if entry["type"] == "duckmail":
        return DuckMailProvider(entry, conf)
    if entry["type"] == "gptmail":
        return GptMailProvider(entry, conf)
    if entry["type"] == "moemail":
        return MoEmailProvider(entry, conf)
    if entry["type"] == "inbucket":
        return InbucketMailProvider(entry, conf)
    if entry["type"] == "yyds_mail":
        return YydsMailProvider(entry, conf)
    raise RuntimeError(f"不支持的 mail.provider: {entry['type']}")


def create_mailbox(mail_config: dict, username: str | None = None) -> dict:
    enabled = _enabled_entries(mail_config)
    tried: set[str] = set()
    last_error = ""
    for _ in range(len(enabled)):
        provider = _create_provider(mail_config)
        provider_key = f"{provider.name}#{provider.provider_ref}"
        try:
            if provider_key in tried:
                continue
            tried.add(provider_key)
            mailbox = provider.create_mailbox(username)
            return mailbox
        except RuntimeError as error:
            last_error = str(error)
            if "DDG日上限已达" not in last_error:
                raise
        finally:
            provider.close()
    raise RuntimeError(last_error or "所有启用的邮箱提供商均无法创建邮箱")


def wait_for_code(mail_config: dict, mailbox: dict, stop_event: Any = None) -> str | None:
    provider = _create_provider(mail_config, str(mailbox.get("provider") or ""), str(mailbox.get("provider_ref") or ""))
    provider.stop_event = stop_event
    try:
        return provider.wait_for_code(mailbox)
    finally:
        provider.close()


def prepare_code_baseline(mail_config: dict, mailbox: dict) -> None:
    provider = _create_provider(mail_config, str(mailbox.get("provider") or ""), str(mailbox.get("provider_ref") or ""))
    try:
        provider.prepare_code_baseline(mailbox)
    finally:
        provider.close()


def mark_mailbox_result(mailbox: dict, *, success: bool, error: Exception | str | None = None) -> None:
    """注册流程结束后更新邮箱状态。

    当前各 provider（inbucket 等）均为一次性邮箱，无固定邮箱池状态需要跟踪，
    此函数保留为占位，供后续接入固定邮箱池（如 outlook_token）时扩展。
    """
    pass


def release_mailbox(mailbox: dict) -> None:
    """主动释放邮箱（当前为占位，供固定邮箱池扩展）。"""
    pass
