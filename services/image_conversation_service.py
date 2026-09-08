from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from services.config import DATA_DIR, config

DEFAULT_MAX_ITEMS = 200
# 每个对话最多保留的参考图数量，超出后从最旧的 turn 开始丢弃。
# 前端会先裁剪，这里是防止单条记录膨胀的兜底防线。
MAX_REFERENCE_IMAGES = 30
# 相对路径的最大长度，防止意外把大字符串当路径存进 JSON。
MAX_REL_LENGTH = 200

TURN_STATUSES = {"queued", "generating", "success", "error"}
IMAGE_STATUSES = {"loading", "success", "error"}
CONVERSATION_MODES = {"generate", "edit"}

_IMAGES_MARKER = "/images/"


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _conversation_key(owner_id: str, conversation_id: str) -> str:
    return f"{owner_id}:{conversation_id}"


def _timestamp(value: object) -> float:
    """解析前端时间戳：前端写的是 ISO 格式（带 Z），同时兼容后端 %Y-%m-%d %H:%M:%S。"""
    if not isinstance(value, str) or not value.strip():
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:26], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _is_safe_rel(rel: str) -> bool:
    """校验图片相对路径：拒绝绝对路径、目录穿越和异常长度的值。"""
    if not rel or len(rel) > MAX_REL_LENGTH or rel.startswith("/"):
        return False
    parts = PurePosixPath(rel).parts
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)


def _relative_from_url(value: object) -> str:
    """从图片地址提取裸相对路径。

    持久化只存 `2026/09/08/x.png` 这样的相对路径，读取时再按当前请求重算 URL，
    这样换端口／域名／IP 后历史图片不会全部失效。
    data: 内联图片一律丢弃 —— 参考图走 image_storage_service 落盘，
    生成图本来就是服务器 URL，两者都不该以 base64 形态进 JSON。
    """
    text = _clean(value)
    if not text or text.lower().startswith("data:"):
        return ""
    path = urlparse(text).path if "://" in text else text
    index = path.find(_IMAGES_MARKER)
    if index >= 0:
        return path[index + len(_IMAGES_MARKER):].lstrip("/")
    return path.lstrip("/")


def _normalize_reference_image(image: object) -> dict[str, Any]:
    """归一化参考图：只保留 name/type/rel，丢弃 dataUrl 与绝对 url。"""
    if not isinstance(image, dict):
        return {}
    rel = _relative_from_url(image.get("rel") or image.get("url"))
    if not rel or not _is_safe_rel(rel):
        return {}
    return {
        "name": _clean(image.get("name"), "reference.png"),
        "type": _clean(image.get("type"), "image/png"),
        "rel": rel,
    }


def _normalize_stored_image(image: object) -> dict[str, Any]:
    """归一化生成结果图：丢弃 b64_json，把 url 收敛成 rel。"""
    if not isinstance(image, dict):
        return {}
    image_id = _clean(image.get("id"))
    if not image_id:
        return {}
    rel = _relative_from_url(image.get("rel") or image.get("url"))
    if rel and not _is_safe_rel(rel):
        rel = ""
    status = _clean(image.get("status"))
    if status not in IMAGE_STATUSES:
        # 与前端 normalizeStoredImage 同款推导：有图即成功，否则视为生成中。
        status = "success" if rel else "loading"
    item: dict[str, Any] = {"id": image_id, "status": status}
    task_id = _clean(image.get("taskId"))
    if task_id:
        item["taskId"] = task_id
    if rel:
        item["rel"] = rel
    revised_prompt = _clean(image.get("revised_prompt"))
    if revised_prompt:
        item["revised_prompt"] = revised_prompt
    error = _clean(image.get("error"))
    if error:
        item["error"] = error
    return item


def _normalize_turn(turn: object) -> dict[str, Any]:
    """归一化单个对话轮次，保留前端的 camelCase 字段名与 ISO 时间戳原样。"""
    if not isinstance(turn, dict):
        return {}
    turn_id = _clean(turn.get("id"))
    if not turn_id:
        return {}
    reference_images = [
        item
        for item in (_normalize_reference_image(image) for image in (turn.get("referenceImages") or []))
        if item
    ] if isinstance(turn.get("referenceImages"), list) else []
    images = [
        item
        for item in (_normalize_stored_image(image) for image in (turn.get("images") or []))
        if item
    ] if isinstance(turn.get("images"), list) else []
    status = _clean(turn.get("status"))
    if status not in TURN_STATUSES:
        status = "generating" if any(item["status"] == "loading" for item in images) else "success"
    try:
        count = max(1, int(turn.get("count") or len(images) or 1))
    except (TypeError, ValueError):
        count = max(1, len(images) or 1)
    item: dict[str, Any] = {
        "id": turn_id,
        # 提示词全量保存，不做截断（用户明确要求）。
        "prompt": str(turn.get("prompt") or ""),
        "model": _clean(turn.get("model"), "gpt-image-2"),
        "mode": "edit" if turn.get("mode") == "edit" else "generate",
        "referenceImages": reference_images,
        "count": count,
        "size": _clean(turn.get("size")),
        "ratio": _clean(turn.get("ratio"), "1:1"),
        "tier": _clean(turn.get("tier"), "1k"),
        "quality": _clean(turn.get("quality"), "auto"),
        "images": images,
        "createdAt": _clean(turn.get("createdAt")),
        "status": status,
    }
    error = _clean(turn.get("error"))
    if error:
        item["error"] = error
    if turn.get("promptDeleted") is True:
        item["promptDeleted"] = True
    if turn.get("resultsDeleted") is True:
        item["resultsDeleted"] = True
    return item


def _prune_reference_images(turns: list[dict[str, Any]]) -> None:
    """把单个对话的参考图总数裁到 MAX_REFERENCE_IMAGES，优先保留最新 turn 的。"""
    total = sum(len(turn.get("referenceImages") or []) for turn in turns)
    if total <= MAX_REFERENCE_IMAGES:
        return
    # turns 按时间从旧到新排列，所以从头部开始丢，保住最新的参考图。
    for turn in turns:
        if total <= MAX_REFERENCE_IMAGES:
            break
        references = turn.get("referenceImages") or []
        if not references:
            continue
        overflow = total - MAX_REFERENCE_IMAGES
        dropped = min(overflow, len(references))
        turn["referenceImages"] = references[dropped:]
        total -= dropped


def _normalize_conversation(conversation: object, owner: str) -> dict[str, Any] | None:
    """归一化整个对话记录；缺少 id 时返回 None 由调用方丢弃。"""
    if not isinstance(conversation, dict):
        return None
    conversation_id = _clean(conversation.get("id"))
    if not conversation_id:
        return None
    turns = [
        item
        for item in (_normalize_turn(turn) for turn in (conversation.get("turns") or []))
        if item
    ] if isinstance(conversation.get("turns"), list) else []
    _prune_reference_images(turns)
    created_at = _clean(conversation.get("createdAt")) or (turns[0].get("createdAt") if turns else "")
    updated_at = _clean(conversation.get("updatedAt")) or (turns[-1].get("createdAt") if turns else "") or created_at
    return {
        "id": conversation_id,
        "owner_id": owner,
        "title": _clean(conversation.get("title")),
        "createdAt": created_at,
        "updatedAt": updated_at,
        "turns": turns,
    }


def _image_url(rel: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/images/{rel}"


def _public_reference_image(image: dict[str, Any], base_url: str) -> dict[str, Any]:
    return {
        "name": image.get("name"),
        "type": image.get("type"),
        "rel": image.get("rel"),
        "url": _image_url(str(image.get("rel") or ""), base_url),
    }


def _public_stored_image(image: dict[str, Any], base_url: str) -> dict[str, Any]:
    item = dict(image)
    rel = str(item.pop("rel", "") or "")
    if rel:
        item["rel"] = rel
        item["url"] = _image_url(rel, base_url)
    return item


def _public_conversation(conversation: dict[str, Any], base_url: str) -> dict[str, Any]:
    """对外序列化：丢弃 owner_id，并按当前请求把 rel 重算成可访问的 url。"""
    turns = []
    for turn in conversation.get("turns") or []:
        public_turn = dict(turn)
        public_turn["referenceImages"] = [
            _public_reference_image(image, base_url) for image in turn.get("referenceImages") or []
        ]
        public_turn["images"] = [_public_stored_image(image, base_url) for image in turn.get("images") or []]
        turns.append(public_turn)
    return {
        "id": conversation.get("id"),
        "title": conversation.get("title"),
        "createdAt": conversation.get("createdAt"),
        "updatedAt": conversation.get("updatedAt"),
        "turns": turns,
    }


class ImageConversationService:
    """画图对话记录的服务器端存储。

    结构与 ImageTaskService 一致：整表载入内存、单锁保护、同级 .tmp 原子重写。
    对话文本量级很小（单个对话约 50KB），所以不需要数据库。
    """

    def __init__(
        self,
        path: Path,
        *,
        max_items_getter: Callable[[], int] | None = None,
    ):
        self.path = path
        self.max_items_getter = max_items_getter or (lambda: config.image_conversation_max_items)
        self._lock = threading.RLock()
        self._conversations: dict[str, dict[str, Any]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._conversations = self._load_locked()
            if self._prune_locked():
                self._save_locked()

    def list_conversations(self, identity: dict[str, object], *, base_url: str = "") -> dict[str, Any]:
        owner = _owner_id(identity)
        with self._lock:
            if self._prune_locked():
                self._save_locked()
            items = [
                _public_conversation(conversation, base_url)
                for conversation in self._conversations.values()
                if conversation.get("owner_id") == owner
            ]
        items.sort(key=lambda item: _timestamp(item.get("updatedAt")), reverse=True)
        return {"items": items}

    def save_conversation(
        self,
        identity: dict[str, object],
        conversation: dict[str, Any],
        *,
        base_url: str = "",
    ) -> dict[str, Any]:
        owner = _owner_id(identity)
        conversation_id = _clean(conversation.get("id") if isinstance(conversation, dict) else "")
        if not conversation_id:
            raise ValueError("conversation id is required")
        self._save_many(identity, [conversation])
        with self._lock:
            # last-writer-wins 可能保留了服务器上的旧记录，所以返回实际存储的内容。
            stored = self._conversations.get(_conversation_key(owner, conversation_id))
            if stored is None:
                raise ValueError("conversation id is required")
            return _public_conversation(stored, base_url)

    def save_conversations(
        self,
        identity: dict[str, object],
        conversations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        saved = self._save_many(identity, conversations)
        return self._summary(identity, saved=saved)

    def import_conversations(
        self,
        identity: dict[str, object],
        conversations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """首次迁移：浏览器本地历史批量上传。已有记录一律以服务器为准，不覆盖。"""
        owner = _owner_id(identity)
        imported = 0
        written: set[str] = set()
        with self._lock:
            for raw in conversations if isinstance(conversations, list) else []:
                conversation = _normalize_conversation(raw, owner)
                if conversation is None:
                    continue
                key = _conversation_key(owner, str(conversation["id"]))
                if key in self._conversations:
                    continue
                self._conversations[key] = conversation
                written.add(key)
                imported += 1
            if imported:
                self._prune_locked(protect=written)
                self._save_locked()
        return self._summary(identity, imported=imported)

    def rename_conversation(
        self,
        identity: dict[str, object],
        conversation_id: str,
        title: str,
        *,
        base_url: str = "",
    ) -> dict[str, Any]:
        owner = _owner_id(identity)
        key = _conversation_key(owner, _clean(conversation_id))
        with self._lock:
            conversation = self._conversations.get(key)
            if conversation is None:
                raise ValueError("conversation not found")
            conversation["title"] = _clean(title)
            self._save_locked()
            return _public_conversation(conversation, base_url)

    def delete_conversation(self, identity: dict[str, object], conversation_id: str) -> dict[str, Any]:
        owner = _owner_id(identity)
        key = _conversation_key(owner, _clean(conversation_id))
        with self._lock:
            existed = self._conversations.pop(key, None) is not None
            if existed:
                self._save_locked()
        return {"ok": True, "deleted": existed}

    def clear_conversations(self, identity: dict[str, object]) -> dict[str, Any]:
        owner = _owner_id(identity)
        with self._lock:
            keys = [
                key for key, conversation in self._conversations.items() if conversation.get("owner_id") == owner
            ]
            for key in keys:
                self._conversations.pop(key, None)
            if keys:
                self._save_locked()
        return {"ok": True, "deleted": len(keys)}

    def _save_many(self, identity: dict[str, object], conversations: list[dict[str, Any]]) -> int:
        owner = _owner_id(identity)
        saved = 0
        written: set[str] = set()
        with self._lock:
            for raw in conversations if isinstance(conversations, list) else []:
                conversation = _normalize_conversation(raw, owner)
                if conversation is None:
                    continue
                key = _conversation_key(owner, str(conversation["id"]))
                # last-writer-wins：多标签页同时写时以 updatedAt 较新的一方为准，
                # 与前端 pickLatestConversation 的行为保持一致。
                current = self._conversations.get(key)
                if current is not None and _timestamp(current.get("updatedAt")) > _timestamp(conversation["updatedAt"]):
                    continue
                self._conversations[key] = conversation
                written.add(key)
                saved += 1
            if saved:
                self._prune_locked(protect=written)
                self._save_locked()
        return saved

    def _summary(self, identity: dict[str, object], *, saved: int = 0, imported: int = 0) -> dict[str, Any]:
        owner = _owner_id(identity)
        with self._lock:
            total = sum(1 for conversation in self._conversations.values() if conversation.get("owner_id") == owner)
        return {"ok": True, "saved": saved, "imported": imported, "total": total}

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        raw_items = raw.get("conversations") if isinstance(raw, dict) else raw
        if not isinstance(raw_items, list):
            return {}
        conversations: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            owner = _clean(item.get("owner_id"))
            if not owner:
                continue
            conversation = _normalize_conversation(item, owner)
            if conversation is None:
                continue
            conversations[_conversation_key(owner, str(conversation["id"]))] = conversation
        return conversations

    def _save_locked(self) -> None:
        items = sorted(
            self._conversations.values(),
            key=lambda item: _timestamp(item.get("updatedAt")),
            reverse=True,
        )
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps({"conversations": items}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def _prune_locked(self, protect: set[str] | None = None) -> bool:
        """按 owner 分组淘汰：每个身份最多保留 max_items 个对话，超出删 updatedAt 最旧的。

        protect 里的 key 本轮刚写入，一律不参与淘汰 —— 否则 updatedAt 撞在同一毫秒时，
        排序退化成插入顺序，新写的对话会被自己触发的淘汰删掉。
        """
        try:
            limit = max(1, int(self.max_items_getter()))
        except Exception:
            limit = DEFAULT_MAX_ITEMS
        protected = protect or set()
        keys_by_owner: dict[str, list[str]] = {}
        for key, conversation in self._conversations.items():
            keys_by_owner.setdefault(str(conversation.get("owner_id") or ""), []).append(key)
        removed: list[str] = []
        for keys in keys_by_owner.values():
            if len(keys) <= limit:
                continue
            by_recency = lambda key: _timestamp(self._conversations[key].get("updatedAt"))
            owned_protected = [key for key in keys if key in protected]
            candidates = [key for key in keys if key not in protected]
            if len(owned_protected) > limit:
                # 本轮写入的就已超限（例如一次导入几百条）：保护集合内部也要按新旧裁剪，
                # 否则「每 owner 最多 max_items 条」这个不变量要等到下次写入才恢复。
                owned_protected.sort(key=by_recency, reverse=True)
                removed.extend(owned_protected[limit:])
                removed.extend(candidates)
                continue
            candidates.sort(key=by_recency, reverse=True)
            removed.extend(candidates[limit - len(owned_protected):])
        for key in removed:
            self._conversations.pop(key, None)
        return bool(removed)


image_conversation_service = ImageConversationService(DATA_DIR / "image_conversations.json")
