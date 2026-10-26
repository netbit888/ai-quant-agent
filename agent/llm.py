"""LLM 封装：DeepSeek 开放平台（OpenAI 兼容接口）

用法：
    python -m agent.llm "用三句话描述双均线交叉策略"

配置（按优先级）：
    1. 项目根目录 .env 文件（推荐，参考 .env.example）
    2. 系统环境变量

    LLM_API_KEY       通用 key（换供应商时填这个，优先级高于 DEEPSEEK_API_KEY）
    DEEPSEEK_API_KEY  老名字，仍兼容
    LLM_MODEL         可选，默认 deepseek-chat
    LLM_BASE_URL      可选，默认 https://api.deepseek.com

换供应商只改这三行，代码不用动（都是 OpenAI 兼容接口）。示例：
    智谱 GLM-4-Flash（免费档）
        LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
        LLM_MODEL=glm-4-flash
    硅基流动
        LLM_BASE_URL=https://api.siliconflow.cn/v1
        LLM_MODEL=Qwen/Qwen3-8B
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from openai import OpenAI

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


_DOTENV_LOADED = False


def _load_dotenv() -> None:
    """极简 .env 加载器，避免引入额外依赖。不覆盖已存在的环境变量。

    只读一次（幂等），且**必须先于任何 os.getenv 调用**——否则进程启动后
    第一次读到的 LLM_MODEL 是 None、退回 DEFAULT_MODEL，表现成「base_url
    换了、模型名没换」的 400（智谱会回 1211 模型不存在）。
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    # 路径统一走 appdirs：源码态 = 仓库根 .env；绿化版 = exe 同级 config/.env。
    # 这是唯一出口，读写两侧都从这里取，避免"向导写 A 文件、这里读 B 文件"。
    from live import appdirs

    env_file = appdirs.env_file()
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str) -> str:
    """读配置项：先从 .env 补环境变量，再取值。"""
    _load_dotenv()
    return os.getenv(name) or default


def _active_key() -> tuple[str, str]:
    """返回 (key, 来源变量名)。来源名用于面板显示「这把 key 是从哪读的」。"""
    if os.getenv("LLM_API_KEY"):
        return os.environ["LLM_API_KEY"], "LLM_API_KEY"
    if os.getenv("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"], "DEEPSEEK_API_KEY"
    return "", ""


def current_config() -> dict:
    """当前生效的配置，给面板显示用。key 只给尾 4 位，绝不整串返回。"""
    _load_dotenv()
    key, source = _active_key()
    return {
        "base_url": os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL,
        "model": os.getenv("LLM_MODEL") or DEFAULT_MODEL,
        "key_set": bool(key),
        "key_tail": key[-4:] if len(key) >= 4 else "",
        "key_source": source,
    }


def apply_config(
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict:
    """运行时改配置，**下一次调用立刻生效**。

    为什么不能只改 .env：`_load_dotenv()` 用的是 `setdefault`，进程里已经存在的
    环境变量不会被文件里的新值覆盖。所以切模型必须显式写 os.environ，
    否则面板上点了「切换」、界面上显示新模型，实际还在调老模型——
    这比报错更糟，因为看不出问题。
    """
    global _DOTENV_LOADED
    if api_key:
        os.environ["LLM_API_KEY"] = api_key.strip()
    if base_url:
        os.environ["LLM_BASE_URL"] = base_url.strip()
    if model:
        os.environ["LLM_MODEL"] = model.strip()
    _DOTENV_LOADED = True
    return current_config()


def _client() -> OpenAI:
    _load_dotenv()
    # LLM_API_KEY 优先：换供应商时只需加这一行，不用动原来的 DeepSeek key，
    # 想切回去删掉即可。两个都填了却用错 key，会得到 401 而不是答案。
    api_key = _env("LLM_API_KEY", "") or _env("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "缺少 API key。请在项目根目录创建 .env 文件并写入：\n"
            "    LLM_API_KEY=你的key\n"
            "或老写法 DEEPSEEK_API_KEY=sk-xxxxxxxx\n"
            "（模板见 .env.example）"
        )
    return OpenAI(
        api_key=api_key,
        base_url=_env("LLM_BASE_URL", DEFAULT_BASE_URL),
        timeout=60.0,
        max_retries=2,
    )


def chat(prompt: str, model: str | None = None, system: str | None = None) -> str:
    """发送一轮对话，返回纯文本回复。

    注意：deepseek-reasoner 不支持 temperature 等采样参数，因此这里不传。
    """
    return chat_ex(prompt, model=model, system=system)[0]


def chat_ex(
    prompt: str, model: str | None = None, system: str | None = None,
    json_mode: bool = False,
) -> tuple[str, dict]:
    """同 `chat`，但额外返回用量信息，供调用方记账（花了多少 token）。

    `json_mode=True` 时要求服务端强制输出合法 JSON（response_format）。
    超短线决策只输出一个对象，开这个能省下不少输出 token、也省掉解析失败。
    注意 deepseek-reasoner 不支持这个参数，调用方要能接受失败后重试。

    返回 (文本, {"model": ..., "prompt_tokens": ..., "completion_tokens": ...})
    用量字段在服务端未返回时为空。
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    used_model = model or _env("LLM_MODEL", DEFAULT_MODEL)
    kwargs: dict = {"model": used_model, "messages": messages}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _client().chat.completions.create(**kwargs)

    text = resp.choices[0].message.content or ""
    usage: dict = {"model": used_model}
    u = getattr(resp, "usage", None)
    if u is not None:
        extra = getattr(u, "model_extra", None) or {}
        for src, dst in (
            ("prompt_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
            # DeepSeek 的前缀缓存计量。拿不到就是平台没返回，不做假设。
            ("prompt_cache_hit_tokens", "cache_hit_tokens"),
            ("prompt_cache_miss_tokens", "cache_miss_tokens"),
        ):
            val = getattr(u, src, None)
            if val is None:
                val = extra.get(src)
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
