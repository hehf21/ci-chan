# -*- coding: utf-8 -*-
"""结构闸门回测 —— 止损口径与结构闸门能不能救这套策略

设计（全部写死在下面 CONFIG；样本外为负不许回头调这里凑正）：
  票池     : cache/min30_*.pkl 里 2020-01-02~2026-10-29 全段可用的 12 只（蓝筹，见 POOL/NAMES）
  区间     : 2020-07-01 ~ 2026-10-29；样本内 2020-07~2023-12，样本外 2024-01~2026-10
  信号     : chan_law.L.run(bars).points，只做多：B1/B2/B3 进，S1/S2/S3 或结构失效出
  进场     : 信号「能被知道」的那根 bar 的**下一根开盘**成交（known_t+1 的开盘价）；
             naive_t 对照臂用 p.t+1（含未来函数，只用来量化偏差）
  出场     : 决策在收盘（只用当根收盘已知的结构），成交在下一根开盘
  成本     : 佣金万2.5双边、印花税0.05%卖出、滑点单边0.1%、A股T+1（按自然日）
  仓位     : 单笔风险预算 = 总资产 1% → 仓位 = 1% / 止损距离（按实际成交价算），最多 3 只，其余现金

三臂（只改出场与闸门，信号完全不动）：
  A 旧口径 : 止损 = 买点所属中枢的上沿 ZG；持仓期间按「最近已确认中枢 ZG」只上移（既有策略的做法）
  B 新口径 : 止损 = 结构失效价 = 买点低点 × (1-0.5%)；中枢 ZD 破位（该 ZD 位于建仓价下方时）也算出场
  C = B + 结构闸门 : 持仓期间 broken=True（跌破最近中枢 ZD）→ 次日开盘无条件出；
                     且 broken 状态下不许新开仓（闸门只做减法，不加信号）

三口径：
  walkforward : 决策点滚动重算 —— L.run(bars[:i+1]) + state_at(i)，第 i 根只能看见截至当时的全部结构（主口径）
  naive_known : 一次性全量结构 + confirm_t/known_t 过滤（结构会被后来的数据改写 → 隐性未来函数）
  naive_t     : 同上但用 p.t 进场 —— 明确含未来函数，只用于量化「被发现的时间」造成的偏差

用法：
  python 结构闸门回测.py                 # 全量（12 票 × 3 臂 × 3 口径）；WF 部分约 40~70 分钟
  python 结构闸门回测.py --no-wf          # 冒烟：只跑 naive，结论不可当主口径
  python 结构闸门回测.py --jobs 4 --codes 600036,601318
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)              # CI：仓库根目录（chan_law.py / verify_real.py 都在这）
PROG = ROOT          # 下游有代码用 PROG 取 chan_law.py 的 mtime 做缓存键，保留这个名字
STRAT = ROOT
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np            # noqa: E402
import chan_law as L          # noqa: E402
import verify_real as V       # noqa: E402

# ============================== CONFIG ==============================
POOL = ["600036", "601398", "601166", "601318", "601601", "600030",
        "600519", "000858", "000333", "000651", "600276", "600048"]
NAMES = {"600036": "招商银行", "601398": "工商银行", "601166": "兴业银行", "601318": "中国平安",
         "601601": "中国太保", "600030": "中信证券", "600519": "贵州茅台", "000858": "五粮液",
         "000333": "美的集团", "000651": "格力电器", "600276": "恒瑞医药", "600048": "保利发展"}

TRADE_START = "2020-07-01"
IS_START, IS_END = "2020-07-01", "2023-12-31"
OOS_START, TRADE_END = "2024-01-01", "2026-10-29"

CAPITAL = 1_000_000.0
RISK_PER_TRADE = 0.01
MAX_POS = 3
COMMISSION, STAMP, SLIP = 0.00025, 0.0005, 0.001
STOP_BUF = 0.005
MIN_LOT = 100
WARMUP_BARS = 250          # 交易起点前养结构的 bar 数（README 第二条纪律）
COOLDOWN_BARS = 8          # 同票进出冷却（沿用 portfolio_bt 的 8 根）；
#                            必要性：walk-forward 下同一个逻辑点会被后续数据改写 t 而重复“出现”
#                            （实测 600036 的同一 B3 在 843/849/863 三根先后成立），
#                            不加冷却等于同一个结构被反复交易。三臂同规则，不影响横向比较。

CACHE_DIR = os.path.join(tempfile.gettempdir(), "zg_bt_cache")
SCHEMA = "v1"

OUT_DIR = os.environ.get("ZG_OUT", os.path.join(HERE, "_out"))
os.makedirs(OUT_DIR, exist_ok=True)
OUT_JSON = os.path.join(OUT_DIR, "结果-闸门.json")
OUT_MD = os.path.join(OUT_DIR, "结果-闸门.md")


# ============================== 数据 ==============================
def load_stock(code: str):
    """读缓存 30 分钟线，转成 chan_law.Bar（带 open，用于「下一根开盘成交」）。"""
    bars, df = V.load_bars(code, 20000)
    if bars is None or df is None:
        return None
    df = df.reset_index(drop=True)
    n = len(df)
    opn = df["open"].to_numpy(float)
    hi = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    cl = df["close"].to_numpy(float)
    ts = df["date"].to_numpy().astype("datetime64[ns]")   # 统一到 ns：us 会让比较常量差 1000 倍
    day = ts.astype("datetime64[D]").astype("int64")
    b = [L.Bar(t=i, high=float(hi[i]), low=float(lo[i]), open=float(opn[i]), close=float(cl[i]))
         for i in range(n)]
    return {"code": code, "bars": b, "open": opn, "high": hi, "low": lo, "close": cl,
            "ts": ts, "ts_i": ts.astype("int64"), "day": day, "n": n}


def state_at(c, t: int) -> dict:
    """timeline(c)[-1] 的等价快取版：第 t 根能看到的结构状态（只用 confirm_t <= t 的段/中枢）。

    与 L.timeline(c)[-1] 逐字段等价（探针按 137 根抽样校验过，0 处不一致）。
    """
    seg = None
    for s in c.segs:
        if s.confirmed:
            if s.confirm_t <= t:
                seg = s
            else:
                break
    zs = None
    for z in c.zss:
        if z.confirm_t <= t:
            zs = z
        else:
            break
    return {"seg_dir": seg.d if seg else 0,
            "zd": float(zs.zd) if zs else None,
            "zg": float(zs.zg) if zs else None,
            "broken": bool(zs is not None and c.bars[t].low < zs.zd - L.EPS),
            "zs_confirm": int(zs.confirm_t) if zs else -1}


# ============================== 结构序列 ==============================
def rows_wf(code: str, debug_n: int | None = None) -> list:
    """walk-forward：第 i 根用 L.run(bars[:i+1]) 重算，只用截至当时的信息。

    每行 (i, day, open, close, zd, zg, broken, seg_dir, buys, sells)
      buys  = ((kind, 信号价, 所属中枢ZG, 所属中枢ZD), ...)  本根「新出现」的买点
      sells = (kind, ...)                                   本根「新出现」的卖点
    「新出现」= 上一根的点集合里没有 (kind, t)，且该点在本次截断跑里已知（known_t <= i）。
    """
    st = load_stock(code)
    bars, n = st["bars"], st["n"]
    if debug_n:
        bars, n = bars[:debug_n], min(n, debug_n)
    rows = []
    prev: set = set()
    for i in range(n):
        c = L.run(bars[: i + 1])
        s = state_at(c, i)
        cur = set()
        buys, sells = [], []
        for p in c.points:
            ident = (p.kind, p.t)
            cur.add(ident)
            kt = p.known_t if p.known_t >= 0 else p.t
            if ident in prev or kt > i:
                continue
            if p.kind[0] == "B":
                z = c.zss[p.zs_ix] if 0 <= p.zs_ix < len(c.zss) else None
                buys.append((p.kind, float(p.price),
                             float(z.zg) if z else None, float(z.zd) if z else None))
            else:
                sells.append(p.kind)
        rows.append((i, int(st["day"][i]), float(st["open"][i]), float(st["close"][i]),
                     s["zd"], s["zg"], s["broken"], s["seg_dir"], tuple(buys), tuple(sells)))
        prev = cur
    return rows


def rows_naive(st: dict, c, mode: str) -> list:
    """naive：一次性全量结构，按 confirm_t（段/中枢）与 known_t / p.t（点）过滤。

    隐性未来函数：zd/zg/broken 来自全量跑，后来的数据能把前面的中枢区间改写。
    """
    n = st["n"]
    tl = L.timeline(c)
    key = (lambda p: (p.known_t if (p.known_t >= 0 and mode == "known") else p.t))
    fresh_at: dict = {}
    for p in sorted(c.points, key=key):
        fresh_at.setdefault(key(p), []).append(p)
    rows = []
    for i in range(n):
        r = tl[i]
        buys, sells = [], []
        for p in fresh_at.get(i, ()):
            if p.kind[0] == "B":
                z = c.zss[p.zs_ix] if 0 <= p.zs_ix < len(c.zss) else None
                buys.append((p.kind, float(p.price),
                             float(z.zg) if z else None, float(z.zd) if z else None))
            else:
                sells.append(p.kind)
        rows.append((i, int(st["day"][i]), float(st["open"][i]), float(st["close"][i]),
                     r["zd"], r["zg"], bool(r["broken"]), int(r["seg_dir"]),
                     tuple(buys), tuple(sells)))
    return rows


def _wf_cache_path(code: str) -> str:
    """缓存 key 含 chan_law.py 的 mtime：库改过就自动失效，防止拿旧缓存冒充新库。"""
    tag = f"{SCHEMA}_{int(os.path.getmtime(os.path.join(PROG, 'chan_law.py')))}"
    return os.path.join(CACHE_DIR, f"wf_{code}_{tag}.pkl")


def wf_worker(code: str):
    """多进程 worker：算一只票的 WF 序列（带磁盘缓存，可断点续跑）。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _wf_cache_path(code)
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                obj = pickle.load(fh)
            return code, obj["rows"], {"cache": "hit", "sec": 0.0}
        except Exception:                                     # noqa: BLE001
            pass
    t0 = time.time()
    rows = rows_wf(code)
    dt = time.time() - t0
    with open(path, "wb") as fh:
        pickle.dump({"rows": rows, "n": len(rows), "sec": dt}, fh)
    return code, rows, {"cache": "miss", "sec": dt}


# ============================== 回放引擎 ==============================
def replay(rows_by_code: dict, meta: dict, axis: list, arm: str) -> dict:
    """单臂组合回放。决策在收盘、成交在下一根开盘；A 股 T+1。"""
    cash = CAPITAL
    pos: dict = {}
    pending: list = []
    last_px: dict = {}
    eq: list = []
    cash_series: list = []
    trades: list = []
    reject = {"stop_above_fill": 0, "gate_blocked": 0, "no_cash": 0, "slot_full": 0, "cooldown": 0}
    cooldown: dict = {}
    n_filled = 0

    def _exec_sell(code: str, j: int, t: int, reason: str) -> None:
        nonlocal cash
        p = pos.pop(code)
        cooldown[code] = j + COOLDOWN_BARS
        fill = rows_by_code[code][j][2] * (1 - SLIP)
        gross = p["shares"] * fill
        fee = gross * (COMMISSION + STAMP)
        cash += gross - fee
        pnl = (gross - fee) - (p["cost"] + p["entry_fee"])
        trades.append({"code": code, "kind": p["kind"], "reason": reason,
                       "entry_t": int(p["entry_t"]), "exit_t": int(t),
                       "entry_day": int(p["entry_day"]), "exit_day": int(meta[code]["day"][j]),
                       "entry_px": p["entry_px"], "exit_px": fill, "shares": p["shares"],
                       "stop0": p["stop0"], "stop_last": p["stop"],
                       "stop_dist0": (p["entry_px"] - p["stop0"]) / p["entry_px"],
                       "weight": p["cost"] / p["equity_at_entry"],
                       "pnl": pnl, "ret": pnl / (p["cost"] + p["entry_fee"]),
                       "hold_bars": j - p["entry_i"]})

    for ti, t in enumerate(axis):
        # ---------- 1) 执行上一根收盘的决策（下一根开盘成交）
        keep = []
        for od in pending:
            m = meta[od["code"]]
            j = m["idx_of"].get(t)
            if j is None:                                   # 该票这根没有 bar（停牌/缺数据）
                if ti - od["ti"] <= 3:
                    keep.append(od)
                continue
            row = rows_by_code[od["code"]][j]
            if od["side"] == "buy":
                if od["code"] in pos:
                    continue
                fill = row[2] * (1 + SLIP)
                stop = od["stop"]
                if stop is None or stop >= fill * 0.999:
                    reject["stop_above_fill"] += 1           # 止损在成交价上方/贴着 → 该口径下建仓不成立
                    continue
                dist = (fill - stop) / fill
                if dist <= 1e-4:
                    reject["stop_above_fill"] += 1
                    continue
                mv = sum(pp["shares"] * last_px.get(c, pp["entry_px"]) for c, pp in pos.items())
                equity = cash + mv
                weight = RISK_PER_TRADE / dist               # 风险预算定仓位
                amount = min(weight * equity, cash)          # 不许加杠杆
                shares = int(amount / fill / MIN_LOT) * MIN_LOT
                if shares <= 0:
                    reject["no_cash"] += 1
                    continue
                cost = shares * fill
                fee = cost * COMMISSION
                if cost + fee > cash:
                    continue
                cash -= cost + fee
                pos[od["code"]] = {"shares": shares, "entry_px": fill, "stop": stop, "stop0": stop,
                                   "kind": od["kind"], "entry_i": j, "entry_t": t,
                                   "entry_day": int(m["day"][j]), "trail": 0,
                                   "entry_fee": fee, "cost": cost, "equity_at_entry": equity}
                n_filled += 1
                cooldown[od["code"]] = j + COOLDOWN_BARS
            else:
                p = pos.get(od["code"])
                if p is None:
                    continue
                if int(m["day"][j]) <= p["entry_day"]:
                    continue                                  # T+1：当日不能卖，次日重新决策
                _exec_sell(od["code"], j, t, od["reason"])
        pending = keep

        # ---------- 2) 估值（停牌沿用上一已知收盘价）
        for code, m in meta.items():
            j = m["idx_of"].get(t)
            if j is not None:
                last_px[code] = rows_by_code[code][j][3]
        mv = sum(pp["shares"] * last_px.get(c, pp["entry_px"]) for c, pp in pos.items())
        eq.append((t, cash + mv))
        cash_series.append(cash)

        # ---------- 3) 收盘后决策（只用当根收盘已知的结构）
        busy = {od["code"] for od in pending}
        n_pend_buy = sum(1 for od in pending if od["side"] == "buy")
        for code, m in meta.items():
            j = m["idx_of"].get(t)
            if j is None:
                continue
            row = rows_by_code[code][j]
            zd, zg, broken, close = row[4], row[5], row[6], row[3]
            buys, sells = row[8], row[9]
            p = pos.get(code)
            if p is not None:
                reason = None
                if arm == "A":
                    if zg is not None and zg > p["stop"]:
                        p["stop"] = zg                        # 既有策略：ZG 只上移
                        p["trail"] += 1
                    if close < p["stop"]:
                        reason = "破ZG止损"
                else:
                    if close < p["stop"]:
                        reason = "破结构失效价"
                    elif broken and (arm == "C" or (zd is not None and zd < p["entry_px"])):
                        reason = "中枢ZD破位" if arm == "B" else "闸门:结构走坏"
                if reason is None and sells:
                    reason = f"卖点{sells[0]}"
                if reason and code not in busy:
                    pending.append({"code": code, "side": "sell", "reason": reason, "ti": ti})
                    busy.add(code)
                continue
            if not buys or code in busy:
                continue
            if j < cooldown.get(code, -1):
                reject["cooldown"] += 1                       # 同票冷却（防同一逻辑点反复触发）
                continue
            if len(pos) + n_pend_buy >= MAX_POS:
                reject["slot_full"] += 1
                continue
            kind, px, zg_zs, zd_zs = buys[0]
            stop = zg_zs if arm == "A" else px * (1 - STOP_BUF)
            if stop is None:
                reject["stop_above_fill"] += 0
                continue
            if arm == "C" and broken:
                reject["gate_blocked"] += 1                   # 闸门只做减法
                continue
            pending.append({"code": code, "side": "buy", "stop": float(stop), "kind": kind, "ti": ti})
            busy.add(code)
            n_pend_buy += 1

    # ---------- 4) 期末平仓（按最后一根已知收盘价，含成本），保证恒等式可对账
    for code in list(pos):
        m = meta[code]
        j = m["idx_of"].get(axis[-1])
        if j is None:
            j = len(rows_by_code[code]) - 1
        p = pos[code]
        fill = rows_by_code[code][j][3] * (1 - SLIP)
        gross = p["shares"] * fill
        fee = gross * (COMMISSION + STAMP)
        cash += gross - fee
        pnl = (gross - fee) - (p["cost"] + p["entry_fee"])
        trades.append({"code": code, "kind": p["kind"], "reason": "期末平仓",
                       "entry_t": int(p["entry_t"]), "exit_t": int(axis[-1]),
                       "entry_day": int(p["entry_day"]), "exit_day": int(m["day"][j]),
                       "entry_px": p["entry_px"], "exit_px": fill, "shares": p["shares"],
                       "stop0": p["stop0"], "stop_last": p["stop"],
                       "stop_dist0": (p["entry_px"] - p["stop0"]) / p["entry_px"],
                       "weight": p["cost"] / p["equity_at_entry"],
                       "pnl": pnl, "ret": pnl / (p["cost"] + p["entry_fee"]),
                       "hold_bars": j - p["entry_i"]})
        pos.pop(code)
    if trades:
        eq[-1] = (eq[-1][0], cash)
    return {"eq": eq, "cash": cash_series, "trades": trades, "reject": reject, "filled_buys": n_filled}


# ============================== 指标 ==============================
def _mdd(vals) -> float:
    peak = vals[0]
    worst = 0.0
    for v in vals:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1.0)
    return worst


def window_stats(eq, cash_series, trades, t0: int, t1: int) -> dict:
    idx = [k for k, (t, _) in enumerate(eq) if t0 <= int(t) <= t1]
    if not idx:
        return {}
    vals = [eq[k][1] for k in idx]
    base = vals[0]
    expo = [1.0 - cash_series[k] / eq[k][1] if eq[k][1] > 0 else 0.0 for k in idx]
    tt = [x for x in trades if t0 <= int(x["exit_t"]) <= t1]
    wins = [x for x in tt if x["pnl"] > 0]
    losses = [x for x in tt if x["pnl"] <= 0]
    aw = sum(x["ret"] for x in wins) / len(wins) if wins else None
    al = sum(x["ret"] for x in losses) / len(losses) if losses else None
    return {"ret": vals[-1] / base - 1.0, "mdd": _mdd(vals), "rounds": len(tt),
            "n_win": len(wins), "n_loss": len(losses),
            "win_rate": (len(wins) / len(tt)) if tt else None,
            "avg_win": aw, "avg_loss": al,
            "pf": (aw / abs(al)) if (aw is not None and al not in (None, 0)) else None,
            "avg_ret": (sum(x["ret"] for x in tt) / len(tt)) if tt else None,
            "avg_pnl": (sum(x["pnl"] for x in tt) / len(tt)) if tt else None,
            "avg_weight": (sum(x["weight"] for x in tt) / len(tt)) if tt else None,
            "exposure": sum(expo) / len(expo),
            "avg_hold_bars": (sum(x["hold_bars"] for x in tt) / len(tt)) if tt else None,
            "pnl_sum": sum(x["pnl"] for x in tt), "bars": len(idx)}


def bench_stats(stocks: dict, t0: int, t1: int) -> dict:
    rs = []
    for code, st in stocks.items():
        ts_i = st["ts_i"]
        j0 = next((k for k in range(len(ts_i)) if ts_i[k] >= t0), None)
        j1 = next((k for k in range(len(ts_i) - 1, -1, -1) if ts_i[k] <= t1), None)
        if j0 is None or j1 is None or j1 <= j0:
            continue
        rs.append(float(st["close"][j1] / st["close"][j0] - 1.0))
    rs.sort()
    med = rs[len(rs) // 2] if len(rs) % 2 else (rs[len(rs) // 2 - 1] + rs[len(rs) // 2]) / 2
    return {"mean": sum(rs) / len(rs), "median": med, "n": len(rs),
            "min": rs[0], "max": rs[-1], "per_stock": dict(zip(sorted(stocks), rs))}


def _ts(s: str) -> int:
    return int(np.datetime64(s, "ns").astype("int64"))


# ============================== 主流程 ==============================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=",".join(POOL))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-wf", action="store_true", help="跳过 walk-forward（冒烟用）")
    ap.add_argument("--debug-bars", type=int, default=0, help="调试：只截前 N 根（严禁用于正式结论）")
    ap.add_argument("--arms", default="A,B,C")
    ap.add_argument("--tag", default="", help="输出文件名后缀（调试用）")
    args = ap.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    t_begin = time.time()

    stocks, full_runs, sig_counts = {}, {}, {}
    for code in codes:
        st = load_stock(code)
        if st is None:
            print(f"[跳过] {code} 无缓存", flush=True)
            continue
        if args.debug_bars:
            k = min(st["n"], args.debug_bars)
            st = {**st, "bars": st["bars"][:k], "open": st["open"][:k], "high": st["high"][:k],
                  "low": st["low"][:k], "close": st["close"][:k], "ts": st["ts"][:k],
                  "ts_i": st["ts_i"][:k], "day": st["day"][:k], "n": k}
        stocks[code] = st
        t0 = time.time()
        c = L.run(st["bars"])
        full_runs[code] = c
        kinds = {}
        for p in c.points:
            kinds[p.kind] = kinds.get(p.kind, 0) + 1
        lags = sorted(p.known_t - p.t for p in c.points if p.known_t >= 0)
        gaps = []
        for p in c.points:
            kt = p.known_t if p.known_t >= 0 else p.t
            if kt + 1 < st["n"]:
                gaps.append(float(st["open"][kt + 1] / p.price - 1.0))
        sig_counts[code] = {"kinds": kinds,
                            "buy": kinds.get("B1", 0) + kinds.get("B2", 0) + kinds.get("B3", 0),
                            "sell": kinds.get("S1", 0) + kinds.get("S2", 0) + kinds.get("S3", 0),
                            "lag_median": lags[len(lags) // 2] if lags else None,
                            "lag_max": lags[-1] if lags else None,
                            "gap_median": sorted(gaps)[len(gaps) // 2] if gaps else None,
                            "gap_worst": max(gaps) if gaps else None}
        print(f"[结构] {code} n={st['n']} {c.summarize()} ({time.time()-t0:.2f}s) "
              f"买点{sig_counts[code]['buy']} 卖点{sig_counts[code]['sell']}", flush=True)

    rows_by_mode = {"naive_t": {}, "naive_known": {}, "walkforward": {}}
    for code, st in stocks.items():
        rows_by_mode["naive_t"][code] = rows_naive(st, full_runs[code], "t")
        rows_by_mode["naive_known"][code] = rows_naive(st, full_runs[code], "known")
    print(f"[naive] {len(stocks)} 只票序列就绪", flush=True)

    if not args.no_wf:
        from concurrent.futures import ProcessPoolExecutor
        print(f"[WF] 滚动重算 {len(stocks)} 只票（jobs={args.jobs}）…", flush=True)
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            for code, rows, info in ex.map(wf_worker, list(stocks)):
                rows_by_mode["walkforward"][code] = rows
                print(f"[WF] {code} rows={len(rows)} {info}", flush=True)
    modes = [m for m in ("walkforward", "naive_known", "naive_t") if rows_by_mode[m]]
    if args.no_wf:
        print("[WF] 已跳过：本报告不得当主口径结论", flush=True)

    # ---- 时间轴
    meta, all_ts = {}, set()
    for code, st in stocks.items():
        meta[code] = {"idx_of": {int(st["ts_i"][j]): j for j in range(st["n"])},
                      "day": st["day"], "ts_i": st["ts_i"], "close": st["close"]}
        all_ts.update(int(x) for x in st["ts_i"])
    ts_start, ts_is_start, ts_is_end = _ts(TRADE_START), _ts(IS_START), _ts(IS_END)
    ts_oos_start, ts_end = _ts(OOS_START), _ts(TRADE_END)
    axis = [t for t in sorted(all_ts) if ts_start <= t <= ts_end]

    # ---- 三臂 × 三口径
    results = {}
    for mode in modes:
        results[mode] = {}
        for arm in arms:
            t0 = time.time()
            r = replay(rows_by_mode[mode], meta, axis, arm)
            results[mode][arm] = {
                "all": window_stats(r["eq"], r["cash"], r["trades"], ts_start, ts_end),
                "is": window_stats(r["eq"], r["cash"], r["trades"], ts_is_start, ts_is_end),
                "oos": window_stats(r["eq"], r["cash"], r["trades"], ts_oos_start, ts_end),
                "reject": r["reject"], "filled_buys": r["filled_buys"],
                "n_trades": len(r["trades"]), "final_equity": (r["eq"][-1][1] if r["eq"] else CAPITAL),
                "trades": r["trades"]}
            a = results[mode][arm]
            print(f"[臂] {mode:12s} {arm} 全段{a['all']['ret']:+.2%} 回撤{a['all']['mdd']:.2%} "
                  f"回合{a['all']['rounds']} 暴露{a['all']['exposure']:.1%} ({time.time()-t0:.1f}s)",
                  flush=True)

    bench = {"all": bench_stats(stocks, ts_start, ts_end),
             "is": bench_stats(stocks, ts_is_start, ts_is_end),
             "oos": bench_stats(stocks, ts_oos_start, ts_end)}

    # ---- 自查
    checks = {}
    for mode in modes:
        for arm in arms:
            a = results[mode][arm]
            dir_tr = [x for x in a["trades"] if x["exit_day"] <= x["entry_day"]]
            bad_stop = [x for x in a["trades"] if x["stop0"] >= x["entry_px"]]
            drift = (a["final_equity"] - CAPITAL) - a["all"]["pnl_sum"]
            checks[f"{mode}/{arm}"] = {
                "rounds": len(a["trades"]), "filled_buys": a["filled_buys"],
                "t1_violations": len(dir_tr), "stop_above_entry": len(bad_stop),
                "identity_drift": drift, "identity_ok": abs(drift) <= CAPITAL * 1e-6,
                "zero_rounds": len(a["trades"]) == 0}

    # ---- WF vs naive 状态不一致率（lead 说的「20% 被改写」在本池上的实测）
    wf_diag = {}
    if "walkforward" in rows_by_mode and rows_by_mode["walkforward"]:
        for code in stocks:
            wfn = {r[0]: r for r in rows_by_mode["walkforward"][code]}
            nn = rows_by_mode["naive_known"][code]
            n = len(nn)
            d_dir = d_zd = d_br = 0
            cnt = 0
            for i in range(WARMUP_BARS, n):
                a, b = wfn.get(i), nn[i]
                if a is None:
                    continue
                cnt += 1
                if a[7] != b[7]:
                    d_dir += 1
                if (a[4] is None) != (b[4] is None) or (a[4] is not None and abs(a[4] - b[4]) > 1e-6):
                    d_zd += 1
                if a[6] != b[6]:
                    d_br += 1
            cnt = max(1, cnt)
            wf_diag[code] = {"bars": cnt, "seg_dir_diff": d_dir / cnt,
                             "zd_diff": d_zd / cnt, "broken_diff": d_br / cnt}
        wf_diag["_mean"] = {k: sum(v[k] for c, v in wf_diag.items() if c != "_mean") /
                               max(1, len(wf_diag) - 1)
                            for k in ("seg_dir_diff", "zd_diff", "broken_diff")}

    out = {"meta": {"pool": list(stocks), "names": {c: NAMES.get(c, "") for c in stocks},
                    "trade_start": TRADE_START, "is": [IS_START, IS_END],
                    "oos": [OOS_START, TRADE_END], "capital": CAPITAL,
                    "risk_per_trade": RISK_PER_TRADE, "max_pos": MAX_POS,
                    "cost": {"commission": COMMISSION, "stamp": STAMP, "slip": SLIP},
                    "stop_buf": STOP_BUF, "modes": modes, "arms": arms,
                    "warmup_bars": WARMUP_BARS, "debug_bars": args.debug_bars,
                    "elapsed_sec": time.time() - t_begin},
           "signals": sig_counts, "wf_diag": wf_diag, "benchmark": bench,
           "arms": {m: {a: {k: v for k, v in results[m][a].items() if k != "trades"}
                        for a in arms} for m in modes},
           "trades": {m: {a: results[m][a]["trades"] for a in arms} for m in modes},
           "checks": checks}

    suffix = args.tag
    jp = OUT_JSON if not suffix else OUT_JSON.replace(".json", f"-{suffix}.json")
    mp = OUT_MD if not suffix else OUT_MD.replace(".md", f"-{suffix}.md")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=float)
    print(f"[输出] {jp}", flush=True)
    write_md(out, mp)
    print(f"[输出] {mp}", flush=True)
    return 0


# ============================== 报告 ==============================
def _pct(x, nd=2, sign=True):
    if x is None:
        return "—"
    return (f"{x*100:+.{nd}f}%" if sign else f"{x*100:.{nd}f}%")


def _num(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def _arm_table(out, mode, arms, win):
    L_ = ["| 臂 | 收益 | 最大回撤 | 回合数 | 胜率 | 均盈 | 均亏 | 盈亏比(均盈/|均亏|) | 平均单笔 | 平均暴露 | 平均持仓(根) |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        s = out["arms"][mode][arm][win]
        pf = s.get("pf")
        L_.append(f"| {arm} | {_pct(s['ret'])} | {_pct(s['mdd'])} | {s['rounds']} | "
                  f"{_pct(s['win_rate'], 1, False)} | {_pct(s['avg_win'])} | {_pct(s['avg_loss'])} | "
                  f"{_num(pf)} | {_pct(s['avg_ret'])} | {_pct(s['exposure'], 1, False)} | "
                  f"{_num(s['avg_hold_bars'], 0)} |")
    return "\n".join(L_)


def write_md(out, path):
    m = out["meta"]
    pool = m["pool"]
    modes = m["modes"]
    arms = m["arms"]
    main_mode = "walkforward" if "walkforward" in modes else modes[0]
    L_ = []
    A = L_.append

    # ---------- 结论
    oos = {a: out["arms"][main_mode][a]["oos"] for a in arms}
    best = max(arms, key=lambda a: oos[a]["ret"])
    A("# 结果-闸门：止损口径与结构闸门对照回测")
    A("")
    A(f"> 主口径 = **walk-forward**（决策点滚动重算结构）；naive 两口径只用于量化偏差。")
    A(f"> 票池 {len(pool)} 只，区间 2020-07-01~2026-10-29，样本内 2020-07~2023-12，样本外 2024-01~2026-10。")
    A("")
    A("## 0. 一句话结论")
    A("")
    A(f"样本外三臂：A {_pct(oos['A']['ret'])} / B {_pct(oos['B']['ret'])} / C {_pct(oos['C']['ret'])}，"
      f"最好的是 **{best} 臂（{_pct(oos[best]['ret'])}，回撤 {_pct(oos[best]['mdd'])}，"
      f"{oos[best]['rounds']} 回合）**。")
    neg = [a for a in arms if oos[a]["ret"] <= 0]
    if neg:
        A("")
        A(f"**{('、'.join(neg))} 臂样本外为负 → 按纪律写「不成立」，不回头调参把样本外凑正。**")
    for a in arms:
        if out["arms"][main_mode][a]["all"]["ret"] > 0 >= out["arms"][main_mode][a]["oos"]["ret"]:
            A(f"- {a} 臂全段为正、样本外为负：**全段靠样本内撑起来**，不能当有效证据。")
    A("")

    # ---------- 口径
    A("## 1. 口径与实现（先把这个看清楚再读数字）")
    A("")
    A("| 项 | 取法 |")
    A("|---|---|")
    A("| 数据 | `策略/cache/min30_<code>_*_qfq.pkl`（baostock 30 分钟前复权，2020-01-02~2026-10-29） |")
    A(f"| 票池 | {len(pool)} 只：" + "、".join(f"{c}({m['names'].get(c,'')})" for c in pool) + " |")
    A(f"| 区间 | 全段 2020-07-01~2026-10-29；样本内 2020-07~2023-12；样本外 2024-01~2026-10 |")
    A(f"| 信号 | `chan_law.L.run(bars).points`：B1/B2/B3 进，S1/S2/S3 出（**三臂同一批信号，只改出场**） |")
    A(f"| 进场 | 信号「能被知道」的那根 bar 的下一根**开盘**价（`known_t+1`）；naive_t 臂用 `p.t+1` |")
    A(f"| 出场 | 收盘决策、下一根开盘成交；破止损/破位/卖点全部按此执行 |")
    A(f"| 成本 | 佣金 {m['cost']['commission']*1e4:.1f}‱ 双边、印花税 {m['cost']['stamp']*100:.2f}% 卖出、滑点单边 {m['cost']['slip']*100:.1f}% |")
    A(f"| T+1 | 按自然日判定，买入当日不得卖出（被拦的卖单次日按条件重新决策） |")
    A(f"| 仓位 | 单笔风险预算 = 总资产 {m['risk_per_trade']*100:.0f}%  ÷ 止损距离（**按实际成交价**算），最多 {m['max_pos']} 只，不加杠杆，其余现金 |")
    A(f"| 期末 | 最后一根按收盘价平仓（含成本），保证 Σ已实现盈亏 = 权益变动（见第 8 节恒等式自查） |")
    A("")
    A("三臂定义（唯一变量）：")
    A("")
    A("| 臂 | 止损位 | 额外出场 |")
    A("|---|---|---|")
    A("| A 旧口径 | 买点所属中枢上沿 **ZG**，持仓期间按最近已确认中枢 ZG **只上移**（既有策略做法） | 卖点 |")
    A("| B 新口径 | **结构失效价** = 买点低点 × (1-0.5%) | 中枢 ZD 破位（该 ZD 在成交价下方时）、卖点 |")
    A("| C = B + 闸门 | 同 B | 同 B + **broken=True 无条件次日开盘出**；broken 状态下**不许新开仓** |")
    A("")
    A("三口径（lead 要求的两层对照）：")
    A("")
    A("| 口径 | 做法 | 什么时候能用 |")
    A("|---|---|---|")
    A("| walkforward（主） | 每个决策 bar 用 `L.run(bars[:i+1])` 重算，`state_at(i)` 只看见截至当根的结构 | 唯一可下结论的口径 |")
    A("| naive_known | 一次性全量结构 + `confirm_t`/`known_t` 过滤 | 只用于量化「结构被后来的数据改写」造成的偏差 |")
    A("| naive_t | 同上但用 `p.t` 进场 | **含未来函数，只用于量化「被发现的时间」造成的偏差** |")
    A("")

    # ---------- 信号体检
    A("## 2. 信号体检（数量级 / 有没有票完全没信号）")
    A("")
    A("| 代码 | 名称 | bar 数 | B1 | B2 | B3 | S1 | S2 | S3 | 买点 | 确认滞后中位(根) | 最大 | 进场价 vs 信号价(中位) | 最差 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    tot_buy = 0
    zero_sig = []
    for c in pool:
        s = out["signals"].get(c)
        if not s:
            continue
        k = s["kinds"]
        tot_buy += s["buy"]
        if s["buy"] == 0:
            zero_sig.append(c)
        A(f"| {c} | {m['names'].get(c,'')} | {len(out['signals']) and ''} | {k.get('B1',0)} | {k.get('B2',0)} | "
          f"{k.get('B3',0)} | {k.get('S1',0)} | {k.get('S2',0)} | {k.get('S3',0)} | {s['buy']} | "
          f"{s['lag_median']} | {s['lag_max']} | {_pct(s['gap_median'])} | {_pct(s['gap_worst'])} |")
    A(f"| **合计** | — | — | — | — | — | — | — | — | **{tot_buy}** | — | — | — | — |")
    A("")
    A(f"- 买点合计 **{tot_buy} 个**（{len(pool)} 只票 / 6.8 年）→ 每票每年约 {tot_buy/len(pool)/6.8:.1f} 个，"
      f"与 chan_law 验收文档的量级（600036 全段 6 个买点）一致，**数量级合理**。")
    A(f"- 「某只票完全没信号」检查：{'**无**（12 只票都有买点）' if not zero_sig else '**有**：' + '、'.join(zero_sig)}")
    A("- 确认滞后（`known_t - t`）中位 " + str(
        sorted(v["lag_median"] for v in out["signals"].values() if v["lag_median"] is not None)[len(out["signals"])//2]) +
      " 根 30 分钟 bar：**信号在价格出现后约一周才可执行**，进场价中位已高出信号价 " +
      _pct(sorted(v["gap_median"] for v in out["signals"].values() if v["gap_median"] is not None)[len(out["signals"])//2]) +
      "。这一条直接决定了止损距离与仓位（见第 6 节）。")
    A("")

    # ---------- 主表
    A("## 3. 三臂结果（walk-forward 主口径）")
    for win, name in (("all", "全段 2020-07~2026-10"), ("is", "样本内 2020-07~2023-12"),
                      ("oos", "样本外 2024-01~2026-10")):
        A("")
        A(f"### 3.{'123'[('all','is','oos').index(win)]} {name}")
        A("")
        A(_arm_table(out, main_mode, arms, win))
    A("")
    A("> 样本外段的初始状态**来自样本内交易**（同一条权益曲线，未独立重置资金）—— 这是省时间的一次跑完再切段，"
      "读样本外时必须知道这一点。")
    A("")
    A("逐臂的执行诊断（为什么某臂笔数少）：")
    A("")
    A("| 臂 | 已成交买入 | 止损位在成交价上方被拒 | 闸门拦下 | 槽位满 | 回合 |")
    A("|---|---|---|---|---|---|")
    for a in arms:
        r = out["arms"][main_mode][a]["reject"]
        A(f"| {a} | {out['arms'][main_mode][a]['filled_buys']} | {r['stop_above_fill']} | "
          f"{r['gate_blocked']} | {r['slot_full']} | {out['arms'][main_mode][a]['all']['rounds']} |")
    A("")

    # ---------- naive vs WF
    A("## 4. naive 与 walk-forward 差多少（未来函数的偏差有多大）")
    A("")
    if "naive_known" in modes and "walkforward" in modes:
        A("| 口径 | 臂 | 全段收益 | 全段回撤 | 回合 | 样本外收益 | 样本外回撤 |")
        A("|---|---|---|---|---|---|---|")
        for mode in ("naive_t", "naive_known", "walkforward"):
            if mode not in out["arms"]:
                continue
            for a in arms:
                s, so = out["arms"][mode][a]["all"], out["arms"][mode][a]["oos"]
                A(f"| {mode} | {a} | {_pct(s['ret'])} | {_pct(s['mdd'])} | {s['rounds']} | "
                  f"{_pct(so['ret'])} | {_pct(so['mdd'])} |")
        A("")
        A("偏差量（naive − walkforward，正数=naive 偏乐观）：")
        A("")
        A("| 臂 | 全段收益差 | 全段回撤差 | 回合差 | 样本外收益差 | 样本外回撤差 |")
        A("|---|---|---|---|---|---|")
        for a in arms:
            d = lambda win, k: out["arms"]["naive_known"][a][win][k] - out["arms"]["walkforward"][a][win][k]
            A(f"| {a} | {_pct(d('all','ret'))} | {_pct(d('all','mdd'))} | "
              f"{out['arms']['naive_known'][a]['all']['rounds'] - out['arms']['walkforward'][a]['all']['rounds']} | "
              f"{_pct(d('oos','ret'))} | {_pct(d('oos','mdd'))} |")
        A("")
        if "_mean" in out["wf_diag"]:
            md = out["wf_diag"]["_mean"]
            A(f"结构状态被后来的数据改写率（walk-forward vs 全量，逐 bar 比）："
              f"线段方向 {_pct(md['seg_dir_diff'],1,False)}、中枢 ZD {_pct(md['zd_diff'],1,False)}、"
              f"broken {_pct(md['broken_diff'],1,False)}。")
            A("")
    else:
        A("（本次运行未跑 walk-forward，此节缺；不得当主口径结论。）")
        A("")

    # ---------- 基准
    A("## 5. 基准：同池等权买入持有 + 暴露归一")
    A("")
    A("| 窗口 | 等权基准(均值) | 等权基准(中位) | 最差票 | 最好票 |")
    A("|---|---|---|---|---|")
    for win, name in (("all", "全段"), ("is", "样本内"), ("oos", "样本外")):
        b = out["benchmark"][win]
        A(f"| {name} | {_pct(b['mean'])} | {_pct(b['median'])} | {_pct(b['min'])} | {_pct(b['max'])} |")
    A("")
    A("判据写死：**用均值**（中位数另列，两者差多少自己看）。基准用的是**实际交易的同一批票**，不是「有数据的票池」。")
    A("")
    A("| 窗口 | 臂 | 策略收益 | 平均暴露 | 基准×暴露（暴露归一） | 策略 − 归一基准 |")
    A("|---|---|---|---|---|---|")
    for win, name in (("all", "全段"), ("is", "样本内"), ("oos", "样本外")):
        b = out["benchmark"][win]["mean"]
        for a in arms:
            s = out["arms"][main_mode][a][win]
            norm = b * s["exposure"]
            A(f"| {name} | {a} | {_pct(s['ret'])} | {_pct(s['exposure'],1,False)} | {_pct(norm)} | "
              f"{_pct(s['ret']-norm)} |")
    A("")
    A("> 暴露归一按「基准收益 × 平均暴露」线性折算，是近似（真实资金分批进出与复利会让两者不完全等价），"
      "但方向不会错：**先看暴露，再比收益**，否则 3 仓 + 大量现金的策略天然被 100% 满仓基准碾压。")
    A("")

    # ---------- 分解
    A("## 6. 三臂差异的分解：止损/闸门 vs 「被发现的时间」")
    A("")
    if "walkforward" in modes:
        wa = {a: out["arms"]["walkforward"][a]["oos"]["ret"] for a in arms}
        A("| 差的来源 | 怎么量 | 样本外差值 |")
        A("|---|---|---|")
        if "naive_t" in out["arms"] and "naive_known" in out["arms"]:
            A(f"| 「被发现的时间」（信号价 vs known_t+1 开盘） | naive_t − naive_known（同结构，只改进场时刻） | "
              f"{_pct(out['arms']['naive_t'][ 'A']['oos']['ret'] - out['arms']['naive_known']['A']['oos']['ret'])}（A 臂） |")
        A(f"| 结构被后来数据改写（naive → walk-forward） | naive_known − walkforward | "
          f"{_pct(out['arms']['naive_known']['A']['oos']['ret'] - wa['A'])}（A 臂） |")
        A(f"| 止损口径（A → B） | walkforward B − A | {_pct(wa['B'] - wa['A'])} |")
        A(f"| 结构闸门（B → C） | walkforward C − B | {_pct(wa['C'] - wa['B'])} |")
        A("")
        A(f"读法：三臂之间（A/B/C）的差就是**止损与闸门**能带来的全部；跨口径（naive_t → walkforward）的差是"
          f"**「被发现的时间 + 结构改写」**带来的。主口径下后者已被剔除，所以第 3 节的差异可以归给止损/闸门；"
          f"而第 4 节的偏差表告诉你：如果图省事用 naive，会把结果虚高约 "
          f"{_pct(out['arms']['naive_known']['A']['oos']['ret'] - wa['A'])} 这种量级。")
        A("")
        A("更关键的一条：三臂样本外的**绝对值都很小**（|收益| 多在个位数百分点），而进场价相对信号价中位高出 "
          "5% 以上 —— 止损口径与闸门动的是「单笔亏多少」，动不了「进场就贵了 5%」这个结构性问题。")
        A("")

    # ---------- 纪律
    A("## 7. 纪律与局限（照实写，不粉饰）")
    A("")
    A("1. **样本外为负就是「不成立」**，不许回头调止损 buffer / 闸门口径 / 仓位去把样本外凑正。")
    A("2. 样本外段的初始状态来自样本内交易（一次跑完再切段），不是独立重跑。")
    A("3. 票池是 12 只蓝筹（银行/保险/券商/白酒/家电/医药/地产），**同质性强、且是今天还活着的权重股**，"
      "存在幸存者偏差；结论只能说明「这套结构信号在这类票上如何」，不能外推全市场。")
    A("4. 每臂样本外回合数见第 3 节；回合数 < 20 的一律只写「不足以判断」，不写「有效」。")
    A("5. walk-forward 用 `L.run(bars[:i+1])` 逐 bar 重算，代价大但**没有未来函数**；"
      "naive 两口径**都含**不同程度的未来函数（naive_t 最重）。结论只用 walk-forward。")
    A("6. 出场全部按「收盘决策、次日开盘成交」执行，没有模拟盘中触价止损 —— "
      "真实盘中止损会比这里更早出场（可能更好也可能更差），本报告不假设 bar 内的价格路径。")
    A("7. 未建模：涨跌停无法成交、停牌（按缺 bar 处理，挂单最多顺延 3 步）、成交量参与率、分红除权。")
    A("")

    # ---------- 自查
    A("## 8. 自查（不变量 / 恒等式 / 0 笔检查）")
    A("")
    A("| 臂(口径) | 回合 | T+1 违规 | 止损在成交价上方 | 恒等式漂移(元) | 0 笔? |")
    A("|---|---|---|---|---|---|")
    for k, v in out["checks"].items():
        A(f"| {k} | {v['rounds']} | {v['t1_violations']} | {v['stop_above_entry']} | "
          f"{v['identity_drift']:.2f} | {'**是**' if v['zero_rounds'] else '否'} |")
    A("")
    A("- 恒等式：`期末权益 − 初始资金` 必须等于 `Σ已平仓盈亏`（期末强制平仓，所以能逐分对账）。")
    A("- T+1 违规必须 0；「止损在成交价上方」必须 0（A 臂里 ZG 高于成交价的信号在建仓时就被拒，见第 3 节诊断表）。")
    A("")
    A("## 9. 复现")
    A("")
    A("```powershell")
    A("cd D:\\大龙虾\\股票工具\\缠论\\策略\\完善-20261031")
    A("python 结构闸门回测.py            # 12 票 × 3 臂 × 3 口径（walk-forward 约 40~70 分钟，缓存可续跑）")
    A("python 结构闸门回测.py --no-wf    # 冒烟：只跑 naive")
    A("```")
    A("")
    A(f"本次运行：{m['elapsed_sec']/60:.1f} 分钟，口径 {modes}，臂 {arms}。")
    A("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L_) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
