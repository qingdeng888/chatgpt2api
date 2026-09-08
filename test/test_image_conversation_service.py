from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.image_conversation_service import ImageConversationService


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}
BASE_URL = "http://local.test"


def make_turn(turn_id: str = "turn-1", **overrides) -> dict:
    turn = {
        "id": turn_id,
        "prompt": "一只猫",
        "model": "gpt-image-2",
        "mode": "generate",
        "referenceImages": [],
        "count": 1,
        "size": "1024x1024",
        "ratio": "1:1",
        "tier": "1k",
        "quality": "auto",
        "images": [],
        "createdAt": "2026-09-08T10:00:00.000Z",
        "status": "success",
    }
    turn.update(overrides)
    return turn


def make_conversation(conversation_id: str = "conv-1", **overrides) -> dict:
    conversation = {
        "id": conversation_id,
        "title": "标题",
        "createdAt": "2026-09-08T10:00:00.000Z",
        "updatedAt": "2026-09-08T10:00:00.000Z",
        "turns": [make_turn()],
    }
    conversation.update(overrides)
    return conversation


class ImageConversationServiceTests(unittest.TestCase):
    def make_service(self, path: Path, max_items: int = 200) -> ImageConversationService:
        return ImageConversationService(path, max_items_getter=lambda: max_items)

    def test_save_and_list_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(OWNER, make_conversation(), base_url=BASE_URL)

            items = service.list_conversations(OWNER, base_url=BASE_URL)["items"]

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["id"], "conv-1")
            self.assertEqual(items[0]["turns"][0]["prompt"], "一只猫")

    def test_public_payload_drops_owner_id(self):
        """租户隔离：owner_id 只用于内部分组，绝不能出现在接口响应里。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(OWNER, make_conversation(), base_url=BASE_URL)

            payload = json.dumps(service.list_conversations(OWNER, base_url=BASE_URL))

            self.assertNotIn("owner_id", payload)
            self.assertNotIn(OWNER["id"], payload)

    def test_owner_isolation(self):
        """每个登录身份只能看到自己的对话。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(OWNER, make_conversation("mine"), base_url=BASE_URL)
            service.save_conversation(OTHER_OWNER, make_conversation("theirs"), base_url=BASE_URL)

            self.assertEqual(
                [item["id"] for item in service.list_conversations(OWNER, base_url=BASE_URL)["items"]],
                ["mine"],
            )
            self.assertEqual(
                [item["id"] for item in service.list_conversations(OTHER_OWNER, base_url=BASE_URL)["items"]],
                ["theirs"],
            )

    def test_same_conversation_id_is_isolated_per_owner(self):
        """不同身份用相同对话 id 不会互相覆盖。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(OWNER, make_conversation("shared", title="甲"), base_url=BASE_URL)
            service.save_conversation(OTHER_OWNER, make_conversation("shared", title="乙"), base_url=BASE_URL)

            self.assertEqual(
                service.list_conversations(OWNER, base_url=BASE_URL)["items"][0]["title"], "甲"
            )
            self.assertEqual(
                service.list_conversations(OTHER_OWNER, base_url=BASE_URL)["items"][0]["title"], "乙"
            )

    def test_url_is_normalized_to_relative_path(self):
        """持久化只存裸 rel，换端口/域名/IP 后历史图片不会全部失效。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_conversations.json"
            service = self.make_service(path)
            service.save_conversation(
                OWNER,
                make_conversation(
                    turns=[
                        make_turn(
                            images=[
                                {
                                    "id": "img-1",
                                    "status": "success",
                                    "url": "http://23.106.46.224:13003/images/2026/09/08/out.png",
                                    "revised_prompt": "改写后的提示词",
                                }
                            ]
                        )
                    ]
                ),
                base_url=BASE_URL,
            )

            stored = json.loads(path.read_text(encoding="utf-8"))

            self.assertNotIn("23.106.46.224", json.dumps(stored, ensure_ascii=False))
            self.assertNotIn("13003", json.dumps(stored, ensure_ascii=False))
            image = stored["conversations"][0]["turns"][0]["images"][0]
            self.assertEqual(image["rel"], "2026/09/08/out.png")
            self.assertNotIn("url", image)

    def test_url_is_recomputed_per_request(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(
                OWNER,
                make_conversation(turns=[make_turn(images=[{"id": "i", "rel": "2026/09/08/a.png"}])]),
                base_url=BASE_URL,
            )

            image = service.list_conversations(OWNER, base_url="http://other.host:9000")["items"][0]["turns"][0]["images"][0]

            self.assertEqual(image["url"], "http://other.host:9000/images/2026/09/08/a.png")
            self.assertEqual(image["rel"], "2026/09/08/a.png")

    def test_base64_is_never_persisted(self):
        """生成结果图与内联参考图都不得以 base64 形态进 JSON。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_conversations.json"
            service = self.make_service(path)
            service.save_conversation(
                OWNER,
                make_conversation(
                    turns=[
                        make_turn(
                            mode="edit",
                            referenceImages=[
                                {"name": "r.png", "type": "image/png", "dataUrl": "data:image/png;base64,QUJDREVG"}
                            ],
                            images=[{"id": "i1", "status": "success", "b64_json": "QUJDREVG"}],
                        )
                    ]
                ),
                base_url=BASE_URL,
            )

            raw = path.read_text(encoding="utf-8")

            self.assertNotIn("QUJDREVG", raw)
            self.assertNotIn("dataUrl", raw)
            turn = json.loads(raw)["conversations"][0]["turns"][0]
            self.assertEqual(turn["referenceImages"], [])
            self.assertNotIn("b64_json", turn["images"][0])

    def test_path_traversal_reference_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_conversations.json"
            service = self.make_service(path)
            service.save_conversation(
                OWNER,
                make_conversation(
                    turns=[
                        make_turn(
                            mode="edit",
                            referenceImages=[{"name": "x.png", "type": "image/png", "rel": "../../etc/passwd"}],
                        )
                    ]
                ),
                base_url=BASE_URL,
            )

            turn = json.loads(path.read_text(encoding="utf-8"))["conversations"][0]["turns"][0]

            self.assertEqual(turn["referenceImages"], [])

    def test_last_writer_wins_rejects_stale_update(self):
        """多标签页同时写时以 updatedAt 较新的一方为准。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(
                OWNER,
                make_conversation(title="新", updatedAt="2026-09-08T12:00:00.000Z"),
                base_url=BASE_URL,
            )

            service.save_conversation(
                OWNER,
                make_conversation(title="旧", updatedAt="2026-09-08T10:00:00.000Z"),
                base_url=BASE_URL,
            )

            self.assertEqual(
                service.list_conversations(OWNER, base_url=BASE_URL)["items"][0]["title"], "新"
            )

    def test_last_writer_wins_accepts_newer_update(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(
                OWNER,
                make_conversation(title="旧", updatedAt="2026-09-08T10:00:00.000Z"),
                base_url=BASE_URL,
            )

            service.save_conversation(
                OWNER,
                make_conversation(title="新", updatedAt="2026-09-08T12:00:00.000Z"),
                base_url=BASE_URL,
            )

            self.assertEqual(
                service.list_conversations(OWNER, base_url=BASE_URL)["items"][0]["title"], "新"
            )

    def test_reference_images_are_capped_per_conversation(self):
        """每个对话最多保留 30 张参考图，优先保留最新 turn 的。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            turns = [
                make_turn(
                    f"turn-{index}",
                    mode="edit",
                    referenceImages=[
                        {"name": f"{index}-{slot}.png", "type": "image/png", "rel": f"2026/09/0{index + 1}/{slot}.png"}
                        for slot in range(10)
                    ],
                )
                for index in range(4)
            ]

            service.save_conversation(OWNER, make_conversation(turns=turns), base_url=BASE_URL)

            stored = service.list_conversations(OWNER, base_url=BASE_URL)["items"][0]["turns"]
            total = sum(len(turn["referenceImages"]) for turn in stored)
            self.assertEqual(total, 30)
            # 从最旧的 turn 开始丢，最新的 turn 保持完整
            self.assertEqual(len(stored[-1]["referenceImages"]), 10)
            self.assertEqual(len(stored[0]["referenceImages"]), 0)

    def test_missing_conversation_id_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")

            with self.assertRaises(ValueError):
                service.save_conversation(OWNER, {"title": "无 id"}, base_url=BASE_URL)

    def test_rename_missing_conversation_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")

            with self.assertRaises(ValueError):
                service.rename_conversation(OWNER, "nope", "标题", base_url=BASE_URL)

    def test_rename_and_delete_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(OWNER, make_conversation("a"), base_url=BASE_URL)
            service.save_conversation(OWNER, make_conversation("b"), base_url=BASE_URL)

            renamed = service.rename_conversation(OWNER, "a", "改名后", base_url=BASE_URL)
            self.assertEqual(renamed["title"], "改名后")

            self.assertTrue(service.delete_conversation(OWNER, "a")["deleted"])
            self.assertFalse(service.delete_conversation(OWNER, "a")["deleted"])
            self.assertEqual(len(service.list_conversations(OWNER, base_url=BASE_URL)["items"]), 1)

            self.assertEqual(service.clear_conversations(OWNER)["deleted"], 1)
            self.assertEqual(service.list_conversations(OWNER, base_url=BASE_URL)["items"], [])

    def test_delete_does_not_touch_other_owner(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(OWNER, make_conversation("shared"), base_url=BASE_URL)
            service.save_conversation(OTHER_OWNER, make_conversation("shared"), base_url=BASE_URL)

            service.delete_conversation(OWNER, "shared")

            self.assertEqual(len(service.list_conversations(OWNER, base_url=BASE_URL)["items"]), 0)
            self.assertEqual(len(service.list_conversations(OTHER_OWNER, base_url=BASE_URL)["items"]), 1)

    def test_import_skips_existing_conversations(self):
        """首次迁移：服务器上已有的记录一律以服务器为准，不覆盖。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(OWNER, make_conversation("shared", title="服务器版"), base_url=BASE_URL)

            result = service.import_conversations(
                OWNER,
                [make_conversation("shared", title="浏览器版"), make_conversation("fresh")],
            )

            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["total"], 2)
            titles = {item["id"]: item["title"] for item in service.list_conversations(OWNER, base_url=BASE_URL)["items"]}
            self.assertEqual(titles["shared"], "服务器版")
            self.assertEqual(titles["fresh"], "标题")

    def test_prune_keeps_most_recent_per_owner(self):
        """超出上限时按 owner 分别淘汰 updatedAt 最旧的。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json", max_items=2)
            for conversation_id, day in [("c1", "08"), ("c2", "02"), ("c3", "03"), ("c4", "04")]:
                service.save_conversation(
                    OWNER,
                    make_conversation(conversation_id, updatedAt=f"2026-09-{day}T10:00:00.000Z"),
                    base_url=BASE_URL,
                )
                service.save_conversation(OTHER_OWNER, make_conversation(conversation_id), base_url=BASE_URL)

            self.assertEqual(
                {item["id"] for item in service.list_conversations(OWNER, base_url=BASE_URL)["items"]},
                {"c1", "c4"},
            )
            # 淘汰是按 owner 分组的，不能把别人的对话也删掉
            self.assertEqual(len(service.list_conversations(OTHER_OWNER, base_url=BASE_URL)["items"]), 2)

    def test_write_is_not_pruned_by_itself_when_timestamps_collide(self):
        """回归：updatedAt 撞在同一毫秒时，本轮写入的对话不能被自己触发的淘汰删掉。

        旧实现按 updatedAt 排序后砍掉尾部，排序退化成插入顺序，
        导致刚写入的记录被淘汰、save_conversation 抛 ValueError。
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json", max_items=2)
            same = "2026-09-08T10:00:00.000Z"

            service.save_conversation(OWNER, make_conversation("c1", updatedAt=same), base_url=BASE_URL)
            service.save_conversation(OWNER, make_conversation("c2", updatedAt=same), base_url=BASE_URL)
            # 第三次写入不应抛异常，且新记录必须在场
            service.save_conversation(OWNER, make_conversation("c3", updatedAt=same), base_url=BASE_URL)

            items = service.list_conversations(OWNER, base_url=BASE_URL)["items"]
            self.assertEqual(len(items), 2)
            self.assertIn("c3", {item["id"] for item in items})

    def test_import_larger_than_limit_keeps_newest(self):
        """一次导入超过上限时，保留 updatedAt 最新的若干条。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json", max_items=2)
            conversations = [
                make_conversation(f"c{index}", updatedAt=f"2026-09-{index + 1:02d}T10:00:00.000Z")
                for index in range(5)
            ]

            result = service.import_conversations(OWNER, conversations)

            # c0..c4 的 updatedAt 依次是 09-01..09-05，最新两条是 c4、c3
            items = service.list_conversations(OWNER, base_url=BASE_URL)["items"]
            self.assertEqual(len(items), 2)
            self.assertEqual({item["id"] for item in items}, {"c3", "c4"})
            self.assertEqual(result["imported"], 5)

    def test_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_conversations.json"
            self.make_service(path).save_conversation(OWNER, make_conversation(), base_url=BASE_URL)

            reloaded = self.make_service(path)

            self.assertEqual(len(reloaded.list_conversations(OWNER, base_url=BASE_URL)["items"]), 1)

    def test_load_accepts_bare_list(self):
        """磁盘格式容错：同时接受 {"conversations": [...]} 信封与裸 list。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_conversations.json"
            path.write_text(
                json.dumps([make_conversation() | {"owner_id": OWNER["id"]}], ensure_ascii=False),
                encoding="utf-8",
            )

            service = self.make_service(path)

            self.assertEqual(len(service.list_conversations(OWNER, base_url=BASE_URL)["items"]), 1)

    def test_records_without_owner_are_skipped(self):
        """缺 owner_id 的记录无法归属，直接跳过（防止越权读到无主数据）。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_conversations.json"
            path.write_text(json.dumps([make_conversation()], ensure_ascii=False), encoding="utf-8")

            service = self.make_service(path)

            self.assertEqual(service.list_conversations(OWNER, base_url=BASE_URL)["items"], [])

    def test_corrupted_file_falls_back_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_conversations.json"
            path.write_text("{ 不是合法 JSON", encoding="utf-8")

            service = self.make_service(path)

            self.assertEqual(service.list_conversations(OWNER, base_url=BASE_URL)["items"], [])

    def test_malformed_turns_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            service.save_conversation(
                OWNER,
                make_conversation(turns=["不是对象", {"没有 id": True}, make_turn("good")]),
                base_url=BASE_URL,
            )

            turns = service.list_conversations(OWNER, base_url=BASE_URL)["items"][0]["turns"]

            self.assertEqual([turn["id"] for turn in turns], ["good"])

    def test_legacy_turn_without_turns_field(self):
        """兼容早期形态：对话对象上直接挂 prompt/images 而没有 turns。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")

            service.save_conversation(OWNER, {"id": "legacy", "title": "旧版"}, base_url=BASE_URL)

            items = service.list_conversations(OWNER, base_url=BASE_URL)["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["turns"], [])

    def test_timestamps_keep_iso_format(self):
        """时间戳原样透传：前端用 localeCompare 排序、new Date() 解析，
        换成后端的 %Y-%m-%d %H:%M:%S 会在 Safari 解析成 NaN。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")

            service.save_conversation(
                OWNER,
                make_conversation(updatedAt="2026-09-08T10:00:00.000Z"),
                base_url=BASE_URL,
            )

            self.assertEqual(
                service.list_conversations(OWNER, base_url=BASE_URL)["items"][0]["updatedAt"],
                "2026-09-08T10:00:00.000Z",
            )

    def test_list_is_sorted_by_updated_at_desc(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_conversations.json")
            for conversation_id, day in [("old", "01"), ("new", "09"), ("mid", "05")]:
                service.save_conversation(
                    OWNER,
                    make_conversation(conversation_id, updatedAt=f"2026-09-{day}T10:00:00.000Z"),
                    base_url=BASE_URL,
                )

            self.assertEqual(
                [item["id"] for item in service.list_conversations(OWNER, base_url=BASE_URL)["items"]],
                ["new", "mid", "old"],
            )


if __name__ == "__main__":
    unittest.main()
