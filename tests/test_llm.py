"""`agent/llm.py` 的配置解析测试（不联网、不读真实 .env）。

重点是一条曾经埋了很久的顺序 bug：
    `chat_ex()` 里 `used_model = os.getenv("LLM_MODEL", DEFAULT_MODEL)` 写在
    `_client()` 之前，而 `.env` 是在 `_client()` 内部才加载的 —— 于是进程启动后
    **第一次**调用永远读不到 `.env` 里的模型名，退回 `DEFAULT_MODEL`。
    换到智谱时表现为 `400 1211 模型不存在`（base_url 换了、模型名没换）。
    用 DeepSeek 时因为默认模型刚好对得上，这个 bug 完全看不出来。
"""

from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from agent import llm


def _fake_client(sink: dict):
    """返回一个假 OpenAI 客户端，把 create() 的入参记到 sink 里。"""

    class _Completions:
        def create(self, **kwargs):
            sink.update(kwargs)
            msg = types.SimpleNamespace(content='{"act": true}')
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg)], usage=None
            )

    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_Completions())
    )


class TestModelResolution(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "DEEPSEEK_API_KEY")
        }
        self._saved_loaded = llm._DOTENV_LOADED
        llm._DOTENV_LOADED = False

    def tearDown(self):
        llm._DOTENV_LOADED = self._saved_loaded
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_file_model_is_used_on_first_call(self):
        """首次调用就必须用上 .env 里的 LLM_MODEL，不能退回默认值。"""
        sink: dict = {}

        def fake_load():                      # 模拟「.env 里有 LLM_MODEL」
            os.environ.setdefault("LLM_MODEL", "glm-4-flash")

        with mock.patch.object(llm, "_load_dotenv", side_effect=fake_load), \
                mock.patch.object(llm, "_client", lambda: _fake_client(sink)):
            llm.chat_ex("hi")

        self.assertEqual(sink.get("model"), "glm-4-flash")

    def test_explicit_model_argument_wins(self):
        """显式传 model 时不看 .env。"""
        sink: dict = {}
        with mock.patch.object(llm, "_load_dotenv", lambda: None), \
                mock.patch.object(llm, "_client", lambda: _fake_client(sink)):
            llm.chat_ex("hi", model="custom-model")
        self.assertEqual(sink.get("model"), "custom-model")

    def test_falls_back_to_default_when_unset(self):
        sink: dict = {}
        with mock.patch.object(llm, "_load_dotenv", lambda: None), \
                mock.patch.object(llm, "_client", lambda: _fake_client(sink)):
            llm.chat_ex("hi")
        self.assertEqual(sink.get("model"), llm.DEFAULT_MODEL)

    def test_json_mode_passes_response_format(self):
        sink: dict = {}
        with mock.patch.object(llm, "_load_dotenv", lambda: None), \
                mock.patch.object(llm, "_client", lambda: _fake_client(sink)):
            llm.chat_ex("hi", json_mode=True)
        self.assertEqual(sink.get("response_format"), {"type": "json_object"})

    def test_json_mode_off_by_default(self):
        sink: dict = {}
        with mock.patch.object(llm, "_load_dotenv", lambda: None), \
                mock.patch.object(llm, "_client", lambda: _fake_client(sink)):
            llm.chat_ex("hi")
        self.assertNotIn("response_format", sink)


class TestEnvLoader(unittest.TestCase):
    def test_does_not_override_existing_env(self):
        """已存在的环境变量优先——方便临时在命令行覆盖，不被 .env 盖掉。"""
        saved = os.environ.get("LLM_MODEL")
        os.environ["LLM_MODEL"] = "from-shell"
        try:
            llm._DOTENV_LOADED = False
            llm._load_dotenv()
            self.assertEqual(os.environ["LLM_MODEL"], "from-shell")
        finally:
            if saved is None:
                os.environ.pop("LLM_MODEL", None)
            else:
                os.environ["LLM_MODEL"] = saved
            llm._DOTENV_LOADED = False

    def test_retired_model_name_is_renamed_in_process(self):
        """老的 deepseek-chat 必须被改成现役名。

        安装包升级时会保留 config\\ 目录，老用户的 .env 里仍是 deepseek-chat。
        它已经不在官方 /models 里（剩 deepseek-flash / deepseek-v4-pro），哪天
        别名被摘掉，请求全失败 → 每轮判「本轮不做」，面板看着在跑、实际一笔不
        开，且不报错。这条断言就是防这种静默停摆。
        """
        saved = os.environ.get("LLM_MODEL")
        os.environ["LLM_MODEL"] = "deepseek-chat"
        try:
            llm._DOTENV_LOADED = False
            llm._load_dotenv()
            self.assertEqual(os.environ["LLM_MODEL"], "deepseek-flash")
        finally:
            if saved is None:
                os.environ.pop("LLM_MODEL", None)
            else:
                os.environ["LLM_MODEL"] = saved
            llm._DOTENV_LOADED = False

    def test_current_model_name_is_left_alone(self):
        """现役名不能被瞎改——映射表只对退役名生效。"""
        for name in ("deepseek-flash", "deepseek-v4-pro", "glm-4-flash"):
            saved = os.environ.get("LLM_MODEL")
            os.environ["LLM_MODEL"] = name
            try:
                llm._DOTENV_LOADED = False
                llm._load_dotenv()
                self.assertEqual(os.environ["LLM_MODEL"], name)
            finally:
                if saved is None:
                    os.environ.pop("LLM_MODEL", None)
                else:
                    os.environ["LLM_MODEL"] = saved
                llm._DOTENV_LOADED = False

    def test_loaded_once_only(self):
        """重复调用不重复读文件（幂等），否则每次请求都多一次磁盘 IO。"""
        llm._DOTENV_LOADED = False
        calls = []
        real_read = llm.Path.read_text

        def counting_read(self, *a, **kw):
            calls.append(1)
            return real_read(self, *a, **kw)

        try:
            with mock.patch.object(llm.Path, "read_text", counting_read):
                llm._load_dotenv()
                llm._load_dotenv()
                llm._load_dotenv()
        finally:
            llm._DOTENV_LOADED = False
        self.assertLessEqual(len(calls), 1)


def _client_that(calls: list, errors: list):
    """按 errors 逐次抛异常的假客户端；errors[i]=None 表示第 i 次正常返回。"""

    class _Completions:
        def create(self, **kwargs):
            i = len(calls)
            calls.append(dict(kwargs))
            err = errors[i] if i < len(errors) else None
            if err is not None:
                raise err
            msg = types.SimpleNamespace(content='{"act": false}')
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg)], usage=None
            )

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))


class TestThinkingSwitch(unittest.TestCase):
    """思考模式默认关闭（A/B 结论见 `agent/llm.py` 里 `_thinking_off` 上方注释）。

    这里锁三件事：默认关、能一键开回来、换别家供应商不塞这个字段。
    """

    def setUp(self):
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("LLM_BASE_URL", "LLM_THINKING")
        }
        self._saved_broken = llm._THINKING_UNSUPPORTED
        llm._THINKING_UNSUPPORTED = False

    def tearDown(self):
        llm._THINKING_UNSUPPORTED = self._saved_broken
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self) -> dict:
        sink: dict = {}
        with mock.patch.object(llm, "_load_dotenv", lambda: None), \
                mock.patch.object(llm, "_client", lambda: _fake_client(sink)):
            llm.chat_ex("hi")
        return sink

    def test_deepseek_default_is_off(self):
        """默认就该关——不写任何配置也是关的，否则新装的用户等于没生效。"""
        os.environ["LLM_BASE_URL"] = "https://api.deepseek.com"
        sink = self._run()
        self.assertEqual(sink.get("extra_body"), {"thinking": {"type": "disabled"}})

    def test_can_turn_back_on(self):
        """LLM_THINKING=1 时不传这个字段（交由服务端默认=开）。"""
        os.environ["LLM_BASE_URL"] = "https://api.deepseek.com"
        for word in ("1", "true", "ON"):
            os.environ["LLM_THINKING"] = word
            self.assertNotIn("extra_body", self._run(), msg=word)

    def test_other_provider_never_gets_the_field(self):
        """智谱/硅基流动不认 extra_body.thinking，塞进去就是 400。"""
        for base in (
            "https://open.bigmodel.cn/api/paas/v4",
            "https://api.siliconflow.cn/v1",
        ):
            os.environ["LLM_BASE_URL"] = base
            os.environ.pop("LLM_THINKING", None)
            self.assertNotIn("extra_body", self._run(), msg=base)


class TestThinkingFallback(unittest.TestCase):
    """上游不认这个字段时的降级。

    防的是"静默停摆"：请求全失败 → 每轮判「本轮不做」→ 面板看着在跑、实际
    一笔不开、不报错。`deepseek-chat` 退役那次就是这个形状。
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("LLM_BASE_URL", "LLM_THINKING")}
        os.environ["LLM_BASE_URL"] = "https://api.deepseek.com"
        os.environ.pop("LLM_THINKING", None)
        self._saved_broken = llm._THINKING_UNSUPPORTED
        llm._THINKING_UNSUPPORTED = False

    def tearDown(self):
        llm._THINKING_UNSUPPORTED = self._saved_broken
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_drops_field_and_retries_once(self):
        """服务端报"不认 thinking"→ 去掉字段重试，第二次必须成功。"""
        calls: list = []
        err = RuntimeError("400 unknown field: thinking is not supported")
        with mock.patch.object(llm, "_load_dotenv", lambda: None), \
                mock.patch.object(llm, "_client", lambda: _client_that(calls, [err])):
            text, _ = llm.chat_ex("hi")

        self.assertIn("extra_body", calls[0])
        self.assertNotIn("extra_body", calls[1])
        self.assertIn("act", text)
        self.assertTrue(llm._THINKING_UNSUPPORTED, "拒过一次就该在本进程内停用")

    def test_disable_sticks_for_later_calls(self):
        """停用后同一进程的后续请求不再白撞一次。"""
        llm._THINKING_UNSUPPORTED = True
        calls: list = []
        with mock.patch.object(llm, "_load_dotenv", lambda: None), \
                mock.patch.object(llm, "_client", lambda: _client_that(calls, [])):
            llm.chat_ex("hi")
        self.assertNotIn("extra_body", calls[0])

    def test_unrelated_errors_are_not_swallowed(self):
        """429 / 超时这类不是参数问题，不许重试、不许顺手把思考关掉。"""
        calls: list = []
        err = RuntimeError("429 rate limit exceeded, please retry later")
        with mock.patch.object(llm, "_load_dotenv", lambda: None), \
                mock.patch.object(llm, "_client", lambda: _client_that(calls, [err])):
            with self.assertRaises(RuntimeError):
                llm.chat_ex("hi")

        self.assertEqual(len(calls), 1)
        self.assertFalse(llm._THINKING_UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
