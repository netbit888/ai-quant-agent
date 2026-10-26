"""换模型供应商：写 .env + 立刻验证连通，**进程内即时生效，不用重启**。

两个入口，逻辑同一套：
    命令行  python -m tools.switch_llm zhipu --key 你的key
    面板    卡头「模型」那一行，选供应商 → 填 key（可留空）→ 应用

为什么要单独一个脚本，而不是让人手动改 .env：
手动改的三个坑都是真实踩过的——
  1. 三行只取消注释两行（base_url 换了、model 没换）；
  2. key 复制时带上空格或换行 → 401 invalid key，报错里只说 key 无效；
  3. 改完忘了重启面板，以为没生效。
这个脚本把三件事一次做完：写配置、真发一次调用、把结果直接打出来。

用法：
    python -m tools.switch_llm zhipu --key 你的key
    python -m tools.switch_llm zhipu --model glm-4-air     # 只换模型，沿用原 key
    python -m tools.switch_llm deepseek --key sk-xxx
    python -m tools.switch_llm --show                      # 只看当前用的是谁

验证会真花一次调用（十几个 token），失败自动回滚且不计费。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from live import appdirs

ROOT = appdirs.app_root()
# 唯一配置出口。源码态 = 仓库根 .env；绿化版 = exe 同级 config/.env。
# **不能写 _MEIPASS**：那里进程退出就删，用户填的 key 全丢且不报错。
ENV_FILE = appdirs.env_file()

# 都是 OpenAI 兼容接口，所以换供应商只需要这三个值
# models 只是**常用示例**，不是白名单——免费档的模型名换得很勤，
# 面板上允许直接手输任何模型名，以各平台控制台为准。
PROVIDERS: dict[str, dict] = {
    "zhipu": {
        "name": "智谱 GLM（免费档，国内直连）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus", "glm-4-long"],
        "note": "免费 Flash 的模型名会更新（glm-4.7-flash / glm-5.x-flash 等），"
                "以 open.bigmodel.cn 控制台为准，可用 --model 覆盖",
    },
    "deepseek": {
        "name": "DeepSeek（按量付费，判断质量更好）",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "note": "reasoner 不支持 JSON 输出，超短线会自动退回文本解析，慢一倍左右",
    },
    "siliconflow": {
        "name": "硅基流动（部分开源模型永久免费）",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen3-8B",
        "models": ["Qwen/Qwen3-8B", "Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "note": "免费模型有 RPM 限制，30 秒一次够用",
    },
}


def provider_of(base_url: str | None) -> str:
    """从 base_url 反推供应商 id，认不出来就返回空串（面板显示「自定义」）。"""
    if not base_url:
        return ""
    target = base_url.rstrip("/").lower()
    for pid, spec in PROVIDERS.items():
        if spec["base_url"].rstrip("/").lower() == target:
            return pid
    return ""



def upsert_env(text: str, values: dict[str, str]) -> str:
    """把 key=value 写进 .env 文本：已存在就替换，不存在就追加。

    同名但被注释掉的行（`# LLM_MODEL=...`）不碰——那是给人看的备选示例，
    真生效的只有不带 # 的那一行。
    """
    lines = text.splitlines()
    for k, v in values.items():
        done = False
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            if s.split("=", 1)[0].strip() == k:
                lines[i] = f"{k}={v}"
                done = True
                break
        if not done:
            lines.append(f"{k}={v}")
    return "\n".join(lines).rstrip("\n") + "\n"


def switch(
    provider: str,
    key: str | None = None,
    model: str | None = None,
    verify: bool = True,
) -> dict:
    """写 .env 并验证。失败自动回滚到原文件**和原进程环境**。

    key 传 None / 空串 = 沿用当前 key（只换模型时不逼人重填一遍 key）。
    verify=False 只写不验证，给自动化用；面板一律走 True。
    """
    spec = PROVIDERS[provider]
    model = (model or "").strip() or spec["model"]
    key = (key or "").strip()

    # 两个快照：文件的、进程环境的。回滚要同时回，否则「文件回去了、
    # 内存还是坏的」，面板会一直用坏配置调到进程重启为止。
    old_text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    backup = None
    if ENV_FILE.exists():
        # .env 是隐藏文件（无后缀），with_suffix 会抛异常，必须用 with_name
        backup = ENV_FILE.with_name(".env.bak")
        shutil.copy2(ENV_FILE, backup)

    from agent.llm import current_config

    before = current_config()
    old_env = {k: os.environ.get(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")}

    values = {"LLM_BASE_URL": spec["base_url"], "LLM_MODEL": model}
    if key:
        values["LLM_API_KEY"] = key
    new = upsert_env(old_text, values)
    # 首启时 config/ 还不存在（绿化版第一次配置就落在这里），不先建目录会
    # FileNotFoundError: config.env 的父目录。源码态 config/ 也不存在，同样需要。
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(new, encoding="utf-8")

    # 让当前进程立刻用上新值（_load_dotenv 用 setdefault，必须显式覆盖）
    from agent.llm import apply_config

    apply_config(model=model, base_url=spec["base_url"], api_key=key or None)

    def _rollback() -> None:
        """文件回滚 + 进程环境回滚。"""
        if backup is not None:
            shutil.copy2(backup, ENV_FILE)
            backup.unlink(missing_ok=True)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    base = {"model": model, "base_url": spec["base_url"],
            "provider": provider, "key_changed": bool(key),
            "previous": before}

    if not verify:
        return {"ok": True, **base, "verified": False,
                "backup": str(backup) if backup else None}

    from agent.llm import chat_ex

    started = time.time()
    try:
        text, usage = chat_ex("只回两个字：可用")
    except Exception as exc:  # noqa: BLE001
        _rollback()
        return {"ok": False, **base, "error": f"{type(exc).__name__}: {exc}",
                "rollback": backup is not None or bool(old_env)}

    elapsed = time.time() - started
    return {"ok": True, **base, "verified": True,
            "reply": (text or "").strip()[:40], "elapsed": elapsed,
            "usage": usage, "backup": str(backup) if backup else None}


def main() -> None:
    parser = argparse.ArgumentParser(description="换模型供应商并验证连通")
    parser.add_argument("provider", nargs="?", choices=sorted(PROVIDERS), help="供应商")
    parser.add_argument("--key", help="API key")
    parser.add_argument("--model", help="覆盖默认模型名")
    parser.add_argument("--show", action="store_true", help="只看当前配置，不改")
    args = parser.parse_args()

    if args.show:
        from agent.llm import current_config

        cfg = current_config()
        pid = provider_of(cfg["base_url"])
        print(f"供应商   = {PROVIDERS[pid]['name'] if pid else '自定义 / 未知'}")
        print(f"base_url = {cfg['base_url']}")
        print(f"model    = {cfg['model']}")
        tail = f"（尾号 {cfg['key_tail']}，来自 {cfg['key_source']}）" if cfg["key_set"] else ""
        print(f"key      = {'已配置' if cfg['key_set'] else '缺失'}{tail}")
        return

    if not args.provider:
        parser.error("要指定供应商；只看配置用 --show")

    spec = PROVIDERS[args.provider]
    print(f"→ {spec['name']}")
    print(f"  base_url = {spec['base_url']}")
    print(f"  model    = {args.model or spec['model']}")
    if spec["note"]:
        print(f"  注意：{spec['note']}")

    res = switch(args.provider, args.key, args.model)
    if not res["ok"]:
        print(f"\n✗ 验证失败：{res['error']}")
        if res.get("rollback"):
            print("  已回滚到原来的配置（文件和进程环境都回去了），你没被留在坏配置上。")
        else:
            print("  原 .env 不存在，无法回滚；请自行修正 .env。")
        sys.exit(1)

    print(f"\n✓ 调用成功（{res['elapsed']:.2f}s）：{res['reply']}")
    print(f"  usage = {res['usage']}")
    if res.get("backup"):
        print(f"  旧配置已备份：{Path(res['backup']).name}")
    if os.environ.get("_WB_IN_DASHBOARD") != "1":
        print("  下一步：重启面板（面板上也能直接切，不用重启）")


if __name__ == "__main__":
    main()
