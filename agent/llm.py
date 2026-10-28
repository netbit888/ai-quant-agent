"""LLM 封装：DeepSeek 开放平台（OpenAI 兼容接口）

用法：
    python -m agent.llm "用三句话描述双均线交叉策略"

配置（按优先级）：
    1. 项目根目录 .env 文件（推荐，参考 .env.example）
    2. 系统环境变量

    LLM_API_KEY       通用 key（换供应商时填这个，优先级高于 DEEPSEEK_API_KEY）
    DEEPSEEK_API_KEY  老名字，仍兼容
    LLM_MODEL         可选，默认 deepseek-flash
    LLM_BASE_URL      可选，默认 https://api.deepseek.com
    LLM_THINKING      可选，默认关闭模型的思考过程（见 `_thinking_off()`）。
                      写 1/true/on 可恢复；只对 DeepSeek 接口生效。

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
# 官方 /models 只列 deepseek-flash 和 deepseek-v4-pro（2026-10-29 实测）。
# 老的 deepseek-chat 仍能通，但已不在模型表里，属于**未承诺的兼容**——
# 哪天摘掉，请求全部失败 → 每轮都走「本轮不做」，面板看着在跑、实际一笔不开。
DEFAULT_MODEL = "deepseek-flash"


_DOTENV_LOADED = False

# 已退役的模型名 → 现役名。
#
# `deepseek-chat` 在 2026-10 已从官方 /models 里消失（只剩 deepseek-flash /
# deepseek-v4-pro），但请求仍能通——属于**未承诺的兼容**。老用户的
# `config/.env` 里写的还是它（安装包卸载/升级时保留 config\，不会自动更新），
# 上游哪天摘掉别名，请求就全部失败 → 每轮都判「本轮不做」，面板看着在跑、
# 实际一笔不开，且不报错。
#
# 所以在**进程内**改名，不擅改用户的配置文件（那是他的东西，改不改由他决定）。
# 只在 `_load_dotenv` 里映射：面板/命令行主动切模型走 `apply_config`，是显式
# 选择，不在映射范围内。
_RETIRED_MODELS = {"deepseek-chat": "deepseek-flash"}


def _rename_retired_model() -> None:
    cur = (os.getenv("LLM_MODEL") or "").strip()
    new = _RETIRED_MODELS.get(cur)
    if new:
        os.environ["LLM_MODEL"] = new


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
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    _rename_retired_model()


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


# 思考模式开关。DeepSeek 的混合推理模型默认会先想再答，这里默认关掉。
#
# 依据：2026-10-29 用 60 个真实决策点做过 A/B（同 prompt、只切这个开关）——
#   方向命中   思考开 16/24=67%   思考关 16/23=70%   （两臂都动手的 17 点方向 100% 一致）
#   Brier      0.2380            0.2286              （配对 t=1.20, p≈0.23）
#   sl3.0/tp3.25 回放  +21.75      +12.25            （置换检验 p=0.449，去掉 5 个点翻号）
#   首字延迟   6.11s（最长 12.8s）  0.95s
#   输出       923 token         64 token
# 三项精度指标全部不显著，延迟差异却是确定性的。超短线决策落在每根 M1 的第
# 28~52 秒，多等 6 秒等于把后半根也赌进去；出场端 assist 在弹窗期间价格还会
# 漂移（实盘 slip -0.14，作废线 0.80）。所以默认关——理由不是省钱，是别在
# 没有收益的地方引入延迟。
#
# 想改回开：.env 里写 LLM_THINKING=1，不用动代码。
_THINKING_OFF = {"thinking": {"type": "disabled"}}
_THINKING_ON_WORDS = ("1", "true", "on", "yes", "enabled")

# 服务端一旦拒过这个字段，本进程内不再传（见 `_rejects_thinking`）。
_THINKING_UNSUPPORTED = False


def _thinking_off() -> bool:
    """本次请求是否显式关思考。

    `extra_body` 是**透传给服务端**的字段，只有 DeepSeek 认（上轮 A/B 实测
    可用）。换到智谱/硅基流动还硬塞，会得到 400 未知参数——所以按 base_url
    判断，别家一律不加。
    """
    _load_dotenv()
    if _THINKING_UNSUPPORTED:
        return False
    if (os.getenv("LLM_THINKING") or "").strip().lower() in _THINKING_ON_WORDS:
        return False
    base = (os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL).lower()
    return "deepseek" in base


def _rejects_thinking(exc: Exception) -> bool:
    """这次失败是不是"服务端不认 extra_body.thinking"。

    为什么要留这一手：模型表/参数是上游说改就改的，而这套程序一旦请求全失败
    就会走「本轮不做」分支——面板看着在跑、实际一笔不开、不报错（`deepseek-chat`
    退役那次就是这个形状）。加个字段换来静默停摆，不值。

    只认明确措辞：必须同时出现 `thinking` 和一个"不认识"类词。别把 429、
    超时、余额不足也当参数问题重试——那会把一次真实故障变成两次请求，还顺手
    把思考永久关掉。
    """
    msg = str(exc).lower()
    if "thinking" not in msg:
        return False
    return any(
        w in msg
        for w in ("unknown", "unrecognized", "unsupported", "not support", "extra")
    )


def _disable_thinking_for_process() -> None:
    global _THINKING_UNSUPPORTED
    _THINKING_UNSUPPORTED = True


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
    thinking_off = _thinking_off()
    if thinking_off:
        # 关闭"先想再答"。默认路径见 `_thinking_off()` 上方那段 A/B 记录。
        kwargs["extra_body"] = _THINKING_OFF
    try:
        resp = _client().chat.completions.create(**kwargs)
    except Exception as exc:
        # 上游不认这个字段 → 去掉重试一次，并本进程内不再传。
        if not thinking_off or not _rejects_thinking(exc):
            raise
        _disable_thinking_for_process()
        kwargs.pop("extra_body", None)
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
