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
# models 只是**常用示例**，不是白名单——免费档的模型名换得很勤。
#
# 面板的下拉是两级的：**供应商**（这里的 key）→ **模型**（`variants`）。
# DeepSeek 的「思考 / 不思考」是**同一个模型名 + 一个请求参数**，模型名本身
# 分不出来，所以它必须体现在「模型」这一级的选项上，而不是把供应商拆成
# 两条——那会让「这一家的 key」这种判断失去落点（2026-11-05 改回来）。
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek 开放平台",
        "label": "DeepSeek",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "models": ["deepseek-flash"],
        "variants": [
            {"model": "deepseek-flash", "thinking": False,
             "label": "deepseek-flash"},
            {"model": "deepseek-flash", "thinking": True,
             "label": "deepseek-flash（深度思考）"},
        ],
        "note": "deepseek-chat 已不在官方模型表里（旧别名，随时可能失效）。"
                "深度思考是同一个 flash 打开 thinking：60 个真实决策点的 A/B 结论"
                "是无可测增益（Brier 0.2380 vs 0.2286），而首字 6.11s vs 0.95s、"
                "输出 token 约 14 倍——想「想清楚再答」可以选，别指望更准",
    },
    "zhipu": {
        "name": "智谱 GLM",
        "label": "智谱 GLM",
        "provider": "zhipu",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus", "glm-4-long"],
        "note": "免费 Flash 的模型名会更新（glm-4.7-flash / glm-5.x-flash 等），"
                "以 open.bigmodel.cn 控制台为准，可用 --model 覆盖",
    },
    "siliconflow": {
        "name": "硅基流动",
        "label": "硅基流动",
        "provider": "siliconflow",
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


def variants_of(pid: str) -> list[dict]:
    """面板「模型」下拉的选项（`{model, thinking, label}`）。

    有 `variants` 就用它；没有就按 `models` 摊平，thinking 一律 False。
    这样别家不用重复维护两份清单，而 DeepSeek 那条能表达「同名模型、两种模式」。
    """
    spec = PROVIDERS.get(pid)
    if not spec:
        return []
    if spec.get("variants"):
        return [dict(v) for v in spec["variants"]]
    return [{"model": m, "thinking": False, "label": m} for m in spec["models"]]


def variant_index_of(pid: str, model: str | None, thinking: bool) -> int:
    """当前生效的是「模型」下拉里的第几项；匹配不到返回 -1。

    面板要靠它把下拉回填到正确那一条——DeepSeek 的两项模型名完全相同，
    只比模型名会永远落在第一项，显示成「不思考」而实际在思考。
    """
    for i, v in enumerate(variants_of(pid)):
        if v["model"] == model and bool(v["thinking"]) == bool(thinking):
            return i
    return -1



def upsert_env(text: str, values: dict[str, str]) -> str:
    """把 key=value 写进 .env 文本：已存在就替换，不存在就追加。

    同名但被注释掉的行（`# LLM_MODEL=...`）不碰——那是给人看的备选示例，
    真生效的只有不带 # 的那一行。

    值里的换行必须滤掉：.env 是行格式文件，值里一个 \\n 就会变成下一行，
    等于往配置里注入任意变量——比如 model 里塞 "x\\nLLM_BASE_URL=https://evil"
    就能把后续所有 LLM 请求（带着 API key）导向攻击者的服务器。
    调用方（switch）已经拒绝含换行的输入，这里是纵深兜底。
    """
    lines = text.splitlines()
    for k, v in values.items():
        v = "".join(ch for ch in v if ch not in "\n\r")
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
    thinking: bool | None = None,
) -> dict:
    """写 .env 并验证。失败自动回滚到原文件**和原进程环境**。

    key 传 None / 空串 = 沿用当前 key（只换模型时不逼人重填一遍 key）。
    verify=False 只写不验证，给自动化用；面板一律走 True。

    `thinking` 是「要不要先想再答」，只有 DeepSeek 接口认这个字段。面板里它是
    「模型」下拉的一项（同一个 flash 的两种模式），所以由调用方显式传；传 None
    按关处理。别家一律写成 0：留个 1 在那儿虽然代码会按 base_url 忽略，但面板
    会显示一个不存在的状态。
    """
    spec = PROVIDERS[provider]
    model = (model or "").strip() or spec["model"]
    key = (key or "").strip()
    if thinking is None:
        thinking = False
    thinking = bool(thinking) and "deepseek" in spec["base_url"].lower()

    # .env 注入防线（2026-11-05 安全审查 P1）：model/key 来自 HTTP body，
    # 含换行就能往 .env 里塞新行注入 LLM_BASE_URL，把后续请求导去恶意服务器
    # 并外传 API key。这里在写文件之前直接拒绝。
    for label, value in (("model", model), ("key", key)):
        if value and any(c in value for c in "\n\r"):
            return {"ok": False, "error": f"{label} 含换行符，已拒绝写入（防止 .env 注入）"}

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
    old_env = {k: os.environ.get(k) for k in
               ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_THINKING")}

    values = {"LLM_BASE_URL": spec["base_url"], "LLM_MODEL": model,
              "LLM_THINKING": "1" if thinking else "0"}
    if key:
        values["LLM_API_KEY"] = key
    new = upsert_env(old_text, values)
    # 首启时 config/ 还不存在（绿化版第一次配置就落在这里），不先建目录会
    # FileNotFoundError: config.env 的父目录。源码态 config/ 也不存在，同样需要。
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(new, encoding="utf-8")

    # 让当前进程立刻用上新值（_load_dotenv 用 setdefault，必须显式覆盖）
    from agent.llm import apply_config

    apply_config(model=model, base_url=spec["base_url"], api_key=key or None,
                 thinking=thinking)

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
            "thinking": thinking, "previous": before}

    if not verify:
        return {"ok": True, **base, "verified": False,
                "backup": str(backup) if backup else None}

    from agent.llm import chat_ex

    # 验证会真花一次调用。注意：`thinking=True` 时这条也一样要先想再答，
    # 实测约 6 秒、输出几百 token（不思考只要十几个）——不是 bug，是所选模式
    # 本身的价格，面板的确认弹窗里已经写明。
    started = time.time()
    try:
        text, usage = chat_ex("只回两个字：可用")
    except Exception as exc:  # noqa: BLE001
        _rollback()
        return {"ok": False, **base, "error": f"{type(exc).__name__}: {exc}",
                "rollback": backup is not None or bool(old_env)}

    elapsed = time.time() - started
    res = {"ok": True, **base, "verified": True,
           "reply": (text or "").strip()[:40], "elapsed": elapsed,
           "usage": usage, "backup": str(backup) if backup else None}

    # 「切到深度思考」到底成不成，只有 reasoning_tokens 说了算。
    # 只看 HTTP 200 不够：服务端拒过这个字段时 chat_ex 会去掉它重试，
    # 请求照样成功，但那时候的 200 只证明「模型答了」，不证明「它想了」。
    # 这里不判失败（配置本身是好的），只把不一致交回给面板显示。
    if thinking:
        got = int((usage or {}).get("reasoning_tokens") or 0)
        res["thinking_effective"] = bool(got)
        if not got:
            res["warning"] = ("服务端没回 reasoning_tokens，深度思考看起来没生效——"
                              "多半是上游不认 thinking 字段，已自动降级成不思考。")
    return res


def main() -> None:
    parser = argparse.ArgumentParser(description="换模型供应商并验证连通")
    parser.add_argument("provider", nargs="?", choices=sorted(PROVIDERS), help="供应商")
    parser.add_argument("--key", help="API key")
    parser.add_argument("--model", help="覆盖默认模型名")
    parser.add_argument("--think", action="store_true",
                        help="打开深度思考（只对 DeepSeek 生效；面板里是模型下拉的一项）")
    parser.add_argument("--show", action="store_true", help="只看当前配置，不改")
    args = parser.parse_args()

    if args.show:
        from agent.llm import current_config

        cfg = current_config()
        pid = provider_of(cfg["base_url"])
        who = (PROVIDERS[pid].get("label") or PROVIDERS[pid]["name"]) if pid else "自定义 / 未知"
        print(f"供应商   = {who}")
        print(f"base_url = {cfg['base_url']}")
        print(f"model    = {cfg['model']}")
        think = bool(cfg.get("thinking"))
        on = "开" if think else "关"
        if think and not cfg.get("thinking_effective"):
            on += "（未生效：该接口不认，或被服务端拒过）"
        print(f"深度思考 = {on}")
        if pid:
            idx = variant_index_of(pid, cfg["model"], think)
            vs = variants_of(pid)
            if idx >= 0:
                print(f"模型项   = 第 {idx + 1}/{len(vs)} 项 · {vs[idx]['label']}")
            else:
                print("模型项   = 不在下拉清单里（手输的模型名）")
        tail = f"（尾号 {cfg['key_tail']}，来自 {cfg['key_source']}）" if cfg["key_set"] else ""
        print(f"key      = {'已配置' if cfg['key_set'] else '缺失'}{tail}")
        return

    if not args.provider:
        parser.error("要指定供应商；只看配置用 --show")

    spec = PROVIDERS[args.provider]
    print(f"→ {spec['name']}")
    print(f"  base_url = {spec['base_url']}")
    print(f"  model    = {args.model or spec['model']}")
    print(f"  深度思考 = {'开' if args.think else '关'}")
    if spec["note"]:
        print(f"  注意：{spec['note']}")

    res = switch(args.provider, args.key, args.model, thinking=args.think)
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
