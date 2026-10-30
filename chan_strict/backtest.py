"""
严格版回测（30 分钟操作级别）
=============================
规矩：
① 每根 30 分钟 K 线收盘时算结构，**下一根开盘**成交（不许用当根收盘价成交）；
② 喂给结构引擎的数据必须截至当前 bar，代码里 assert 卡死；
③ A 股 T+1：当日买入当日不可卖（按自然日判断）；
④ 费用：佣金双边万 2.5、卖出印花税 0.05%、滑点单边 0.1%；
⑤ 大级别过滤用日线走势类型，按**当日之前**的日线数据算，不许偷看。

用法：
    python -m chan_strict.backtest --codes 600036,600519 --start 2024-01-01 --end 2026-10-29
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

from . import pipeline as PL
from . import strategy as ST

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "out")
COMMISSION = 0.00025
STAMP = 0.0005
SLIP = 0.001


def _analyze(df: pd.DataFrame) -> dict:
    return PL.analyze(df[["open", "high", "low", "close"]], max_level=1)


def _daily_points(daily: pd.DataFrame, window_days: int = 30) -> list:
    """日线级别买卖点的日期列表（区间套的“大级别点”）。"""
    out = []
    ohlc = daily[["open", "high", "low", "close"]]
    for i in range(250, len(daily)):
        try:
            res = _analyze(ohlc.iloc[: i + 1].reset_index(drop=True))
        except Exception:
            continue
        for p in res.get("points", []):
            out.append((pd.Timestamp(daily["date"].iloc[i]).normalize(), p["kind"]))
    return out


def _daily_trends(daily: pd.DataFrame) -> dict:
    """每个交易日收盘时的日线走势类型（只用截至当日的数据）。"""
    out = {}
    closes = daily["close"].to_numpy(float)
    ohlc = daily[["open", "high", "low", "close"]]
    for i in range(len(daily)):
        if i < 250:
            continue
        try:
            res = _analyze(ohlc.iloc[: i + 1].reset_index(drop=True))
        except Exception:
            continue
        out[pd.Timestamp(daily["date"].iloc[i]).normalize()] = ST.market_filter(res)
    return out


def run(codes: list[str], start: str, end: str, capital: float = 1_000_000.0,
        window: int = 2000, cfg: dict | None = None, verbose: bool = False,
        trade_start: str | None = None, freq: str = "30") -> dict:
    import chan_data

    cfg = {**ST.DEFAULT_CFG, **(cfg or {})}
    bars: dict[str, pd.DataFrame] = {}
    daily: dict[str, pd.DataFrame] = {}
    trends: dict[str, dict] = {}
    daily_pts: dict[str, list] = {}

    for c in codes:
        try:
            if str(freq) == "5":
                import chan_min5
                m = chan_min5.get_min5(c, start, end)
            else:
                m = chan_data.get_min30(c, start, end)
            d = chan_data.get_daily(c, "2020-01-01", end)
        except Exception as e:  # noqa: BLE001
            print(f"[跳过] {c}: {e}")
            continue
        if m is None or len(m) < 500:
            print(f"[跳过] {c}: 30 分钟数据不足")
            continue
        m = m.copy()
        m["date"] = pd.to_datetime(m["date"])
        m = m.sort_values("date").reset_index(drop=True)
        bars[c] = m
        daily[c] = d.copy()
        daily[c]["date"] = pd.to_datetime(daily[c]["date"])
        trends[c] = _daily_trends(daily[c])
        daily_pts[c] = _daily_points(daily[c]) if cfg.get("require_daily_parent") else []
        print(f"[载入] {c} 30分钟 {len(m)} 根，日线 {len(d)} 根", flush=True)
    if not bars:
        raise RuntimeError("没有可用数据")

    # 统一时间轴（各票 30 分钟时间戳一致）
    axis = sorted(set().union(*[set(b["date"]) for b in bars.values()]))
    pos_of = {c: {t: i for i, t in enumerate(b["date"])} for c, b in bars.items()}

    port = {"cash": capital, "positions": {}, "trades": []}
    fired: set = set()
    seen: set = set()
    last_exit_bar = {c: -10 ** 6 for c in bars}   # 负数才能让 ti - init 是个大正数（曾被写成 10**6，导致永久不买）
    pending: list[dict] = []
    eq_rows = []
    n_done = 0

    t_start = pd.Timestamp(trade_start) if trade_start else None
    for ti, t in enumerate(axis):
        if t_start is not None and t < t_start:
            # 样本外验证：交易起点之前只养结构，不下单也不决策
            eq0 = port["cash"] + sum(
                p["shares"] * float(bars[c]["close"].iloc[min(pos_of[c].get(t, 0), len(bars[c]) - 1)])
                for c, p in port["positions"].items()
            )
            eq_rows.append({"date": t, "equity": eq0})
            continue
        # 1) 开盘执行上一根产生的委托
        port["_total_asset"] = port["cash"] + sum(
            p["shares"] * float(bars[c]["open"].iloc[min(pos_of[c].get(t, 0), len(bars[c]) - 1)])
            for c, p in port["positions"].items()
        )
        still: list[dict] = []
        for od in pending:
            c = od["code"]
            i = pos_of[c].get(t)
            if i is None:
                still.append(od)
                continue
            act = od["act"]
            if act["action"] == "BUY":
                lim = od.get("limit_px")
                if lim is None:
                    px = float(bars[c]["open"].iloc[i]) * (1 + SLIP)
                    _apply(port, act, px, t, c, cfg)
                    continue
                if ti > od["expire_ti"]:
                    continue                              # 限价单过期作废
                if float(bars[c]["low"].iloc[i]) > lim:
                    still.append(od)                      # 没回踩到买点附近，继续等
                    continue
                px = min(float(bars[c]["open"].iloc[i]), lim) * (1 + SLIP)
                _apply(port, act, px, t, c, cfg)
            else:
                pos = port["positions"].get(c)
                if pos and pos.get("entry_day") == t.normalize():
                    still.append(od)                      # T+1：当日买入不可卖，明天再发
                    continue
                px = float(bars[c]["open"].iloc[i]) * (1 - SLIP)
                _apply(port, act, px, t, c, cfg)
        pending = still

        # 2) 收盘：算结构、决策
        total_asset = port["cash"] + sum(
            p["shares"] * float(bars[c]["close"].iloc[min(pos_of[c].get(t, 0), len(bars[c]) - 1)])
            for c, p in port["positions"].items()
        )
        for c, b in bars.items():
            i = pos_of[c].get(t)
            if i is None:
                continue
            lo = max(0, i + 1 - window)
            win = b.iloc[lo : i + 1]
            if len(win) < 300:
                continue
            assert win["date"].max() == t, "未来函数：喂进结构引擎的数据超过了当前 bar"
            try:
                res = _analyze(win.reset_index(drop=True))
            except Exception:
                continue
            res["n_raw"] = len(win)
            pos = port["positions"].get(c)
            day = t.normalize()
            daily_t = None
            for dt in sorted(trends[c].keys(), reverse=True):
                if dt <= day:
                    daily_t = trends[c][dt]
                    break
            state = {
                "fired": fired,
                "seen": seen,
                "bar_offset": lo,
                "daily_trend": daily_t or {"type": "无", "complete": False},
                "n_positions": len(port["positions"]),
                "total_asset": total_asset,
                "bars_since_exit": ti - last_exit_bar[c],
                "daily_parent_ok": any(
                    (day - dd).days <= 30 and kk.startswith("B")
                    for dd, kk in daily_pts[c]
                ) if daily_pts.get(c) else False,
            }
            acts = ST.decide(pos, res, state, cfg)
            for a in acts:
                if a.get("mark"):
                    fired.add(a["mark"])
                if a["action"] in ("SELL", "REDUCE"):
                    last_exit_bar[c] = ti
                pending.append({"code": c, "act": a, "limit_px": a.get("limit_px"),
                                "expire_ti": ti + int(cfg.get("limit_valid_bars", 8))})
                if verbose:
                    print(f"  {t} {c} {a['action']} {a.get('weight', a.get('qty'))} | {a['reason'][:60]}", flush=True)
            if acts:
                break

        # 3) 估值
        eq = port["cash"]
        for c, p in port["positions"].items():
            i = pos_of[c].get(t)
            if i is not None:
                eq += p["shares"] * float(bars[c]["close"].iloc[i])
        eq_rows.append({"date": t, "equity": eq})
        n_done += 1
        if n_done % 500 == 0:
            print(f"  进度 {n_done}/{len(axis)} 权益 {eq:,.0f}", flush=True)

    # 收尾平仓
    last_t = axis[-1]
    for c in list(port["positions"].keys()):
        i = pos_of[c].get(last_t)
        if i is not None:
            _apply(port, {"action": "SELL", "qty": "all"}, float(bars[c]["close"].iloc[i]), last_t, c, cfg)

    eqdf = pd.DataFrame(eq_rows)
    eqdf["ret"] = eqdf["equity"].pct_change().fillna(0)
    trades = pd.DataFrame(port["trades"])
    return {"equity": eqdf, "trades": trades, "stats": _stats(eqdf, trades, capital, start, end)}


def _apply(port: dict, act: dict, px: float, t, code: str, cfg: dict) -> None:
    pos = port["positions"].get(code)
    if act["action"] == "BUY":
        amount = min((port.get("_total_asset") or port["cash"]) * float(act.get("weight", 0.1)),
                     port["cash"] * (1 - cfg["reserve_cash"]))
        shares = int(amount / px / 100) * 100
        if shares <= 0:
            return
        cost = shares * px
        port["cash"] -= cost * (1 + COMMISSION)
        port["positions"][code] = {
            "shares": shares, "cost": px, "stop": float(act.get("stop") or px * 0.95),
            "entry_day": t.normalize(), "entry_ts": t, "entry_kind": act.get("entry_kind"),
        }
        port["trades"].append({"date": t, "code": code, "side": "buy", "price": px,
                               "shares": shares, "reason": act.get("reason", "")})
    elif act["action"] in ("SELL", "REDUCE"):
        if not pos:
            return
        ratio = 1.0 if act["qty"] == "all" else float(act["qty"])
        shares = pos["shares"] if ratio >= 1 else int(pos["shares"] * ratio / 100) * 100
        shares = min(shares, pos["shares"])
        if shares <= 0:
            return
        proceeds = shares * px
        port["cash"] += proceeds * (1 - COMMISSION - STAMP)
        pos["shares"] -= shares
        port["trades"].append({"date": t, "code": code, "side": "sell", "price": px,
                               "shares": shares, "reason": act.get("reason", "")})
        if pos["shares"] <= 0:
            port["positions"].pop(code, None)


def _stats(eq: pd.DataFrame, trades: pd.DataFrame, capital: float, start: str, end: str) -> dict:
    if eq.empty:
        return {}
    final = float(eq["equity"].iloc[-1])
    total = final / capital - 1
    days = max((eq["date"].iloc[-1] - eq["date"].iloc[0]).days, 1)
    annual = (1 + total) ** (365.0 / days) - 1
    dd = (eq["equity"] / eq["equity"].cummax() - 1).min()

    wins, losses = [], []
    lots: dict[str, list] = {}
    if not trades.empty:
        for _, r in trades.sort_values("date").iterrows():
            c = r["code"]
            if r["side"] == "buy":
                lots.setdefault(c, []).append([int(r["shares"]), float(r["price"])])
            else:
                remain, px_out = int(r["shares"]), float(r["price"])
                while remain > 0 and lots.get(c):
                    lot = lots[c][0]
                    take = min(remain, lot[0])
                    pnl = (px_out - lot[1]) / lot[1] - 2 * COMMISSION - STAMP
                    (wins if pnl > 0 else losses).append(pnl)
                    lot[0] -= take
                    remain -= take
                    if lot[0] <= 0:
                        lots[c].pop(0)

    eq = eq.copy()
    eq["year"] = pd.to_datetime(eq["date"]).dt.year
    yearly = eq.groupby("year")["equity"].agg(["first", "last"])
    yearly["ret"] = yearly["last"] / yearly["first"] - 1
    return {
        "区间": f"{start} ~ {end}",
        "初始资金": round(capital, 0),
        "期末权益": round(final, 0),
        "总收益": f"{total*100:.2f}%",
        "年化": f"{annual*100:.2f}%",
        "最大回撤": f"{dd*100:.2f}%",
        "成交笔数": int(len(trades)),
        "胜率": f"{(len(wins)/max(len(wins)+len(losses),1))*100:.1f}% ({len(wins)}胜{len(losses)}负)",
        "平均盈利": f"{(np.mean(wins)*100 if wins else 0):.2f}%",
        "平均亏损": f"{(np.mean(losses)*100 if losses else 0):.2f}%",
        "分年度": {int(y): f"{r*100:.1f}%" for y, r in yearly["ret"].items()},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--window", type=int, default=2000)
    ap.add_argument("--out", default="strict_bt")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--trade-start", default="", help="样本外：该日期之前只养结构不交易")
    ap.add_argument("--freq", default="30", choices=["5", "30"], help="操作级别：5 或 30 分钟")
    a = ap.parse_args()

    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    res = run(codes, a.start, a.end, a.capital, window=a.window, verbose=a.verbose,
              trade_start=a.trade_start or None, freq=a.freq)
    print("\n===== 严格版回测结果 =====")
    for k, v in res["stats"].items():
        print(f"{k}: {v}")
    os.makedirs(OUT, exist_ok=True)
    res["equity"].to_csv(os.path.join(OUT, f"{a.out}_equity.csv"), index=False, encoding="utf-8-sig")
    res["trades"].to_csv(os.path.join(OUT, f"{a.out}_trades.csv"), index=False, encoding="utf-8-sig")
    with open(os.path.join(OUT, f"{a.out}_stats.json"), "w", encoding="utf-8") as f:
        json.dump(res["stats"], f, ensure_ascii=False, indent=2)
    print(f"\n明细已写入 {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
