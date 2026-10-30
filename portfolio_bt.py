"""
组合回测器（执行层，只吃信号表）
================================
输入：信号表（signal_gen.py 产出）+ 行情（按需从缓存读）。
输出：资金曲线与统计。**不含任何结构计算**——结构已经在信号层算完了。

这样做的好处：
  · 换仓位/风控/限价规则，几秒出结果，不用重跑结构；
  · 回测代码短、可审计，不会和结构逻辑纠缠。

成交规则（A 股现实约束，与原文无关）：
  · 信号当根收盘产生 → 下一根开盘执行；
  · 买入用限价：只在价格回踩到 买点价×(1+slack) 以内成交，超时作废（41 课不追高）；
  · T+1：当日买入当日不可卖；卖出用市价（下一根开盘）；
  · 费用：佣金万 2.5 双边、卖出印花税 0.05%、滑点单边 0.1%；
  · 仓位：按风险预算（每笔最多亏总资产 risk_per_trade），单票上限、同时持仓上限；
  · 止损：跌破信号自带的结构失效价（13 课）。

用法：
  python portfolio_bt.py --signals signals/signals_30min_w3000.csv --freq 30 \
      --start 2024-01-01 --end 2026-10-29 --trade-start 2024-07-01 --split 2025-09-01
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import chan_data

OUT = os.path.join(HERE, "out")
COMMISSION, STAMP, SLIP = 0.00025, 0.0005, 0.001

DEFAULT_CFG = {
    "max_positions": 3,
    "max_weight_per_stock": 0.30,
    "risk_per_trade": 0.01,
    "reserve_cash": 0.10,
    "entry_slack": 0.005,
    "limit_valid_bars": 8,
    "stop_buffer": 0.005,
    "require_daily_up": False,
    # 结构跟踪止损（第 13 课「当下走势能不能搞」）：持仓期间若同票出现新高买点，
    # 且其结构失效位高于当前止损位，则把止损上移（只上移不下移）。
    "trailing_stop": True,
    # 跟踪止损模式：
    #   "signal" = 用新买点自带的失效位（稀疏，实测 180 笔只触发 11 次）
    #   "zs"     = 用「最新中枢上沿 ZG」逐 bar 抬高（需要 signals/zs_series_20stk.csv）
    #   "off"    = 关闭
    "trailing_mode": "zs",
    # 大盘状态过滤：只在指数在 MA 上方时开新仓（顺势），不改变卖出逻辑
    "regime_ma": 0,          # 0 = 关闭；例如 60 表示"上证收盘 > MA60 才开新仓"
    "regime_index": "000001",
    # 时间退出：持仓超过 N 根 bar 强制退出（0 = 关闭）
    "time_exit_bars": 0,
    # 第 20 课：「一旦不能出现趋势，一定要在盘整的高点出掉，这和第一、二类买点的策略是一样的」。
    # 操作化：持仓后若出现**新的中枢**（说明进入盘整/更大级别盘整），
    # 且价格触及该中枢上沿 ZG（盘整的高点区域）→ 卖出。
    "panzheng_exit": True,
    # 第 80 课：「一旦出现顶分型，离开就是唯一的选择」
    "exit_fractal_top": False,
    # 第 86 课：「在下一个次级别不破第一个次级别上去的高点，就坚决卖掉」
    "exit_no_new_high": False,
    "exit_series": "signals/exit_series_20stk.csv",
    # 第 81 课：「没有顶分型就可以持有睡觉」——即不被小级别结构止损洗出。
    # 打开后不再用「破结构失效价」退出，只靠卖点 / 分型 / 不破前高。
    "no_struct_stop": False,
    "zs_series": "signals/zs_series_20stk.csv",
}


def load_bars(code: str, freq: str, start: str, end: str) -> pd.DataFrame | None:
    if str(freq) == "5":
        import chan_min5
        d = chan_min5.get_min5(code, start, end)
    else:
        d = chan_data.get_min30(code, start, end)
    if d is None:
        return None
    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    return d.sort_values("date").reset_index(drop=True)


def run(sig: pd.DataFrame, freq: str, start: str, end: str, capital: float = 1_000_000.0,
        cfg: dict | None = None, trade_start: str | None = None, verbose: bool = False) -> dict:
    c = {**DEFAULT_CFG, **(cfg or {})}
    sig = sig.copy()
    sig["ts"] = pd.to_datetime(sig["ts"])
    # 关键：code 必须按字符串处理并补零。CSV 读回来 "000001" 会被解析成数字 1，
    # 取数就会报"无法识别的股票代码 1"，那只票被静默丢掉（实测 6 只票只跑了 5 只）。
    sig["code"] = sig["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    codes = sorted(sig["code"].unique())
    bars = {}
    for code in codes:
        b = load_bars(code, freq, start, end)
        if b is None or len(b) < 100:
            continue
        bars[code] = b
    if not bars:
        raise RuntimeError("没有可用行情")

    # 载入指数与 MA（大盘状态过滤用）
    regime = None
    if c.get("regime_ma"):
        try:
            idx = chan_data.get_index(c.get("regime_index", "000001"), start, end)
            idx = idx.copy()
            idx["ma"] = idx["close"].rolling(int(c["regime_ma"])).mean()
            regime = {r["date"]: (float(r["close"]), None if pd.isna(r["ma"]) else float(r["ma"]))
                      for _, r in idx.iterrows()}
            print(f"[大盘过滤] MA{c['regime_ma']} 载入 {len(regime)} 天", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[提示] 大盘过滤载入失败：{e}", flush=True)

    # 载入分型/笔退出序列
    ex_series = {}
    epath = c.get("exit_series") or ""
    if (c.get("exit_fractal_top") or c.get("exit_no_new_high")) and epath:
        ep = epath if os.path.isabs(epath) else os.path.join(HERE, epath)
        if os.path.exists(ep):
            edf = pd.read_csv(ep, dtype={"code": str})
            edf["code"] = edf["code"].str.zfill(6)
            edf["ts"] = pd.to_datetime(edf["ts"])
            for code, g in edf.groupby("code"):
                g = g.sort_values("ts").reset_index(drop=True)
                prev = g["no_new_high"].shift(1).fillna(0)
                g["nnh_evt"] = ((g["no_new_high"] > 0) & (prev == 0)).astype(int)
                ex_series[code] = {"ts": list(g["ts"]), "top": list(g["top_conf"]),
                                   "nnh": list(g["nnh_evt"])}
            print(f"[退出序列] 载入 {len(ex_series)} 只票", flush=True)
        else:
            print(f"[提示] 没找到退出序列 {ep}", flush=True)

    # 载入中枢序列（逐 bar 跟踪止损用）
    zs_series = {}
    zpath = c.get("zs_series") or ""
    if zpath:
        zp = zpath if os.path.isabs(zpath) else os.path.join(HERE, zpath)
        if os.path.exists(zp):
            zdf = pd.read_csv(zp, dtype={"code": str})
            zdf["code"] = zdf["code"].str.zfill(6)
            zdf["ts"] = pd.to_datetime(zdf["ts"])
            for code, g in zdf.groupby("code"):
                g = g.sort_values("ts")
                zs_series[code] = {"ts": list(g["ts"]),
                                   "zg": [None if pd.isna(v) else float(v) for v in g["ZG"]],
                                   "n_zs": [int(v) if not pd.isna(v) else None for v in g["n_zs"]]}
            print(f"[中枢序列] 载入 {len(zs_series)} 只票，共 {len(zdf)} 个采样点", flush=True)
        else:
            print(f"[提示] 没找到中枢序列 {zp}，跟踪止损退回 signal 模式", flush=True)

    axis = sorted(set().union(*[set(b["date"]) for b in bars.values()]))
    pos_of = {k: {t: i for i, t in enumerate(v["date"])} for k, v in bars.items()}

    # 信号按时间索引：信号在 bar i 收盘产生，下一根开盘可成交
    by_time: dict = {}
    for _, r in sig.iterrows():
        by_time.setdefault(r["ts"], []).append(r)

    port = {"cash": capital, "positions": {}, "trades": []}
    pending: list[dict] = []
    last_exit = {k: -10 ** 6 for k in bars}
    # 估值用的最新价：某只票当天没有 bar（停牌）时，必须沿用上一个已知价，
    # 不能用 pos_of.get(t, 0) —— 那个默认值会拿序列第一根的价格来估值，
    # 停牌期间权益虚高、复牌瞬间暴跳（实测某根 K 线"亏"掉 30% 权益就是它）。
    last_px: dict = {}
    eq_rows = []
    t_start = pd.Timestamp(trade_start) if trade_start else None

    for ti, t in enumerate(axis):
        # ---------- 执行挂单
        still = []
        for od in pending:
            code, act = od["code"], od["act"]
            i = pos_of[code].get(t)
            if i is None:
                still.append(od)
                continue
            if act["side"] == "buy":
                # 防御：若此刻价格已经在止损位下方，这笔买入不成立（否则一进场就该止损）
                if od.get("stop") and float(bars[code]["open"].iloc[i]) <= float(od["stop"]):
                    _log_skip(code, t, od, "开盘价已跌破止损位")
                    continue
                if ti > od["expire"]:
                    continue
                lim = od["limit"]
                if float(bars[code]["low"].iloc[i]) > lim:
                    still.append(od)
                    continue
                px = min(float(bars[code]["open"].iloc[i]), lim) * (1 + SLIP)
                _buy(port, code, px, t, act, c)
            else:
                pos = port["positions"].get(code)
                if pos and pos["entry_day"] == t.normalize():
                    still.append(od)
                    continue
                px = float(bars[code]["open"].iloc[i]) * (1 - SLIP)
                _sell(port, code, px, t, act, c)
                last_exit[code] = ti
        pending = still

        for _c, _b in bars.items():
            _i = pos_of[_c].get(t)
            if _i is not None:
                last_px[_c] = float(_b["close"].iloc[_i])

        if t_start is not None and t < t_start:
            eq_rows.append({"date": t, "equity": port["cash"] + sum(
                pp["shares"] * last_px.get(k, pp["cost"]) for k, pp in port["positions"].items())})
            continue

        # ---------- 结构跟踪止损
        mode = "off" if not c.get("trailing_stop") else c.get("trailing_mode", "signal")
        if mode == "signal":
            import trailing as TR
            sigs_here = [r for r in by_time.get(t, []) if str(r["kind"]).startswith("B")]
            for ch in TR.track(port["positions"], sigs_here):
                _trail_log(ch)
        elif mode == "zs" and zs_series:
            import bisect
            for code, p in list(port["positions"].items()):
                ser = zs_series.get(code)
                if not ser:
                    continue
                idx = bisect.bisect_right(ser["ts"], t) - 1
                if idx < 0:
                    continue
                zg = ser["zg"][idx]
                if zg is None or zg != zg or zg <= 0:      # zg != zg 用来排除 NaN
                    continue
                cur = float(p.get("stop") or 0)
                if zg > cur:
                    p["stop"] = float(zg)
                    _trail_log({"code": code, "old": cur, "new": float(zg),
                                "kind": "ZS", "ts": t})

        # ---------- 第 80/86 课的结构退出
        if ex_series and (c.get("exit_fractal_top") or c.get("exit_no_new_high")):
            import bisect as _bs3
            for code, p in list(port["positions"].items()):
                ser = ex_series.get(code)
                if not ser:
                    continue
                j = _bs3.bisect_right(ser["ts"], t) - 1
                if j < 0:
                    continue
                if c.get("exit_fractal_top") and ser["top"][j]:
                    pending.append({"code": code,
                                    "act": {"side": "sell", "reason": "顶分型离开（80课）", "qty": "all"},
                                    "expire": ti + 1, "limit": None})
                    continue
                if c.get("exit_no_new_high") and ser["nnh"][j]:
                    pending.append({"code": code,
                                    "act": {"side": "sell", "reason": "次级别不破前高（86课）", "qty": "all"},
                                    "expire": ti + 1, "limit": None})

        # ---------- 盘整高点出掉（第 20 课）
        if c.get("panzheng_exit") and zs_series:
            import bisect as _bs2
            for code, p in list(port["positions"].items()):
                ser = zs_series.get(code)
                if not ser:
                    continue
                j = _bs2.bisect_right(ser["ts"], t) - 1
                if j < 0:
                    continue
                n_zs_now = ser["n_zs"][j]
                zg_now = ser["zg"][j]
                entry_n = p.get("entry_n_zs")
                i = pos_of[code].get(t)
                if i is None or zg_now is None or entry_n is None or n_zs_now is None:
                    continue
                # 出现新中枢（进入盘整）且价格回到该中枢上沿 → 出掉
                if n_zs_now > int(entry_n) and float(bars[code]["close"].iloc[i]) >= float(zg_now):
                    pending.append({"code": code,
                                    "act": {"side": "sell", "reason": "盘整高点出掉（20课）", "qty": "all"},
                                    "expire": ti + 1, "limit": None})

        # ---------- 时间退出（用建仓时间反查 bar 下标，避免改 _buy 的签名）
        if c.get("time_exit_bars"):
            for code, p in list(port["positions"].items()):
                ets = p.get("entry_ts")
                ei = pos_of[code].get(ets) if ets is not None else None
                if ei is not None and (ti - ei) >= int(c["time_exit_bars"]):
                    pending.append({"code": code,
                                    "act": {"side": "sell",
                                            "reason": f"{c['time_exit_bars']}根时间退出", "qty": "all"},
                                    "expire": ti + 1, "limit": None})

        # ---------- 风控：结构止损（第 81 课口径下可关闭）
        for code, p in ([] if c.get("no_struct_stop") else list(port["positions"].items())):
            i = pos_of[code].get(t)
            if i is None:
                continue
            close = float(bars[code]["close"].iloc[i])
            if close < p["stop"] * (1 - c["stop_buffer"]):
                pending.append({"code": code, "act": {"side": "sell", "reason": "破结构失效价", "qty": "all"},
                                "expire": ti + 1, "limit": None})

        # ---------- 信号 → 挂单
        total = port["cash"] + sum(pp["shares"] * last_px.get(k, pp["cost"])
                                   for k, pp in port["positions"].items())
        for r in by_time.get(t, []):
            code = r["code"]
            # 当前（截至本 bar）该票已形成的中枢个数，供「盘整高点出掉」用
            cur_n_zs = None
            if zs_series:
                ser0 = zs_series.get(code)
                if ser0:
                    import bisect as _bs
                    _j = _bs.bisect_right(ser0["ts"], t) - 1
                    if _j >= 0:
                        cur_n_zs = ser0["n_zs"][_j]
            if code not in bars:
                continue
            if r["kind"].startswith("S"):
                if code in port["positions"]:
                    pending.append({"code": code, "act": {"side": "sell", "reason": f"{r['kind']} 卖点", "qty": "all"},
                                    "expire": ti + 1, "limit": None})
                continue
            # 买点
            if regime is not None:
                rr = regime.get(pd.Timestamp(t).normalize())
                if rr is None or rr[1] is None or not (rr[0] > rr[1]):
                    continue          # 指数在 MA 下方 → 不开新仓
            if code in port["positions"] or len(port["positions"]) >= c["max_positions"]:
                continue
            # 同一根 bar 可能同时冒出多个买点（B1+B2），必须只挂一张单：
            # 否则两笔都成交、仓位被后一笔覆盖，等于白付一笔钱（实测一根 K 线"亏"30% 就是这个）。
            if any(o["code"] == code and o["act"]["side"] == "buy" for o in pending):
                continue
            if ti - last_exit.get(code, -10 ** 6) < 8:
                continue
            lim = float(r["px"]) * (1 + c["entry_slack"])
            stop = float(r["stop"])
            risk_frac = max((lim - stop) / lim, 1e-6)
            w = min(c["max_weight_per_stock"], c["risk_per_trade"] / risk_frac)
            amount = min(total * w, port["cash"] * (1 - c["reserve_cash"]))
            if amount < 1000:
                continue
            pending.append({"code": code, "stop": stop,
                            "act": {"side": "buy", "weight": w, "stop": stop, "n_zs": cur_n_zs,
                                                  "amount": amount, "reason": f"{r['kind']} 买点", "kind": r["kind"]},
                            "expire": ti + int(c["limit_valid_bars"]), "limit": lim})
            if verbose:
                print(f"  {t} {code} 挂买 {r['kind']} 限价{lim:.2f} 止损{stop:.2f} 仓位{w*100:.1f}%")

        eq = port["cash"] + sum(pp["shares"] * last_px.get(k, pp["cost"])
                                for k, pp in port["positions"].items())
        eq_rows.append({"date": t, "equity": eq})

    # 收尾平仓
    last_t = axis[-1]
    for code in list(port["positions"]):
        i = pos_of[code].get(last_t)
        if i is not None:
            _sell(port, code, float(bars[code]["close"].iloc[i]), last_t,
                  {"side": "sell", "reason": "期末平仓", "qty": "all"}, c)

    eqdf = pd.DataFrame(eq_rows)
    eqdf["ret"] = eqdf["equity"].pct_change().fillna(0)
    trades = pd.DataFrame(port["trades"])
    return {"equity": eqdf, "trades": trades, "stats": _stats(eqdf, trades, capital, start, end)}


def _trail_log(ch: dict) -> None:
    if not hasattr(_trail_log, "rows"):
        _trail_log.rows = []
    _trail_log.rows.append(ch)


def _log_skip(code: str, t, od: dict, why: str) -> None:
    if not hasattr(_log_skip, "rows"):
        _log_skip.rows = []
    _log_skip.rows.append({"date": t, "code": code, "why": why, "stop": od.get("stop")})


def _buy(port, code, px, t, act, cfg):
    if code in port["positions"]:      # 防御：已有持仓不再买（不加码）
        return
    shares = int(act["amount"] / px / 100) * 100
    if shares <= 0:
        return
    cost = shares * px
    if cost * (1 + COMMISSION) > port["cash"]:
        shares = int(port["cash"] / (px * (1 + COMMISSION)) / 100) * 100
        if shares <= 0:
            return
        cost = shares * px
    port["cash"] -= cost * (1 + COMMISSION)
    port["positions"][code] = {"shares": shares, "cost": px, "stop": act["stop"],
                               "entry_day": t.normalize(), "entry_ts": t, "kind": act.get("kind"),
                               "entry_n_zs": act.get("n_zs")}
    port["trades"].append({"date": t, "code": code, "side": "buy", "price": px,
                           "shares": shares, "reason": act.get("reason", "")})


def _sell(port, code, px, t, act, cfg):
    p = port["positions"].get(code)
    if not p:
        return
    shares = p["shares"]
    port["cash"] += shares * px * (1 - COMMISSION - STAMP)
    port["trades"].append({"date": t, "code": code, "side": "sell", "price": px,
                           "shares": shares, "reason": act.get("reason", "")})
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
            cc = r["code"]
            if r["side"] == "buy":
                lots.setdefault(cc, []).append([int(r["shares"]), float(r["price"])])
            else:
                remain, px_out = int(r["shares"]), float(r["price"])
                while remain > 0 and lots.get(cc):
                    lot = lots[cc][0]
                    take = min(remain, lot[0])
                    pnl = (px_out - lot[1]) / lot[1] - 2 * COMMISSION - STAMP
                    (wins if pnl > 0 else losses).append(pnl)
                    lot[0] -= take
                    remain -= take
                    if lot[0] <= 0:
                        lots[cc].pop(0)
    eq2 = eq.copy()
    eq2["year"] = pd.to_datetime(eq2["date"]).dt.year
    yearly = eq2.groupby("year")["equity"].agg(["first", "last"])
    yearly["ret"] = yearly["last"] / yearly["first"] - 1
    return {
        "区间": f"{start} ~ {end}",
        "初始资金": round(capital, 0), "期末权益": round(final, 0),
        "总收益": f"{total*100:.2f}%", "年化": f"{annual*100:.2f}%", "最大回撤": f"{dd*100:.2f}%",
        "成交笔数": int(len(trades)),
        "胜率": f"{(len(wins)/max(len(wins)+len(losses),1))*100:.1f}% ({len(wins)}胜{len(losses)}负)",
        "平均盈利": f"{(np.mean(wins)*100 if wins else 0):.2f}%",
        "平均亏损": f"{(np.mean(losses)*100 if losses else 0):.2f}%",
        "分年度": {int(y): f"{r*100:.1f}%" for y, r in yearly["ret"].items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", required=True)
    ap.add_argument("--freq", default="30")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-10-29")
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--trade-start", default="")
    ap.add_argument("--split", default="", help="切分点：给出则分别输出样本内/外")
    ap.add_argument("--out", default="pf")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    sig = pd.read_csv(a.signals, dtype={"code": str, "kind": str, "freq": str})
    res = run(sig, a.freq, a.start, a.end, a.capital,
              trade_start=a.trade_start or None, verbose=a.verbose)
    print("\n===== 组合回测（只吃信号表）=====")
    for k, v in res["stats"].items():
        print(f"{k}: {v}")
    os.makedirs(OUT, exist_ok=True)
    res["equity"].to_csv(os.path.join(OUT, f"{a.out}_equity.csv"), index=False, encoding="utf-8-sig")
    res["trades"].to_csv(os.path.join(OUT, f"{a.out}_trades.csv"), index=False, encoding="utf-8-sig")
    print(f"\n明细：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
