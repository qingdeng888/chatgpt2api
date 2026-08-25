"""测试 refresh_accounts 的 remove_failed 分支：
- InvalidAccessTokenError（token 真失效）时删除账号
- 其他异常（瞬态网络错误等）不删除，只记录 errors
"""
import unittest
from unittest.mock import patch

from services.account_service import AccountService


class MemoryStorage:
    def __init__(self, accounts=None) -> None:
        self.accounts = list(accounts or [])

    def load_accounts(self):
        return list(self.accounts)

    def save_accounts(self, accounts) -> None:
        self.accounts = list(accounts)

    def load_auth_keys(self):
        return []

    def save_auth_keys(self, auth_keys) -> None:
        pass

    def health_check(self) -> dict:
        return {"ok": True}

    def get_backend_info(self) -> dict:
        return {"type": "memory"}


def make_account(token: str) -> dict:
    return {
        "access_token": token,
        "email": f"{token}@example.com",
        "status": "正常运行",
    }


class RefreshAccountsRemoveFailedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()
        self.service = AccountService(storage_backend=self.storage)

    def _seed(self, tokens: list[str]) -> list[str]:
        self.service.add_account_items([make_account(token) for token in tokens])
        return tokens

    def test_invalid_token_error_removes_account(self) -> None:
        from services.openai_backend_api import InvalidAccessTokenError

        tokens = self._seed(["tok-good", "tok-bad"])
        def raiser(access_token, event="fetch_remote_info"):
            if access_token == "tok-bad":
                raise InvalidAccessTokenError("token invalid")
            return {"email": "good@example.com", "status": "正常"}

        with patch.object(self.service, "fetch_remote_info", side_effect=raiser):
            result = self.service.refresh_accounts(tokens, remove_failed=True)
        remaining = [a["email"].split("@")[0] for a in self.storage.accounts]
        self.assertIn("tok-good", remaining)
        self.assertNotIn("tok-bad", remaining)
        self.assertEqual(len(result["errors"]), 1)

    def test_transient_error_does_not_remove_account(self) -> None:
        tokens = self._seed(["tok-a", "tok-b"])
        def raiser(access_token, event="fetch_remote_info"):
            if access_token == "tok-b":
                raise RuntimeError("temporary network timeout")
            return {"email": "good@example.com", "status": "正常"}

        with patch.object(self.service, "fetch_remote_info", side_effect=raiser):
            result = self.service.refresh_accounts(tokens, remove_failed=True)
        remaining = [a["email"].split("@")[0] for a in self.storage.accounts]
        # 瞬态错误不删除任何账号
        self.assertIn("tok-a", remaining)
        self.assertIn("tok-b", remaining)
        self.assertEqual(len(result["errors"]), 1)


if __name__ == "__main__":
    unittest.main()