"""LLM 封装：DeepSeek 开放平台（OpenAI 兼容接口）

用法：
    python -m agent.llm "用三句话描述双均线交叉策略"

配置（按优先级）：
    1. 项目根目录 .env 文件（推荐，参考 .env.example）
    2. 系统环境变量

    DEEPSEEK_API_KEY  必填，sk- 开头
    LLM_MODEL         可选，默认 deepseek-chat；推理模型可设 deepseek-reasoner
    LLM_BASE_URL      可选，默认 https://api.deepseek.com
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from openai import OpenAI

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def _load_dotenv() -> None:
    """极简 .env 加载器，避免引入额外依赖。不覆盖已存在的环境变量。"""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _client() -> OpenAI:
    _load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(
            "缺少 API key。请在项目根目录创建 .env 文件并写入：\n"
            "    DEEPSEEK_API_KEY=sk-xxxxxxxx\n"
            "（模板见 .env.example）"
        )
    return OpenAI(
        api_key=api_key,
        base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
        timeout=60.0,
        max_retries=2,
    )


def chat(prompt: str, model: str | None = None, system: str | None = None) -> str:
    """发送一轮对话，返回纯文本回复。

    注意：deepseek-reasoner 不支持 temperature 等采样参数，因此这里不传。
    """
    return chat_ex(prompt, model=model, system=system)[0]


def chat_ex(
    prompt: str, model: str | None = None, system: str | None = None
) -> tuple[str, dict]:
    """同 `chat`，但额外返回用量信息，供调用方记账（花了多少 token）。

    返回 (文本, {"model": ..., "prompt_tokens": ..., "completion_tokens": ...})
    用量字段在服务端未返回时为空。
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    used_model = model or os.getenv("LLM_MODEL", DEFAULT_MODEL)
    resp = _client().chat.completions.create(model=used_model, messages=messages)

    text = resp.choices[0].message.content or ""
    usage: dict = {"model": used_model}
    u = getattr(resp, "usage", None)
    if u is not None:
        for src, dst in (
            ("prompt_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            val = getattr(u, src, None)
            if val is not None:
                usage[dst] = int(val)
    return text, usage


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]).strip() or "用三句话描述双均线交叉策略"
    try:
        print(chat(text))
    except Exception as exc:  # 首次运行把错误原样打出来，方便定位
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
