"""项目下一步建议器：回答「现在最该做什么」，不是「哪里坏了」。

和 tools/maintain.py 的分工
--------------------------
· maintain  守：有没有坏（账本坏行、备份打不开、测试红）。它不打分，只分级。
· next_step 攻：劲往哪使。它必须给出**一个**主张，而不是一张候选清单。

两件事的失败方式不一样：体检漏报会出事，建议含糊只是浪费时间。所以这里
宁可只输出一个主结论 + 两三个阻塞项，也不列十条「可以考虑」。

为什么不能靠打分排序
--------------------
「最该做什么」不是把候选按分数排一遍。这个项目的核心原则是「参数不可信，
样本外才可信」（PLAN.md），所以真正的第一问题是**证据够不够**：
  · 证据不够时，任何改动都是拟合噪声——此时唯一的正确动作是「别动」，
    而「别动」在任何候选清单里都不会出现，它的分数永远是 0。
  · 证据够了才轮到改哪一头；而改哪一头，项目自己已经有反事实结论
    （出场规则 227 笔回放，所有放松版本都更差 → 根子在入场）。
所以判定写成一条短路链：先证伪「能不能动」，再谈「动哪里」。

三条口径纪律
------------
1. **按 magic 过滤。** 账户里有别的 EA 和手工单（magic=0），不筛就是拿别人的
   战绩给自己下结论。实测 277733809 的 339 笔平仓里，本程序只占 316 笔，
   剩下 23 笔是 magic=0。
2. **不跨版本合并。** 换过策略的账户之间不能相加：新旧两版的胜率、期望、
   持仓时长都不同，合并后的均值没有任何统计含义。历史账户只做对照，
   并明确标注「不同版本，不合并」。
3. **不和 state 的累计值混用。** `autotrade_state.json` 的 closed/pnl 是**本场次**
   计数（面板重置后就从 0 开始），`deals.jsonl` 是账户全部历史。两者不等是
   设计如此，不是 bug——但报告里要写清用的是哪个，否则「11 笔 +12.54」和
   「14 笔 +23.40」会同时出现在两份材料里，谁也说不清哪个是真的。

用法
----
    python -m tools.next_step              # 自动选最近活跃账户
    python -m tools.next_step --login 165106125
    python -m tools.next_step --json       # 给定时任务消费
    python -m tools.next_step --magic 20261019   # 统计旧版程序的单
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import appdirs, paths  # noqa: E402

# 样本门槛。30 是这个项目自己在 PLAN.md 里用的量级（「微实盘 3 个月」），
# 也是中心极限定理下谈均值的最低限度。低于它，胜率差 10 个百分点只等于
# 三笔单的运气。
SAMPLE_MIN = 30
# 显著性门槛。不引 scipy：两行公式能算的东西不值得加一个 20MB 的依赖。
T_SIG = 2.0

# 阶段
SAMPLE_DEFICIT = "SAMPLE_DEFICIT"
NEG_EDGE = "NEG_EDGE"
POS_EDGE = "POS_EDGE"
INCONCLUSIVE = "INCONCLUSIVE"

_STAGE_TEXT = {
    SAMPLE_DEFICIT: "样本不足——此刻任何调参都是拟合噪声",
    NEG_EDGE: "负期望且统计显著——该改入场，不是出场",
    POS_EDGE: "正期望且统计显著——该验证稳定性，不是继续优化",
    INCONCLUSIVE: "期望与噪声不可区分——再积累，或先清阻塞项",
}

_STAGE_ACTION = {
    SAMPLE_DEFICIT: (
        "不要改参数。让当前版本跑够 {need} 笔（还差 {gap} 笔）。"
        "这一阶段唯一有价值的产出是更多样本，不是更好的参数。"
    ),
    NEG_EDGE: (
        "改入场逻辑。出场规则已经被 227 笔反事实回放否定过（所有放松版本更差），"
        "继续调 SL/TP/超时只是重复那次失败。先看教训库里复发最多的入场主题。"
    ),
    POS_EDGE: (
        "停止优化，转做样本外验证：按周/按时段切片，确认正期望不是某一段行情喂出来的。"
        "同时确认样本里有没有同一天连续多笔（相关性会抬高 t 值）。"
    ),
    INCONCLUSIVE: (
        "不动。t={t:.2f} 落在噪声区间，加样本比改代码划算。"
    ),
}


@dataclass
class Series:
    """一段成交序列的统计。"""

    n: int = 0
    net: float = 0.0
    wins: int = 0
    mean: float = 0.0
    std: float = 0.0
    t: float = 0.0
    first_ts: int = 0
    last_ts: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def expectancy(self) -> float:
        return self.mean

    def as_dict(self) -> dict:
        return {
            "n": self.n, "net": round(self.net, 2), "wins": self.wins,
            "win_rate": round(self.win_rate, 4), "mean": round(self.mean, 4),
            "std": round(self.std, 4), "t": round(self.t, 2),
        }


def _stats(pnls: list[float]) -> Series:
    n = len(pnls)
    if not n:
        return Series()
    net = sum(pnls)
    mean = net / n
    std = 0.0
    if n > 1:
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        std = math.sqrt(var)
    t = mean / (std / math.sqrt(n)) if std > 0 and n > 1 else 0.0
    return Series(n=n, net=net, wins=sum(1 for p in pnls if p > 0),
                  mean=mean, std=std, t=t)


def _read_deals(acct: Path) -> list[dict]:
    p = acct / "deals.jsonl"
    if not p.is_file():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 坏行由 maintain 负责报，这里跳过即可
            if isinstance(rec, dict):
                out.append(rec)
    except OSError:
        pass
    return out


def _closed_pnls(deals: list[dict], magic: int | None) -> list[tuple[int, float]]:
    """(平仓时间, 净盈亏)。

    `entry == 1` 才是平仓（DEAL_ENTRY_OUT）；`entry == 0` 是开仓，它的 profit
    恒为 0，算进去只会把笔数翻倍、把期望腰斩。手续费/隔夜利息/税都要加上：
    黄金 0.05 手一个来回的手续费可能比单笔利润还大。
    """
    out = []
    for r in deals:
        if r.get("entry") != 1:
            continue
        if magic is not None and int(r.get("magic") or 0) != magic:
            continue
        pnl = sum(float(r.get(k) or 0) for k in ("profit", "commission", "swap", "fee"))
        out.append((int(r.get("time") or 0), pnl))
    out.sort()
    return out


def _account_dirs() -> list[Path]:
    base = paths.ACCOUNTS_DIR
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and p.name.isdigit())


def _default_magic() -> int:
    """当前程序写的 magic。

    从 Cfg 取，取不到退回字面值。两者必须一致：改 live/autotrade.py 的
    `Cfg.magic` 时这里会跟着变，写死的话统计会静默变成 0 笔。
    """
    try:
        from live.autotrade import Cfg

        return int(Cfg().magic)
    except Exception:  # noqa: BLE001
        return 20261022


def _pick_account(magic: int, want: str | None) -> tuple[str, list[tuple[int, float]]]:
    """选主账户：本程序最近有成交的那个。

    「最近」比「笔数最多」更合适——笔数最多的是历史账户，它反映的是旧代码，
    拿旧代码的结论指导今天的修改是本末倒置。
    """
    best: tuple[str, list[tuple[int, float]]] | None = None
    for acct in _account_dirs():
        if want and acct.name != str(want):
            continue
        rows = _closed_pnls(_read_deals(acct), magic)
        if not rows:
            continue
        if best is None or rows[-1][0] > best[1][-1][0]:
            best = (acct.name, rows)
    return best if best else (want or "", [])


def _blockers(account: str) -> list[dict]:
    """阻塞项：这些不清，主动作做了也白做。

    刻意不复用 maintain 的检查器——两个工具的失效应该互相独立。体检器某天
    改坏了，建议器不该跟着失聪。这里只取三个「影响下一步决策」的信号。
    """
    out: list[dict] = []

    st_p = paths.ACCOUNTS_DIR / account / "autotrade_state.json" if account else None
    if st_p and st_p.is_file():
        try:
            st = json.loads(st_p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            st = {}
        pending = st.get("pending_reviews") or []
        if pending:
            out.append({
                "kind": "未复盘交易",
                "detail": f"{len(pending)} 笔已平仓但没跑复盘",
                "why": "教训没入库，等于这几笔的学费白交；下一轮提示词也拿不到它",
            })

    # 未提交 / 未推：影响「能不能把当前版本定下来继续跑」
    code, dirty = _git("status", "--porcelain")
    if code == 0 and dirty:
        n = len(dirty.splitlines())
        out.append({"kind": "工作区未提交", "detail": f"{n} 个文件",
                    "why": "跑样本期间混着未定稿的改动，出问题无法确定是哪一版的行为"})
    code, ahead = _git("rev-list", "--count", "@{u}..HEAD")
    if code != 0:
        # 没配 upstream 时 @{u} 会报错，这项检查就静默失效了——退化成显式比较
        # origin/main。宁可多一次容错，也别让「未推送」在发版前夜悄悄不报。
        code, ahead = _git("rev-list", "--count", "origin/main..HEAD")
    if code == 0 and ahead.strip().isdigit() and int(ahead) > 0:
        out.append({"kind": "未推送提交", "detail": f"领先远端 {ahead.strip()} 个",
                    "why": "发版时 gh 会在远端默认分支尖端建 tag，容易挂错源码"})

    # 体检报告的 FAIL：只认 24 小时内的，避免拿上周的结论当今天的状态
    latest = appdirs.data_dir() / "maintenance" / "latest.json"
    if latest.is_file():
        try:
            rep = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rep = {}
        age = time.time() - float(rep.get("ts") or 0)
        fails = [i for i in (rep.get("items") or []) if i.get("level") == "FAIL"]
        if fails and age < 86400:
            out.append({"kind": "体检 FAIL", "detail": f"{len(fails)} 项",
                        "why": "账本/备份的硬问题优先于任何策略改动"})
    return out


def _git(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return proc.returncode, (proc.stdout or "").strip()


def _lesson_signal(account: str) -> list[dict]:
    """教训库里复发最多的主题。

    走 lesson_stats.build_report，不自己数：主题归并（尤其出场三兄弟要按方向
    拆）只在 effective_theme 一处实现，另起一套口径必然和注入口径对不上。
    """
    try:
        from tools.lesson_stats import build_report

        rep = build_report(account or None)
    except Exception:  # noqa: BLE001
        return []
    themes = rep.get("themes") or []
    return [t for t in themes if t.get("count", 0) >= 3][:3]


def _version_compare(magic: int, account: str) -> list[dict]:
    """其他账户的同类统计，只做对照，**不合并**。

    两个账户之间可能隔着一次策略改版。合并计算会得到一个既不属于旧版也不
    属于新版的数字——它唯一的作用是让人误以为「整体是负期望」。
    """
    out = []
    for acct in _account_dirs():
        if acct.name == account:
            continue
        rows = _closed_pnls(_read_deals(acct), magic)
        if not rows:
            continue
        s = _stats([p for _, p in rows])
        out.append({
            "login": acct.name,
            **s.as_dict(),
            "last": time.strftime("%Y-%m-%d", time.localtime(rows[-1][0])),
            "note": "不同账户/版本，不合并",
        })
    return out


def _stage_of(s: Series) -> str:
    if s.n < SAMPLE_MIN:
        return SAMPLE_DEFICIT
    if s.mean < 0 and s.t <= -T_SIG:
        return NEG_EDGE
    if s.mean > 0 and s.t >= T_SIG:
        return POS_EDGE
    return INCONCLUSIVE


def _action_text(stage: str, s: Series) -> str:
    tpl = _STAGE_ACTION[stage]
    return tpl.format(need=SAMPLE_MIN, gap=max(0, SAMPLE_MIN - s.n), t=s.t)


@dataclass
class Advice:
    account: str
    magic: int
    stage: str
    series: dict
    headline: str
    action: str
    blockers: list[dict] = field(default_factory=list)
    lessons: list[dict] = field(default_factory=list)
    others: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "account": self.account, "magic": self.magic, "stage": self.stage,
            "stage_text": _STAGE_TEXT[self.stage],
            "series": self.series, "headline": self.headline, "action": self.action,
            "blockers": self.blockers, "lessons": self.lessons, "others": self.others,
        }


def build(login: str | None = None, magic: int | None = None) -> Advice:
    magic = _default_magic() if magic is None else magic
    account, rows = _pick_account(magic, login)
    pnls = [p for _, p in rows]
    s = _stats(pnls)
    stage = _stage_of(s)

    if not rows:
        headline = f"账户 {account or '(无)'} 没有 magic={magic} 的平仓记录"
        action = "先确认程序真的在以这个 magic 下单（面板设置 → magic），否则统计永远是 0 笔"
        stage = SAMPLE_DEFICIT
    else:
        headline = (
            f"{account}：{s.n} 笔，净 {s.net:+.2f}，胜率 {s.win_rate:.1%}，"
            f"期望 {s.mean:+.3f}/笔，t={s.t:.2f}"
        )
        action = _action_text(stage, s)

    return Advice(
        account=account, magic=magic, stage=stage, series=s.as_dict(),
        headline=headline, action=action,
        blockers=_blockers(account), lessons=_lesson_signal(account),
        others=_version_compare(magic, account),
    )


def render(a: Advice) -> str:
    lines = [
        f"下一步建议  {date.today().isoformat()}  magic={a.magic}",
        "=" * 78,
        f"阶段：{a.stage}  {_STAGE_TEXT[a.stage]}",
        "",
        f"证据：{a.headline}",
    ]
    if a.blockers:
        lines.append("")
        lines.append(f"阻塞 {len(a.blockers)} 项（不清掉，主动作做了也白做）：")
        for b in a.blockers:
            lines.append(f"  · {b['kind']}：{b['detail']}")
            lines.append(f"    {b['why']}")
    if a.lessons:
        lines.append("")
        lines.append("教训库复发主题（说明提示词还没压住）：")
        for t in a.lessons:
            lines.append(f"  · {t['theme']}（{t['scope']}）{t['count']} 次，"
                         f"{t['sessions']} 个场次，趋势 {t.get('trend') or '—'}")
    if a.others:
        lines.append("")
        lines.append("对照账户（不同版本，**不合并**）：")
        for o in a.others:
            lines.append(f"  · {o['login']}：{o['n']} 笔，净 {o['net']:+.2f}，"
                         f"期望 {o['mean']:+.3f}，t={o['t']:.2f}，最后 {o['last']}")
    lines.append("")
    lines.append("-" * 78)
    lines.append("该做什么：")
    lines.append("  " + a.action)
    return "\n".join(lines)


def _write_report(a: Advice) -> Path:
    out_dir = appdirs.data_dir() / "maintenance"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"next-{date.today().isoformat()}.md"
    body = ["# 下一步建议 " + date.today().isoformat(), "",
            f"**阶段**：{a.stage} — {_STAGE_TEXT[a.stage]}", "",
            f"**证据**：{a.headline}", ""]
    if a.blockers:
        body.append("## 阻塞项")
        for b in a.blockers:
            body.append(f"- **{b['kind']}**：{b['detail']} — {b['why']}")
        body.append("")
    if a.lessons:
        body.append("## 教训库复发主题")
        for t in a.lessons:
            body.append(f"- {t['theme']}（{t['scope']}）：{t['count']} 次 / "
                        f"{t['sessions']} 场次，趋势 {t.get('trend') or '—'}")
        body.append("")
    if a.others:
        body.append("## 对照账户（不同版本，不合并）")
        for o in a.others:
            body.append(f"- {o['login']}：{o['n']} 笔，净 {o['net']:+.2f}，"
                        f"期望 {o['mean']:+.3f}，t={o['t']:.2f}")
        body.append("")
    body += ["## 该做什么", "", a.action, ""]
    path.write_text("\n".join(body), encoding="utf-8")
    (out_dir / "next-latest.json").write_text(
        json.dumps({"ts": int(time.time()), **a.as_dict()}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tools.next_step",
        description="根据实盘证据判断当前阶段，给出唯一的主要动作",
    )
    ap.add_argument("--login", help="指定账户，默认取本程序最近有成交的那个")
    ap.add_argument("--magic", type=int, default=None,
                    help="程序 magic，默认取 Cfg.magic")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args(argv)

    advice = build(args.login, args.magic)
    if args.json:
        print(json.dumps({"ts": int(time.time()), **advice.as_dict()},
                         ensure_ascii=False, indent=2))
    else:
        print(render(advice))
    if not args.no_report:
        path = _write_report(advice)
        if not args.json:
            print(f"\n报告：{path}")
    return 0  # 建议不是失败：始终 0，避免定时任务把「该改入场」当异常


if __name__ == "__main__":
    raise SystemExit(main())
