# -*- coding: utf-8 -*-
"""A0 vs A1 对照回测（out-of-tree，不改根目录任何文件）
=========================================================
A0 = 关 trailing / zs / panzheng / regime_ma / time_exit，纯结构止损（= 决策书 T 档口径）
A1 = A0 + `panzheng_exit_mode="GG"`：持仓中出现新中枢且 `cur.DD <= prev.GG`（20 课中心定理二
     「未形成趋势」）→ 以该中枢的 **GG**（20 课「盘整的高点」）为卖出价位，当根收盘 ≥ GG 即挂
     卖单（下一根开盘成交，与根目录「当根收盘算、下一根开盘成交」口径一致）。

成交/风控逻辑逐条复刻 `策略/portfolio_bt.py`（本文件是副本，不 import 它），
成本：佣金万 2.5 双边 / 卖出印花税 0.05% / 滑点单边 0.1%。

用法：
  python bt_gg.py --signals <v2fix.csv> --codes 600519,600036 --arm A0 --split
  python bt_gg.py --signals <v2fix.csv> --arm both --split --out smoke6
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)            # CI 仓库根（不是本机 策略/ 目录）
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import patch_data  # noqa: E402

patch_data.install()                    # 行情改读 data/min30/*.csv.gz，缺失才现取

import chan_data  # noqa: E402

COMMISSION = 0.00025
STAMP = 0.0005
SLIP = 0.001

DEFAULT_CFG = {
    "max_positions": 3,
    "max_weight_per_stock": 0.30,
    "risk_per_trade": 0.01,
    "reserve_cash": 0.10,
    "entry_slack": 0.005,
    "limit_valid_bars": 8,
    "stop_buffer": 0.005,
    "cooldown_bars": 8,          # 根目录硬编码在 L341，这里提成显式参数
    "regime_ma": 0,
    "time_exit_bars": 0,
    "trailing_stop": False,
    "panzheng_exit_mode": "off",  # off | ZG | GG
    "zs_series": os.path.join(HERE, "zs_series_gg.csv"),
}


def sha16(path: str) -> str:
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16].upper()
    except OSError:
        return "MISSING"


def load_bars(code: str, freq: str, start: str, end: str) -> pd.DataFrame | None:
    d = chan_data.get_min30(code, start, end)
    if d is None:
        return None
    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    return d.sort_values("date").reset_index(drop=True)


def load_zs(path: str) -> dict:
    if not os.path.exists(path):
        print(f"[提示] 找不到中枢序列 {path}", flush=True)
        return {}
    z = pd.read_csv(path, dtype={"code": str})
    z["code"] = z["code"].str.zfill(6)
    z["ts"] = pd.to_datetime(z["ts"])
    out = {}
    for code, g in z.groupby("code"):
        g = g.sort_values("ts")

        def col(n):
            return [None if pd.isna(v) else float(v) for v in g[n]] if n in g else [None] * len(g)

        out[code] = {"ts": list(g["ts"]), "zg": col("ZG"), "zd": col("ZD"),
                     "gg": col("GG"), "dd": col("DD"),
                     "prev_gg": col("prev_GG"), "prev_dd": col("prev_DD"),
                     "n_zs": [int(v) if not pd.isna(v) else None for v in g["n_zs"]]}
    print(f"[中枢序列] 载入 {len(out)} 只票 / {len(z)} 采样点", flush=True)
    return out


def dedupe_signals(sig: pd.DataFrame, gap: int = 40, dpx: float = 0.01) -> pd.DataFrame:
    """决策书 §2.2-1 的口径：同票、距上次 <40 根、价格差 <1% 视为同一机会，只留第一次。"""
    keep, last = [], {}
    for _, r in sig.sort_values(["code", "bar_idx"]).iterrows():
        k = str(r["code"])
        prev = last.get(k)
        if prev is not None and int(r["bar_idx"]) - prev[0] < gap and \
                abs(float(r["px"]) - prev[1]) / prev[1] < dpx:
            continue
        keep.append(_)
        last[k] = (int(r["bar_idx"]), float(r["px"]))
    return sig.loc[keep].reset_index(drop=True)


def _buy(port, code, px, t, act):
    if code in port["positions"]:
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
                              "entry_day": t.normalize(), "entry_ts": t,
                              "kind": act.get("kind"), "entry_n_zs": act.get("n_zs")}
    port["trades"].append({"date": t, "code": code, "side": "buy", "price": px,
                           "shares": shares, "reason": act.get("reason", "")})


def _sell(port, code, px, t, reason):
    p = port["positions"].get(code)
    if not p:
        return
    shares = p["shares"]
    port["cash"] += shares * px * (1 - COMMISSION - STAMP)
    port["trades"].append({"date": t, "code": code, "side": "sell", "price": px,
                           "shares": shares, "reason": reason})
    port["positions"].pop(code, None)


def run(sig: pd.DataFrame, freq: str, start: str, end: str, capital: float = 1_000_000.0,
        cfg: dict | None = None, trade_start: str | None = None) -> dict:
    c = {**DEFAULT_CFG, **(cfg or {})}
    sig = sig.copy()
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig["code"] = sig["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    bars = {}
    for code in sorted(sig["code"].unique()):
        b = load_bars(code, freq, start, end)
        if b is not None and len(b) >= 100:
            bars[code] = b
    if not bars:
        raise RuntimeError("没有可用行情")
    zs_series = load_zs(c["zs_series"]) if c["panzheng_exit_mode"] != "off" else {}

    axis = sorted(set().union(*[set(b["date"]) for b in bars.values()]))
    pos_of = {k: {t: i for i, t in enumerate(v["date"])} for k, v in bars.items()}
    by_time: dict = {}
    for _, r in sig.iterrows():
        by_time.setdefault(r["ts"], []).append(r)

    port = {"cash": capital, "positions": {}, "trades": []}
    pending: list[dict] = []
    last_exit = {k: -10 ** 6 for k in bars}
    last_px: dict = {}
    eq_rows, gg_hits, gg_checks = [], 0, 0
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
                if od.get("stop") and float(bars[code]["open"].iloc[i]) <= float(od["stop"]):
                    continue
                if ti > od["expire"]:
                    continue
                lim = od["limit"]
                if float(bars[code]["low"].iloc[i]) > lim:
                    still.append(od)
                    continue
                px = min(float(bars[code]["open"].iloc[i]), lim) * (1 + SLIP)
                _buy(port, code, px, t, act)
            else:
                pos = port["positions"].get(code)
                if pos and pos["entry_day"] == t.normalize():
                    still.append(od)          # T+1：当日买入当日不可卖
                    continue
                px = float(bars[code]["open"].iloc[i]) * (1 - SLIP)
                _sell(port, code, px, t, act.get("reason", ""))
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

        # ---------- 盘整高点退出（A1）
        if c["panzheng_exit_mode"] != "off" and zs_series:
            import bisect
            for code, p in list(port["positions"].items()):
                ser = zs_series.get(code)
                i = pos_of[code].get(t)
                if not ser or i is None:
                    continue
                j = bisect.bisect_right(ser["ts"], t) - 1
                if j < 0 or ser["n_zs"][j] is None:
                    continue
                entry_n = p.get("entry_n_zs")
                if entry_n is None or ser["n_zs"][j] <= int(entry_n):
                    continue                     # 还没出现新中枢 → 谈不上"盘整"
                gg, dd, pgg = ser["gg"][j], ser["dd"][j], ser["prev_gg"][j]
                if c["panzheng_exit_mode"] == "ZG":
                    tgt = ser["zg"][j]
                    cond = tgt is not None and float(bars[code]["close"].iloc[i]) >= float(tgt)
                    why = "盘整高点出掉-ZG(20课)"
                else:                             # GG
                    gg_checks += 1
                    is_trend = (dd is not None and pgg is not None and dd > pgg)
                    tgt = gg
                    cond = (tgt is not None and not is_trend and
                            float(bars[code]["close"].iloc[i]) >= float(tgt))
                    why = "盘整高点出掉-GG(20课)"
                if cond:
                    gg_hits += 1
                    pending.append({"code": code, "act": {"side": "sell", "reason": why}, "expire": ti + 1})

        # ---------- 结构止损
        for code, p in list(port["positions"].items()):
            i = pos_of[code].get(t)
            if i is None:
                continue
            if float(bars[code]["close"].iloc[i]) < p["stop"] * (1 - c["stop_buffer"]):
                pending.append({"code": code, "act": {"side": "sell", "reason": "破结构失效价"},
                                "expire": ti + 1})

        # ---------- 信号 → 挂单
        total = port["cash"] + sum(pp["shares"] * last_px.get(k, pp["cost"])
                                   for k, pp in port["positions"].items())
        for r in by_time.get(t, []):
            code = r["code"]
            if code not in bars:
                continue
            cur_n_zs = None
            ser0 = zs_series.get(code)
            if ser0:
                import bisect as _bs
                _j = _bs.bisect_right(ser0["ts"], t) - 1
                if _j >= 0:
                    cur_n_zs = ser0["n_zs"][_j]
            if str(r["kind"]).startswith("S"):
                if code in port["positions"]:
                    pending.append({"code": code, "act": {"side": "sell", "reason": f"{r['kind']} 卖点"},
                                    "expire": ti + 1})
                continue
            if code in port["positions"] or len(port["positions"]) >= c["max_positions"]:
                continue
            if any(o["code"] == code and o["act"]["side"] == "buy" for o in pending):
                continue
            if ti - last_exit.get(code, -10 ** 6) < int(c["cooldown_bars"]):
                continue
            lim = float(r["px"]) * (1 + c["entry_slack"])
            stop = float(r["stop"])
            risk_frac = max((lim - stop) / lim, 1e-6)
            w = min(c["max_weight_per_stock"], c["risk_per_trade"] / risk_frac)
            amount = min(total * w, port["cash"] * (1 - c["reserve_cash"]))
            if amount < 1000:
                continue
            pending.append({"code": code, "stop": stop,
                            "act": {"side": "buy", "amount": amount, "stop": stop,
                                    "n_zs": cur_n_zs, "kind": r["kind"],
                                    "reason": f"{r['kind']} 买点"},
                            "expire": ti + int(c["limit_valid_bars"]), "limit": lim})

        eq_rows.append({"date": t, "equity": port["cash"] + sum(
            pp["shares"] * last_px.get(k, pp["cost"]) for k, pp in port["positions"].items())})

    last_t = axis[-1]
    for code in list(port["positions"]):
        i = pos_of[code].get(last_t)
        if i is not None:
            _sell(port, code, float(bars[code]["close"].iloc[i]) * (1 - SLIP), last_t, "期末平仓")

    eqdf = pd.DataFrame(eq_rows)
    trades = pd.DataFrame(port["trades"])
    return {"equity": eqdf, "trades": trades,
            "stats": stats(eqdf, trades, capital),
            "gg_hits": gg_hits, "gg_checks": gg_checks}


def stats(eq: pd.DataFrame, trades: pd.DataFrame, capital: float) -> dict:
    if eq.empty:
        return {}
    final = float(eq["equity"].iloc[-1])
    total = final / capital - 1
    dd = float((eq["equity"] / eq["equity"].cummax() - 1).min())
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
    n_rt = len(wins) + len(losses)
    aw = float(np.mean(wins)) if wins else 0.0
    al = float(np.mean(losses)) if losses else 0.0
    return {
        "总收益%": round(total * 100, 2),
        "最大回撤%": round(dd * 100, 2),
        "回合": n_rt,
        "胜率%": round(len(wins) / max(n_rt, 1) * 100, 1),
        "平均盈利%": round(aw * 100, 2),
        "平均亏损%": round(al * 100, 2),
        "盈亏比": round(aw / abs(al), 2) if al else None,
        "期望%/回合": round((np.mean(wins + losses)) * 100, 3) if n_rt else None,
    }


def split_run(res_all: dict, arm: str) -> dict:
    eq = res_all["equity"]
    cuts = {"全段": (None, None),
            "样本内": (pd.Timestamp("2020-07-01"), pd.Timestamp("2023-12-31 23:59")),
            "样本外": (pd.Timestamp("2024-01-01"), None)}
    out = {}
    for name, (a, b) in cuts.items():
        e = eq.copy()
        if a is not None:
            e = e[e["date"] >= a]
        if b is not None:
            e = e[e["date"] <= b]
        if e.empty:
            continue
        base = float(e["equity"].iloc[0])
        total = float(e["equity"].iloc[-1]) / base - 1
        dd = float((e["equity"] / e["equity"].cummax() - 1).min())
        out[name] = {"收益%": round(total * 100, 2), "回撤%": round(dd * 100, 2)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", required=True)
    ap.add_argument("--freq", default="30")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-10-29")
    ap.add_argument("--trade-start", default="2020-07-01")
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--codes", default="")
    ap.add_argument("--arm", default="both", choices=["A0", "A1", "both", "ZG"])
    ap.add_argument("--dedupe", default="rule40", choices=["none", "rule40"])
    ap.add_argument("--zs", default=os.path.join(HERE, "zs_series_gg.csv"))
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    sig = pd.read_csv(a.signals, dtype={"code": str, "kind": str, "freq": str})
    sig["code"] = sig["code"].astype(str).str.zfill(6)
    n_raw = len(sig)
    if a.dedupe == "rule40":
        sig = dedupe_signals(sig)
    if a.codes:
        keep = [c.strip() for c in a.codes.split(",") if c.strip()]
        sig = sig[sig["code"].isin(keep)].reset_index(drop=True)
    print(f"[信号] 原始 {n_raw} → 去重后 {len(sig)} → 本次票 {sig['code'].nunique()} 只 / {len(sig)} 条",
          flush=True)
    if sig.empty:
        print("没有信号可跑")
        return 2

    arms = ["A0", "A1"] if a.arm == "both" else [a.arm]
    result = {"signals": os.path.abspath(a.signals), "dedupe": a.dedupe,
              "n_signals_raw": int(n_raw), "n_signals_used": int(len(sig)),
              "codes": sorted(sig["code"].unique().tolist()),
              "cost": {"commission": COMMISSION, "stamp": STAMP, "slip": SLIP},
              "sha256_16": {"bt_gg.py": sha16(os.path.abspath(__file__)),
                            "gen_zs_series_gg.py": sha16(os.path.join(HERE, "gen_zs_series_gg.py")),
                            "portfolio_bt.py(参考)": sha16(os.path.join(ROOT, "portfolio_bt.py")),
                            "chan_strict/zs.py": sha16(os.path.join(ROOT, "chan_strict", "zs.py")),
                            "chan_strict/pipeline.py": sha16(os.path.join(ROOT, "chan_strict", "pipeline.py"))},
              "zs_series": os.path.abspath(a.zs), "arms": {}}
    for arm in arms:
        mode = "off" if arm == "A0" else ("ZG" if arm == "ZG" else "GG")
        cfg = {"panzheng_exit_mode": mode, "zs_series": a.zs}
        r = run(sig, a.freq, a.start, a.end, a.capital, cfg=cfg, trade_start=a.trade_start or None)
        sp = split_run(r, arm)
        result["arms"][arm] = {"stats": r["stats"], "split": sp,
                               "gg_hits": r["gg_hits"], "gg_checks": r["gg_checks"]}
        print(f"\n===== {arm} （panzheng_exit_mode={mode}）=====")
        for k, v in r["stats"].items():
            print(f"  {k}: {v}")
        for k, v in sp.items():
            print(f"  {k}: 收益 {v['收益%']}% / 回撤 {v['回撤%']}%")
        print(f"  GG 判定次数 {r['gg_checks']} / 触发 {r['gg_hits']}", flush=True)
        if a.out:
            r["trades"].to_csv(os.path.join(HERE, f"trades_{a.out}_{arm}.csv"),
                               index=False, encoding="utf-8-sig")

    with open(os.path.join(HERE, f"res_{a.out or 'run'}.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
