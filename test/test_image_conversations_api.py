from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.image_conversations as image_conversations_module
from services.image_storage_service import StoredImage


AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}


def make_conversation(conversation_id: str = "conv-1") -> dict:
    return {
        "id": conversation_id,
        "title": "标题",
        "createdAt": "2026-09-08T10:00:00.000Z",
        "updatedAt": "2026-09-08T10:00:00.000Z",
        "turns": [],
    }


class FakeConversationService:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, args: tuple, kwargs: dict):
        self.calls.append((name, args, kwargs))

    def list_conversations(self, identity, **kwargs):
        self._record("list", (identity,), kwargs)
        return {"items": [make_conversation()]}

    def save_conversation(self, identity, conversation, **kwargs):
        self._record("save", (identity, conversation), kwargs)
        return conversation

    def save_conversations(self, identity, conversations):
        self._record("save_many", (identity, conversations), {})
        return {"ok": True, "saved": len(conversations), "imported": 0, "total": len(conversations)}

    def import_conversations(self, identity, conversations):
        self._record("import", (identity, conversations), {})
        return {"ok": True, "saved": 0, "imported": len(conversations), "total": len(conversations)}

    def rename_conversation(self, identity, conversation_id, title, **kwargs):
        self._record("rename", (identity, conversation_id, title), kwargs)
        if conversation_id == "missing":
            raise ValueError("conversation not found")
        return {**make_conversation(conversation_id), "title": title}

    def delete_conversation(self, identity, conversation_id):
        self._record("delete", (identity, conversation_id), {})
        return {"ok": True, "deleted": conversation_id != "missing"}

    def clear_conversations(self, identity):
        self._record("clear", (identity,), {})
        return {"ok": True, "deleted": 2}


class FakeStorageService:
    def __init__(self):
        self.saved: list[bytes] = []

    def save(self, image_data: bytes, base_url: str | None = None) -> StoredImage:
        self.saved.append(image_data)
        rel = f"2026/09/08/{len(self.saved)}_fake.png"
        return StoredImage(rel=rel, url=f"{(base_url or '').rstrip('/')}/images/{rel}", storage="local", size=len(image_data))


class ImageConversationsApiTests(unittest.TestCase):
    def setUp(self):
        self.fake_service = FakeConversationService()
        self.fake_storage = FakeStorageService()
        service_patcher = mock.patch.object(
            image_conversations_module, "image_conversation_service", self.fake_service
        )
        storage_patcher = mock.patch.object(
            image_conversations_module, "image_storage_service", self.fake_storage
        )
        service_patcher.start()
        storage_patcher.start()
        self.addCleanup(service_patcher.stop)
        self.addCleanup(storage_patcher.stop)
        app = FastAPI()
        app.include_router(image_conversations_module.create_router())
        self.client = TestClient(app)

    def test_list_conversations(self):
        response = self.client.get("/api/image-conversations", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["items"]), 1)
        self.assertEqual(self.fake_service.calls[0][0], "list")

    def test_save_conversation(self):
        response = self.client.post(
            "/api/image-conversations",
            headers=AUTH_HEADERS,
            json={"conversation": make_conversation()},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], "conv-1")
        name, args, _kwargs = self.fake_service.calls[0]
        self.assertEqual(name, "save")
        self.assertEqual(args[1]["id"], "conv-1")

    def test_bulk_save(self):
        response = self.client.post(
            "/api/image-conversations/bulk",
            headers=AUTH_HEADERS,
            json={"conversations": [make_conversation("a"), make_conversation("b")]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["saved"], 2)

    def test_import(self):
        response = self.client.post(
            "/api/image-conversations/import",
            headers=AUTH_HEADERS,
            json={"conversations": [make_conversation("a")]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["imported"], 1)

    def test_rename(self):
        response = self.client.patch(
            "/api/image-conversations/conv-1",
            headers=AUTH_HEADERS,
            json={"title": "新标题"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["title"], "新标题")

    def test_rename_missing_returns_400(self):
        response = self.client.patch(
            "/api/image-conversations/missing",
            headers=AUTH_HEADERS,
            json={"title": "新标题"},
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["error"], "conversation not found")

    def test_delete(self):
        response = self.client.delete("/api/image-conversations/conv-1", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["deleted"])

    def test_clear(self):
        response = self.client.delete("/api/image-conversations", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["deleted"], 2)

    def test_upload_references_returns_rel_in_order(self):
        response = self.client.post(
            "/api/image-references",
            headers=AUTH_HEADERS,
            files=[
                ("image", ("one.png", b"one", "image/png")),
                ("image", ("two.png", b"two", "image/png")),
            ],
        )

        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual([item["name"] for item in items], ["one.png", "two.png"])
        self.assertEqual([item["rel"] for item in items], ["2026/09/08/1_fake.png", "2026/09/08/2_fake.png"])
        self.assertEqual(self.fake_storage.saved, [b"one", b"two"])

    def test_upload_references_requires_at_least_one_image(self):
        response = self.client.post(
            "/api/image-references",
            headers=AUTH_HEADERS,
            data={},
        )

        self.assertEqual(response.status_code, 400, response.text)

    def test_upload_references_rejects_too_many(self):
        files = [("image", (f"{index}.png", b"x", "image/png")) for index in range(51)]

        response = self.client.post("/api/image-references", headers=AUTH_HEADERS, files=files)

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fake_storage.saved, [], "超限必须在落盘前拦住")

    def test_all_endpoints_require_auth(self):
        """未携带密钥时全部端点返回 401，且不会触达 service。"""
        responses = [
            self.client.get("/api/image-conversations"),
            self.client.post("/api/image-conversations", json={"conversation": make_conversation()}),
            self.client.post("/api/image-conversations/bulk", json={"conversations": []}),
            self.client.post("/api/image-conversations/import", json={"conversations": []}),
            self.client.patch("/api/image-conversations/conv-1", json={"title": "x"}),
            self.client.delete("/api/image-conversations/conv-1"),
            self.client.delete("/api/image-conversations"),
            self.client.post("/api/image-references", files=[("image", ("a.png", b"a", "image/png"))]),
        ]

        for response in responses:
            self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.fake_service.calls, [])
        self.assertEqual(self.fake_storage.saved, [])

    def test_invalid_key_returns_401(self):
        response = self.client.get(
            "/api/image-conversations", headers={"Authorization": "Bearer wrong-key"}
        )

        self.assertEqual(response.status_code, 401, response.text)


if __name__ == "__main__":
    unittest.main()
