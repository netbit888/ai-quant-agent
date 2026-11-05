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
from tools.switch_llm import (PROVIDERS, provider_of, upsert_env,
                              variant_index_of, variants_of)


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

    def test_newline_in_value_is_stripped(self):
        """.env 是行格式文件，值里的 \\n 会变成下一行 = 注入任意变量。
        审查发现的真实攻击面：model 塞 "x\\nLLM_BASE_URL=https://evil"
        就能把后续 LLM 请求（带 API key）导向攻击者服务器。"""
        out = upsert_env("", {"LLM_MODEL": "x\nLLM_BASE_URL=https://evil.cn"})
        self.assertEqual(out.count("\n"), 1)          # 只有行尾一个换行
        # 注入片段被并进 model 的值里（模型名变非法、调用会失败），
        # 关键是不再构成独立的 LLM_BASE_URL 行去改写请求目的地
        for line in out.splitlines():
            self.assertTrue(line.startswith("LLM_MODEL="), line)


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

    def test_deepseek_is_first_and_lists_flash_only(self):
        """供应商只有三家（DeepSeek 在最前），模型清单只列 flash。

        v4-pro 从清单里摘掉是用户明确要求；手输仍然可以（CLI --model）。
        """
        self.assertEqual(list(PROVIDERS)[:1], ["deepseek"])
        self.assertEqual(PROVIDERS["deepseek"]["models"], ["deepseek-flash"])

    def test_deepseek_offers_exactly_two_model_variants(self):
        """「深度思考」是**模型下拉的一项**，不是第二个供应商。

        两个选项的模型名相同（deepseek-flash），差别只在 thinking——
        模型名本身分不出模式，所以必须靠 variants 表达。
        """
        vs = variants_of("deepseek")
        self.assertEqual(len(vs), 2)
        self.assertEqual([v["model"] for v in vs], ["deepseek-flash"] * 2)
        self.assertEqual([bool(v["thinking"]) for v in vs], [False, True])
        self.assertTrue(all(v["label"] for v in vs), "每项都要有可读的标签")

    def test_variant_labels_stay_terse(self):
        """模型下拉只留「deepseek-flash」和「（深度思考）」两串。

        2026-11-05 用户明确：不要「不思考」、不要「（默认）」、不要首字秒数——
        选项一长，下拉里就被截断，反而看不清选的是哪一档。默认项就是模型名本身。
        """
        self.assertEqual([v["label"] for v in variants_of("deepseek")],
                         ["deepseek-flash", "deepseek-flash（深度思考）"])

    def test_provider_names_carry_no_parenthetical(self):
        """供应商下拉只留名字，不挂「（免费档）」「（部分开源模型永久免费）」这类注解。

        2026-11-05 用户要求删掉：注解一长就被下拉宽度截断，而「免费/收费」这类
        说明本来就写在 note 里，不该占下拉的位置。
        """
        for pid, spec in PROVIDERS.items():
            self.assertNotIn("（", spec["name"], pid)
            self.assertNotIn("(", spec["name"], pid)

    def test_default_variant_is_the_non_thinking_one(self):
        """默认必须是「不思考」：60 个决策点的 A/B 没量出增益，延迟是确定的。"""
        self.assertFalse(variants_of("deepseek")[0]["thinking"])

    def test_other_providers_never_ask_for_thinking(self):
        """智谱/硅基流动不认 extra_body.thinking，给它们挂 True 就是自找 400。"""
        for pid, spec in PROVIDERS.items():
            if "deepseek" in spec["base_url"]:
                continue
            self.assertTrue(all(not v["thinking"] for v in variants_of(pid)), pid)


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


class TestVariantIndex(unittest.TestCase):
    """回填「模型」下拉靠下标，不靠模型名。

    少这一层，两项模型名相同，面板会永远落在第一项——显示「不思考」而实际
    在深度思考里跑。
    """

    def test_indexes_both_deepseek_variants(self):
        self.assertEqual(variant_index_of("deepseek", "deepseek-flash", False), 0)
        self.assertEqual(variant_index_of("deepseek", "deepseek-flash", True), 1)

    def test_unknown_model_returns_minus_one(self):
        """手输的模型名不在清单里：返回 -1，让前端自己决定落哪一项。"""
        self.assertEqual(variant_index_of("deepseek", "deepseek-v4-pro", False), -1)

    def test_provider_without_variants_falls_back_to_models(self):
        vs = variants_of("zhipu")
        self.assertIn("glm-4-flash", [v["model"] for v in vs])
        self.assertTrue(all(not v["thinking"] for v in vs))


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

    def test_model_with_newline_is_rejected_and_env_untouched(self):
        """2026-11-05 审查 P1：model 含换行 = .env 注入，必须拒绝且文件原样。"""
        before = self.env.read_text(encoding="utf-8")
        res = switch_mod.switch("zhipu", key=None,
                                model="glm-4-flash\nLLM_BASE_URL=https://evil.cn")
        self.assertFalse(res["ok"])
        self.assertIn("换行", res["error"])
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)

    def test_key_with_newline_is_rejected(self):
        res = switch_mod.switch("zhipu", key="sk-x\nDEEPSEEK_API_KEY=", model="glm-4-flash")
        self.assertFalse(res["ok"])

    def test_key_given_is_written(self):
        res = switch_mod.switch("zhipu", key="new-key", model="glm-4-flash")
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("LLM_API_KEY=new-key", text)
        self.assertTrue(res["key_changed"])

    def test_blank_model_falls_back_to_provider_default(self):
        """模型框留空 = 用该家默认模型，别把空字符串写进 .env。"""
        switch_mod.switch("deepseek", key="k", model="   ")
        self.assertIn("LLM_MODEL=deepseek-flash",
                      self.env.read_text(encoding="utf-8"))

    def test_deepseek_default_is_not_the_legacy_alias(self):
        """默认模型必须是官方 /models 里有的名字。

        deepseek-chat 已从模型表里消失（仍能通，但属于未承诺的兼容）。把它留作
        默认，一旦上游摘掉别名，请求全失败 → 每轮都判「本轮不做」，面板看着在
        跑、实际一笔不开，且不报错。这条断言就是防这个静默停摆。
        """
        spec = switch_mod.PROVIDERS["deepseek"]
        self.assertEqual(spec["model"], "deepseek-flash")
        self.assertNotIn("deepseek-chat", spec["models"])

    def test_thinking_entry_writes_flag_on(self):
        """选「深度思考」= 往 .env 写 LLM_THINKING=1，模型名一个字都不变。"""
        res = switch_mod.switch("deepseek", key=None, thinking=True)
        text = self.env.read_text(encoding="utf-8")
        self.assertTrue(res["ok"])
        self.assertIn("LLM_THINKING=1", text)
        self.assertIn("LLM_MODEL=deepseek-flash", text)
        self.assertTrue(res["thinking"])

    def test_plain_entry_turns_flag_back_off(self):
        """从深度思考切回不思考，必须把 1 改回 0。

        只靠「不写这一行」是不够的：文件里留着 1，下次启动又会是思考模式。
        """
        self.env.write_text("LLM_THINKING=1\n", encoding="utf-8")
        res = switch_mod.switch("deepseek", key=None)
        self.assertIn("LLM_THINKING=0", self.env.read_text(encoding="utf-8"))
        self.assertFalse(res["thinking"])

    def test_non_deepseek_never_writes_thinking_on(self):
        """智谱/硅基流动不认这个字段，thinking 传 True 也得落成 0。"""
        switch_mod.switch("siliconflow", key=None, thinking=True)
        self.assertIn("LLM_THINKING=0", self.env.read_text(encoding="utf-8"))

    def test_thinking_that_did_not_take_effect_is_reported(self):
        """深度思考没生效要报出来，不能只看 HTTP 200。

        服务端拒过 thinking 字段时 chat_ex 会去掉重试，请求照样成功；
        那时候的 200 只证明「模型答了」，不证明「它想了」。
        """
        self.chat.stop()
        self.chat = mock.patch.object(
            llm, "chat_ex", lambda *a, **k: ("可用", {"completion_tokens": 3}))
        self.chat.start()
        res = switch_mod.switch("deepseek", key=None, thinking=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["thinking_effective"])
        self.assertIn("reasoning_tokens", res["warning"])

    def test_thinking_that_took_effect_has_no_warning(self):
        self.chat.stop()
        self.chat = mock.patch.object(
            llm, "chat_ex",
            lambda *a, **k: ("可用", {"completion_tokens": 120, "reasoning_tokens": 98}))
        self.chat.start()
        res = switch_mod.switch("deepseek", key=None, thinking=True)
        self.assertTrue(res["thinking_effective"])
        self.assertNotIn("warning", res)

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
            for k in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_THINKING")
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

    def test_thinking_env_restored_after_failure(self):
        """思考开关也要一起回滚。

        漏掉它，失败的切换会留下「进程里开着思考、文件里是关的」——
        下一次重启表现突变，而且面板显示的是文件里的值，排查时会被带偏。
        """
        with mock.patch.object(llm, "chat_ex", side_effect=RuntimeError("boom")):
            res = switch_mod.switch("deepseek", key="k", thinking=True)
        self.assertFalse(res["ok"])
        self.assertIsNone(os.environ.get("LLM_THINKING"))


if __name__ == "__main__":
    unittest.main()
