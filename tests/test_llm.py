"""`agent/llm.py` 的配置解析测试（不联网、不读真实 .env）。

重点是一条曾经埋了很久的顺序 bug：
    `chat_ex()` 里 `used_model = os.getenv("LLM_MODEL", DEFAULT_MODEL)` 写在
    `_client()` 之前，而 `.env` 是在 `_client()` 内部才加载的 —— 于是进程启动后
    **第一次**调用永远读不到 `.env` 里的模型名，退回 `deepseek-chat`。
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


if __name__ == "__main__":
    unittest.main()
