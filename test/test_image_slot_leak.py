from __future__ import annotations

import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import AccountService
from services.config import config
from services.openai_backend_api import ImagePollTimeoutError
from services.protocol import conversation
from services.storage.json_storage import JSONStorageBackend


def _image_request(n: int = 1) -> types.SimpleNamespace:
    """构造 stream_image_outputs_with_pool 所需的最小 request。"""
    return types.SimpleNamespace(model="gpt-image-2", n=n, message_as_error=False, images=[])


class ImagePollTimeoutSlotReleaseTests(unittest.TestCase):
    """回归：生图轮询超时（ImagePollTimeoutError）必须归还并发闸门。

    历史 bug：conversation.py 的 ImagePollTimeoutError 分支只附加 account_email
    就 raise，唯独漏了释放 slot，导致 _image_inflight 计数泄漏、累积触顶后
    后续任务永久卡死在 running 且不会自动超时。
    """

    def test_poll_timeout_releases_image_slot(self) -> None:
        fake_account_service = mock.Mock()
        fake_account_service.get_available_access_token.return_value = "token-x"
        fake_account_service.get_account.return_value = {"email": "user@example.com"}

        def boom(*_args, **_kwargs):
            raise ImagePollTimeoutError("ChatGPT 生图超时")

        with mock.patch.object(conversation, "account_service", fake_account_service), \
                mock.patch.object(conversation, "OpenAIBackendAPI", mock.Mock()), \
                mock.patch.object(conversation, "stream_image_outputs", boom):
            with self.assertRaises(ImagePollTimeoutError):
                # 生成器需被消费才会执行到异常分支
                list(conversation.stream_image_outputs_with_pool(_image_request()))

        # 关键断言：超时路径必须归还 slot，且不计入 fail 统计（超时非账号失败）
        fake_account_service.release_image_slot.assert_called_once_with("token-x")
        fake_account_service.mark_image_result.assert_not_called()

    def test_generation_error_still_marks_failure(self) -> None:
        """对照：非超时的普通异常仍走 mark_image_result（内部含释放），语义未被破坏。"""
        fake_account_service = mock.Mock()
        fake_account_service.get_available_access_token.return_value = "token-y"
        fake_account_service.get_account.return_value = {"email": "user@example.com"}

        def boom(*_args, **_kwargs):
            raise RuntimeError("upstream 500")

        with mock.patch.object(conversation, "account_service", fake_account_service), \
                mock.patch.object(conversation, "OpenAIBackendAPI", mock.Mock()), \
                mock.patch.object(conversation, "stream_image_outputs", boom):
            with self.assertRaises(conversation.ImageGenerationError):
                list(conversation.stream_image_outputs_with_pool(_image_request()))

        fake_account_service.mark_image_result.assert_called_once_with("token-y", False)


class ImageSlotAcquireTimeoutTests(unittest.TestCase):
    """回归：并发闸门占满时，获取 token 必须有总超时预算，不得永久阻塞。

    纵深防御：即便未来出现新的 slot 泄漏路径，_acquire_next_candidate_token
    也会在 image_poll_timeout_secs 内抛 RuntimeError，任务转 error 而非卡死。
    """

    def test_acquire_times_out_when_all_slots_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [{"access_token": "token-full", "type": "Plus", "status": "正常", "quota": 5}]
            )
            # 人为把该账号的 in-flight 填满到并发上限，模拟 slot 泄漏后的触顶状态
            with service._image_slot_condition:
                service._image_inflight["token-full"] = max(1, int(config.image_account_concurrency))

            # 超时预算压到 1 秒，验证"会超时"而非"永久阻塞"
            with mock.patch.dict(config.data, {"image_poll_timeout_secs": 1}):
                start = time.monotonic()
                with self.assertRaises(RuntimeError) as ctx:
                    service._acquire_next_candidate_token()
                elapsed = time.monotonic() - start

            self.assertIn("超时", str(ctx.exception))
            self.assertLess(elapsed, 5.0, "应在预算内快速超时，而非无限等待")

    def test_acquire_succeeds_after_slot_released(self) -> None:
        """对照：释放 slot 后应能正常获取，证明超时逻辑未误伤正常路径。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [{"access_token": "token-ok", "type": "Plus", "status": "正常", "quota": 5}]
            )
            with mock.patch.dict(config.data, {"image_poll_timeout_secs": 5}):
                token = service._acquire_next_candidate_token()
            self.assertEqual(token, "token-ok")
            self.assertEqual(service._image_inflight.get("token-ok"), 1)

            service.release_image_slot("token-ok")
            self.assertNotIn("token-ok", service._image_inflight)


if __name__ == "__main__":
    unittest.main()
