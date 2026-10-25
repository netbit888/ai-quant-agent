"""换供应商脚本的纯逻辑测试（不联网、不碰真实 .env）。"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import llm
from tools import switch_llm as switch_mod
from tools.switch_llm import PROVIDERS, provider_of, upsert_env


class TestUpsertEnv(unittest.TestCase):
    def test_replaces_existing_key(self):
        text = "DEEPSEEK_API_KEY=sk-old\nLLM_MODEL=deepseek-chat\n"
        out = upsert_env(text, {"LLM_MODEL": "glm-4-flash"})
        self.assertIn("LLM_MODEL=glm-4-flash", out)
        self.assertNotIn("LLM_MODEL=deepseek-chat", out)
        self.assertIn("DEEPSEEK_API_KEY=sk-old", out)   # 别动无关配置

    def test_appends_when_missing(self):
        out = upsert_env("DEEPSEEK_API_KEY=sk-old\n", {"LLM_API_KEY": "abc"})
        self.assertIn("LLM_API_KEY=abc", out)

    def test_commented_line_is_not_treated_as_present(self):
        """`# LLM_MODEL=...` 是给人看的示例，不能被当成已有配置去覆盖。"""
        text = "# LLM_MODEL=glm-4-flash\nDEEPSEEK_API_KEY=sk-old\n"
        out = upsert_env(text, {"LLM_MODEL": "glm-4.7-flash"})
        self.assertIn("# LLM_MODEL=glm-4-flash", out)      # 示例原样保留
        self.assertIn("LLM_MODEL=glm-4.7-flash", out)      # 新增生效行

    def test_idempotent(self):
        once = upsert_env("", {"LLM_MODEL": "x"})
        twice = upsert_env(once, {"LLM_MODEL": "x"})
        self.assertEqual(once, twice)


class TestProviders(unittest.TestCase):
    def test_all_providers_have_three_fields(self):
        for name, spec in PROVIDERS.items():
            self.assertTrue(spec["base_url"].startswith("https://"), name)
            self.assertTrue(spec["model"], name)

    def test_zhipu_points_at_v4(self):
        # /api/paas/v4 少一段就是 404，这个坑很容易踩
        self.assertTrue(PROVIDERS["zhipu"]["base_url"].endswith("/api/paas/v4"))

    def test_every_provider_lists_its_default_model(self):
        """面板的模型下拉用它；默认模型漏了，卡片上会出现一个空列表。"""
        for name, spec in PROVIDERS.items():
            self.assertIn(spec["model"], spec["models"], name)


class TestProviderOf(unittest.TestCase):
    def test_recognizes_known_base_url(self):
        self.assertEqual(provider_of("https://open.bigmodel.cn/api/paas/v4"), "zhipu")
        self.assertEqual(provider_of("https://api.deepseek.com"), "deepseek")

    def test_trailing_slash_and_case_do_not_matter(self):
        self.assertEqual(provider_of("https://API.DeepSeek.com/"), "deepseek")

    def test_unknown_returns_empty(self):
        """认不出来就老实说不知道，别猜成某一家（猜错会显示错供应商名）。"""
        self.assertEqual(provider_of("https://example.com/v1"), "")
        self.assertEqual(provider_of(None), "")


class TestSwitchWritesEnv(unittest.TestCase):
    """写 .env 的行为。用临时文件，网络与进程环境全部打桩。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = Path(self.tmp) / ".env"
        self.env.write_text(
            "DEEPSEEK_API_KEY=sk-old\nLLM_MODEL=old-model\n", encoding="utf-8"
        )
        self._orig_env_file = switch_mod.ENV_FILE
        switch_mod.ENV_FILE = self.env
        self.chat = mock.patch.object(llm, "chat_ex", lambda *a, **k: ("可用", {}))
        self.apply = mock.patch.object(llm, "apply_config", lambda **kw: {})
        self.conf = mock.patch.object(
            llm, "current_config",
            lambda: {"base_url": "https://x", "model": "old-model",
                     "key_set": True, "key_tail": "old1"},
        )
        self.chat.start(); self.apply.start(); self.conf.start()

    def tearDown(self):
        self.chat.stop(); self.apply.stop(); self.conf.stop()
        switch_mod.ENV_FILE = self._orig_env_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_key_optional_keeps_existing(self):
        """只换模型时不填 key：原来的 DEEPSEEK_API_KEY 不能被清掉。"""
        res = switch_mod.switch("zhipu", key=None, model="glm-4-air")
        text = self.env.read_text(encoding="utf-8")
        self.assertTrue(res["ok"])
        self.assertIn("DEEPSEEK_API_KEY=sk-old", text)
        self.assertNotIn("LLM_API_KEY=", text)      # 没给就别凭空写一个
        self.assertIn("LLM_MODEL=glm-4-air", text)
        self.assertIn("LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4", text)
        self.assertFalse(res["key_changed"])

    def test_key_given_is_written(self):
        res = switch_mod.switch("zhipu", key="new-key", model="glm-4-flash")
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("LLM_API_KEY=new-key", text)
        self.assertTrue(res["key_changed"])

    def test_blank_model_falls_back_to_provider_default(self):
        """模型框留空 = 用该家默认模型，别把空字符串写进 .env。"""
        switch_mod.switch("deepseek", key="k", model="   ")
        self.assertIn("LLM_MODEL=deepseek-chat",
                      self.env.read_text(encoding="utf-8"))

    def test_failed_verify_restores_file(self):
        """验证失败必须把文件还原，别把人留在坏配置上。"""
        before = self.env.read_text(encoding="utf-8")
        self.chat.stop()
        self.chat = mock.patch.object(
            llm, "chat_ex",
            side_effect=RuntimeError("401 invalid key"),
        )
        self.chat.start()
        res = switch_mod.switch("zhipu", key="bad", model="glm-4-flash")
        self.assertFalse(res["ok"])
        self.assertIn("401", res["error"])
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)

    def test_verify_false_skips_api_call(self):
        """verify=False 不该真调模型（自动化里用，省调用）。"""
        called = []
        self.chat.stop()
        self.chat = mock.patch.object(
            llm, "chat_ex", lambda *a, **k: called.append(1) or ("", {})
        )
        self.chat.start()
        res = switch_mod.switch("zhipu", key="k", verify=False)
        self.assertTrue(res["ok"])
        self.assertFalse(res["verified"])
        self.assertEqual(called, [])


class TestRollbackRestoresProcessEnv(unittest.TestCase):
    """文件回滚了、内存没回滚 = 面板一直用坏 key 直到重启。真踩过这种坑。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = Path(self.tmp) / ".env"
        self.env.write_text("LLM_MODEL=before-model\n", encoding="utf-8")
        self._orig_env_file = switch_mod.ENV_FILE
        switch_mod.ENV_FILE = self.env
        # 先摘干净：别的用例可能已经把真 .env 灌进 os.environ，
        # 留着会让「原本没有的字段要删掉」这条断言失去意义。
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY")
        }
        os.environ["LLM_MODEL"] = "before-model"
        # 别让 current_config() 顺手把真实 .env 又灌回来：
        # 那样 old_env 里就有真值，回滚当然会「恢复」出 LLM_BASE_URL。
        self._saved_loaded = llm._DOTENV_LOADED
        llm._DOTENV_LOADED = True

    def tearDown(self):
        switch_mod.ENV_FILE = self._orig_env_file
        llm._DOTENV_LOADED = self._saved_loaded
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_restored_after_failure(self):
        with mock.patch.object(llm, "chat_ex", side_effect=RuntimeError("boom")):
            res = switch_mod.switch("zhipu", key="k")   # 用真的 apply_config
        self.assertFalse(res["ok"])
        self.assertEqual(os.environ.get("LLM_MODEL"), "before-model")
        # 原来没有的字段要删掉，不能留着新写进去的 base_url
        self.assertIsNone(os.environ.get("LLM_BASE_URL"))


if __name__ == "__main__":
    unittest.main()
