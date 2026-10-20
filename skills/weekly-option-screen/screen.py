#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周度期权筛选（weekly option screen）—— 找本周五到期的 put / call 候选。

用户口径（2026-10-19 确认 ✓）
  · 预算基准 $15–40 / 张（$25 为主力档）
  · 到期：未指定 ⇒ 本周五；指定 ⇒ 按指定的来
  · 方向：call + put 都出
  · 玩法：买权利金波段，赚了就跑，不等实值、不等到期

核心指标
  · 弹性  = |delta| × 现价 ÷ 权利金         （标的动 1% ⇒ 权利金动几 %）
  · 回本需动 = 买卖价差% ÷ 弹性              （真实摩擦；越小越好 ✓）
  · 隐含周幅 = ATM 跨式 ÷ 现价               （市场自己认为会不会大动）

用法
  python3 screen.py                          # 本周五 + $15-40 + call&put
  python3 screen.py --expiry 20261023        # 指定到期
  python3 screen.py --min 15 --max 25        # 收窄预算
  python3 screen.py --sides calls            # 只看 call
  python3 screen.py --out 结果.md            # 写文件
"""
import argparse
import datetime as dt
import math
import os
import sys

# ── IB 连接参数（与 QQQ 0DTE 实盘系统共用网关，但绝不用它的 clientId ✗）──
HOST, PORT = "127.0.0.1", 7497
CLIENT_ID = 26          # 专用；42 是 QQQ 实盘系统 ✓ 绝不要用
MAX_INFLIGHT = 18       # 同时请求的合约数上限（超过会被静默限流 ⇒ 卡死 ✗）
WAIT_SEC = 8            # 每批取数等待秒数

DEFAULT_NAMES = [
    # 价差最紧的大盘股（深虚策略优先 ✓）
    "TQQQ", "NVDA", "TSLA", "META", "PLTR", "SOXL", "COIN", "MSTR",
    # 高波动小票（预算更友好，但价差通常更宽 ⚠️）
    "SMCI", "IONQ", "SOFI", "MARA",
]


# ─────────────────────────── 数学 ───────────────────────────
def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, sig, right="C", r=0.04):
    """返回 (价格, delta)"""
    if T <= 0:
        if right == "C":
            return max(0.0, S - K), (1.0 if S > K else 0.0)
        return max(0.0, K - S), (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if right == "C":
        return S * _N(d1) - K * math.exp(-r * T) * _N(d2), _N(d1)
    return K * math.exp(-r * T) * _N(-d2) - S * _N(-d1), -_N(-d1)


def solve_sig(S, K, price, T, right="C"):
    """由**单腿**价格反解隐含 σ ✓（⚠️ 跨式必须 ÷2 再传进来 ✗）"""
    if price <= 0 or T <= 0:
        return 0.0
    lo, hi = 0.005, 8.0
    for _ in range(90):
        mid = (lo + hi) / 2
        v, _ = bs(S, K, T, mid, right)
        if v > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ─────────────────────────── 日期 ───────────────────────────
def now_et():
    """美东当前时间（自动处理 EDT/EST ✓）"""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        return dt.datetime.utcnow() - dt.timedelta(hours=4)


def default_expiry():
    """未指定到期 ⇒ 本周五；若今天已是周五 16:00 之后 ⇒ 下周五"""
    n = now_et()
    friday = n + dt.timedelta(days=(4 - n.weekday()) % 7)
    if friday.date() == n.date() and n.hour >= 16:
        friday += dt.timedelta(days=7)
    return friday.strftime("%Y%m%d")


def market_open_hint():
    n = now_et()
    mins = n.hour * 60 + n.minute
    if n.weekday() >= 5:
        return "⚠️ 今天是周末，期权无报价 ⇒ 结果只能参考上周五收盘，**请盘中使用** ✗"
    if mins < 9 * 60 + 30 or mins >= 15 * 60 + 45:
        return "⚠️ 当前不在 09:30–15:45 ET 的有效报价时段 ⇒ 报价可能缺失/价差异常，**建议盘中重跑** ✗"
    return ""


# ─────────────────────────── IB ───────────────────────────
def connect():
    from ib_async import IB
    ib = IB()
    last = None
    for cid in (CLIENT_ID, CLIENT_ID + 1, CLIENT_ID + 2):   # 握手偶发超时 ⇒ 换号重试 ✓
        try:
            ib.connect(HOST, PORT, clientId=cid, timeout=20)
            ib.reqMarketDataType(1)
            return ib
        except Exception as e:
            last = e
    raise RuntimeError(f"无法连接 IB Gateway({HOST}:{PORT})：{last!r}")


def spot(ib, sym):
    from ib_async import Stock
    c = Stock(sym, "SMART", "USD")
    if not ib.qualifyContracts(c):
        return None
    tk = ib.reqMktData(c, "", False, False)
    ib.sleep(1.8)
    v = tk.last if (tk.last and tk.last == tk.last) else tk.close
    ib.cancelMktData(c)
    return v if (v and v == v) else None


def strikes_for(ib, sym, expiry):
    from ib_async import Stock
    c = Stock(sym, "SMART", "USD")
    if not ib.qualifyContracts(c):
        return None, None
    ch = ib.reqSecDefOptParams(sym, "", "STK", c.conId)   # ⚠️ conId=0 会挂死 ✗
    smart = [x for x in ch if x.exchange == "SMART"]
    if not smart or expiry not in smart[0].expirations:
        return c, None
    return c, sorted(smart[0].strikes)


def quote_batch(ib, items, expiry):
    """items = [(sym, right, strike)]，≤ MAX_INFLIGHT ✓；返回 {(sym,right,k): (bid,ask,vol,oi)}"""
    from ib_async import Option
    objs, out = [], {}
    for sym, right, k in items:
        o = Option(sym, expiry, k, right, "SMART", tradingClass=sym, currency="USD")
        try:
            ib.qualifyContracts(o)
        except Exception:
            continue
        if not o.conId:
            continue                        # Error 200 = 无此合约 ⇒ 跳过 ✓
        objs.append((sym, right, k, o, ib.reqMktData(o, "100,101", False, False)))
    ib.sleep(WAIT_SEC)
    for sym, right, k, o, tk in objs:
        b, a = tk.bid, tk.ask
        if b and a and b > 0 and a > 0 and b == b and a == a:
            vol = tk.volume if (tk.volume and tk.volume == tk.volume) else 0
            oi = getattr(tk, "callOpenInterest" if right == "C" else "putOpenInterest", None)
            out[(sym, right, k)] = (b, a, int(vol), int(oi) if (oi and oi == oi) else 0)
        ib.cancelMktData(o)
    return out


# ─────────────────────────── 主流程 ───────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expiry", default=None)
    ap.add_argument("--min", type=float, default=15.0)
    ap.add_argument("--max", type=float, default=40.0)
    ap.add_argument("--sides", default="both", choices=["both", "calls", "puts"])
    ap.add_argument("--names", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    expiry = a.expiry or default_expiry()
    names = [x.strip().upper() for x in a.names.split(",") if x.strip()] or DEFAULT_NAMES
    sides = ["C", "P"] if a.sides == "both" else (["C"] if a.sides == "calls" else ["P"])
    exp_dt = dt.datetime.strptime(expiry, "%Y%m%d")
    T = max((exp_dt - now_et()).total_seconds(), 3600) / 86400.0 / 365.0

    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    emit(f"# 周度期权筛选 · 到期 {expiry[:4]}-{expiry[4:6]}-{expiry[6:]}（剩 {T*365:.1f} 天）")
    emit(f"> 预算 ${a.min:.0f}–${a.max:.0f}｜方向 {'+'.join('call' if s=='C' else 'put' for s in sides)}｜{now_et():%Y-%m-%d %H:%M} ET")
    h = market_open_hint()
    if h:
        emit(f"> {h}")
    emit("")

    try:
        ib = connect()
    except Exception as e:
        emit(f"**数据不可用**：{e}")
        emit("→ 未取到任何报价，**不做任何推断** ✗")
        _write(a.out, lines)
        return 2

    # 1) 现价
    spots = {}
    for s in names:
        v = spot(ib, s)
        if v:
            spots[s] = v
        else:
            emit(f"- {s}: 无现价 ✗ 跳过")
    emit(f"取到 {len(spots)}/{len(names)} 个标的现价 ✓\n")

    # 2) 参数化行权价 + 分批报价
    plan, chk = [], []
    for s, S in spots.items():
        c, ks = strikes_for(ib, s, expiry)
        if not ks:
            emit(f"- {s}: 无 {expiry} 到期 ✗ 跳过")
            continue
        chk.append((s, c, S, ks))
        for sgn in (-5, -10, -16):
            if "C" in sides:
                w = S * (1 + abs(sgn) / 100.0)
                plan.append((s, "C", min(ks, key=lambda x: abs(x - w))))
            if "P" in sides:
                w = S * (1 - abs(sgn) / 100.0)
                plan.append((s, "P", min(ks, key=lambda x: abs(x - w))))
    plan = list(dict.fromkeys(plan))

    quotes = {}
    for i in range(0, len(plan), MAX_INFLIGHT):
        quotes.update(quote_batch(ib, plan[i:i + MAX_INFLIGHT], expiry))

    # 3) 指标
    rows = []
    for (s, r, k), (b, q, vol, oi) in quotes.items():
        cost = q * 100
        if not (a.min <= cost <= a.max):
            continue
        mid = (b + q) / 2
        sig = solve_sig(spots[s], k, mid, T, r)
        if sig <= 0:
            continue
        _, d = bs(spots[s], k, T, sig, r)
        p0, _ = bs(spots[s], k, T, sig, r)
        p1, _ = bs(spots[s] * (1 + (0.01 if r == "C" else -0.01)), k, T, sig, r)
        dp = (p1 - p0) / p0 * 100 if p0 > 0 else 0
        el = abs(d) * spots[s] / mid
        spr = (q - b) / mid * 100
        be = spr / el if el > 0 else float("inf")
        rows.append(dict(sym=s, right=r, k=k, cost=cost, otm=(k / spots[s] - 1) * 100,
                         spr=spr, vol=vol, oi=oi, sig=sig * 100, el=el, dp=dp, be=be))

    if not rows:
        emit(f"**预算 ${a.min:.0f}–${a.max:.0f} 内没有任何带有效报价的合约** ✗")
        emit("→ 可能原因：市场未开 / 该到期无此价位 / 价差异常。请盘中使用或放宽预算 ✓")
        ib.disconnect()
        _write(a.out, lines)
        return 1

    # 4) 输出（按方向 + 回本需动升序）
    for r, label in (("C", "做多（call）"), ("P", "做空（put）")):
        sub = sorted([x for x in rows if x["right"] == r], key=lambda x: x["be"])
        if not sub:
            continue
        emit(f"## {label} 候选（按「回本所需波动」升序 ✓）\n")
        emit("| # | 标的 | 到期 | 行权价 | 1张成本 | 距现价 | 价差 | 量 | OI | 反解σ | 弹性 | 标的{sign}1%⇒权利金 | **回本需动** |")
        emit("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, x in enumerate(sub, 1):
            emit(f"| {i} | **{x['sym']}** | {expiry[:4]}-{expiry[4:6]}-{expiry[6:]} | "
                 f"**{x['k']:.2f}** | **${x['cost']:.0f}** | {x['otm']:+.1f}% | {x['spr']:.0f}% | "
                 f"{x['vol']:,} | {x['oi']:,} | {x['sig']:.0f}% | {x['el']:.0f} | "
                 f"{x['dp']:+.0f}% | **{x['be']:.2f}%** |")
        emit("")
        emit(f"**首选 3 张**：" + "；".join(
            f"{x['sym']} {x['k']:.1f}{x['right']} @${x['cost']:.0f}（回本 {x['be']:.2f}%）" for x in sub[:3]))
        emit("")

    # 5) 排除清单
    emit("## ⚠️ 排除清单")
    emit("- 预算内但**回本需动 > 0.5%** 的档不建议做（摩擦吃掉利润 ✗）：" +
         ("、".join(f"{x['sym']} {x['k']:.1f}{x['right']}({x['be']:.2f}%)" for x in rows if x["be"] > 0.5) or "无 ✓"))
    emit("- 预算外（成本高于上限）的深虚档未列为候选 ✗，如需要放宽 `--max` 重跑 ✓")
    emit("")

    # 6) 自检
    emit("## 自检 ✓")
    bad = [x for x in rows if x["el"] > 200 or x["be"] < 0.02]
    emit(f"- 弹性 > 200 或回本 < 0.02% 的异常项：{len(bad)} 个" +
         ("" if not bad else " ⚠️ 请核对原始报价（可能是 σ 反解或单位错误 ✗）"))
    emit(f"- 有效合约 {len(rows)} 张｜标的 {len(spots)} 个｜取数批次数 {math.ceil(len(plan)/MAX_INFLIGHT)}")
    emit("")
    emit("## 操作提醒 ✗")
    emit("- **限价单**下单（价差 2–20% 都会被市价单吃掉 ✗）")
    emit("- **尾盘 15:45–16:00 ET 不交易**（价差会翻倍）")
    emit(f"- 到期日 {expiry[:4]}-{expiry[4:6]}-{expiry[6:]}，虚值到期即**全损** ✗")
    emit("- 深虚周权**大概率归零**，请只用能全损的钱；以上为数据分析，非投资建议 ✓")

    ib.disconnect()
    _write(a.out, lines)
    return 0


def _write(path, lines):
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n（已写入 {path} ✓）")


if __name__ == "__main__":
    sys.exit(main())
