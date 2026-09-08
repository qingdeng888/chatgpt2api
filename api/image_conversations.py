from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.image_inputs import ImageInput, parse_image_reference_sources, read_image_sources
from api.support import require_identity, resolve_image_base_url
from services.image_conversation_service import image_conversation_service
from services.image_storage_service import image_storage_service

# 单次上传参考图的数量上限。每个对话只保留 30 张（服务端会裁剪），
# 这里留出余量，主要防止一次请求写入过多文件。
MAX_REFERENCE_UPLOADS = 50


class ImageConversationSaveRequest(BaseModel):
    conversation: dict[str, Any]


class ImageConversationBulkRequest(BaseModel):
    conversations: list[dict[str, Any]] = Field(default_factory=list)


class ImageConversationRenameRequest(BaseModel):
    title: str = ""


def _store_references(images: list[ImageInput], base_url: str) -> list[dict[str, Any]]:
    """参考图落盘：返回与入参同序的 rel 列表，前端据此回填对话记录。"""
    items = []
    for data, filename, mime_type in images:
        stored = image_storage_service.save(data, base_url)
        items.append(
            {
                "rel": stored.rel,
                "url": stored.url,
                "name": str(filename or "").strip() or "reference.png",
                "type": str(mime_type or "").strip() or "image/png",
            }
        )
    return items


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/image-conversations")
    async def list_image_conversations(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return await run_in_threadpool(
            image_conversation_service.list_conversations,
            identity,
            base_url=resolve_image_base_url(request),
        )

    @router.post("/api/image-conversations")
    async def save_image_conversation(
        body: ImageConversationSaveRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            return await run_in_threadpool(
                image_conversation_service.save_conversation,
                identity,
                body.conversation,
                base_url=resolve_image_base_url(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/image-conversations/bulk")
    async def save_image_conversations_bulk(
        body: ImageConversationBulkRequest,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            return await run_in_threadpool(
                image_conversation_service.save_conversations,
                identity,
                body.conversations,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/image-conversations/import")
    async def import_image_conversations(
        body: ImageConversationBulkRequest,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            return await run_in_threadpool(
                image_conversation_service.import_conversations,
                identity,
                body.conversations,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.patch("/api/image-conversations/{conversation_id}")
    async def rename_image_conversation(
        conversation_id: str,
        body: ImageConversationRenameRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            return await run_in_threadpool(
                image_conversation_service.rename_conversation,
                identity,
                conversation_id,
                body.title,
                base_url=resolve_image_base_url(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.delete("/api/image-conversations/{conversation_id}")
    async def delete_image_conversation(
        conversation_id: str,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return await run_in_threadpool(
            image_conversation_service.delete_conversation,
            identity,
            conversation_id,
        )

    @router.delete("/api/image-conversations")
    async def clear_image_conversations(
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return await run_in_threadpool(image_conversation_service.clear_conversations, identity)

    @router.post("/api/image-references")
    async def create_image_references(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        """上传参考图换取服务器相对路径。

        对话记录只存 rel 不存 base64（30 张 base64 约 88MB，落盘后只需约 1KB），
        所以前端保存对话前必须先调这个接口。
        """
        require_identity(authorization)
        sources = await parse_image_reference_sources(request)
        if len(sources) > MAX_REFERENCE_UPLOADS:
            raise HTTPException(
                status_code=400,
                detail={"error": f"单次最多上传 {MAX_REFERENCE_UPLOADS} 张参考图"},
            )
        images = await read_image_sources(sources)
        return {"items": await run_in_threadpool(_store_references, images, resolve_image_base_url(request))}

    return router
