# -*- coding: utf-8 -*-
"""价量因子截面回测（CI analyze 用）
====================================
合并 pv_parts/*.csv → 月末截面打分 → 取前 N 只等权持有下月 → 与同池等权基准对比。

因子方向（按 A 股经验先验，写死可复现）：
  f_mom60  +   中期动量（强者恒强）
  f_mom20  +   短期动量
  f_rev5   -   5 日反转（短期超买回落）
  f_vol20  -   低波动异象
  f_turn20 -   高换手往往是情绪过热
  f_amt20  +   流动性（避免买进难以成交的小票）

同时输出每个因子的**月度 Rank IC**（截面 Spearman 相关），
用来判断哪个因子真正有用、而不是只看组合收益。

用法：
  python ci/pv_merge.py --top 30 --bt-start 2020-01-01 --end 2026-10-30
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

_RES_TRADES: list = []


FACTORS = {
    "f_mom60": +1,
    "f_mom20": +1,
    "f_rev5": -1,
    "f_vol20": -1,
    "f_turn20": -1,
    "f_amt20": +1,
}


def load_panel() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(HERE, "pv_parts", "*.csv")))
    frames = []
    for f in files:
        try:
            d = pd.read_csv(f, dtype={"code": str})
        except Exception:
            continue
        if len(d):
            frames.append(d)
    if not frames:
        return pd.DataFrame()
    p = pd.concat(frames, ignore_index=True)
    p["code"] = p["code"].astype(str).str.zfill(6)
    p["date"] = pd.to_datetime(p["date"])
    return p.sort_values(["date", "code"]).reset_index(drop=True)


def zscore_by_date(p: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """按日做截面 z-score（先 1%/99% 缩尾，避免极值主导）。"""
    out = p.copy()
    for c in cols:
        g = out.groupby("date")[c]
        lo = g.transform(lambda s: s.quantile(0.01))
        hi = g.transform(lambda s: s.quantile(0.99))
        s = out[c].clip(lo, hi)
        mu = s.groupby(out["date"]).transform("mean")
        sd = s.groupby(out["date"]).transform("std").replace(0, np.nan)
        out[c + "_z"] = ((s - mu) / sd).fillna(0.0)
    return out




def _spearman(a, b) -> float:
    """秩相关，不依赖 scipy（pandas 的 corr(method="spearman") 需要 scipy）。"""
    try:
        ra = pd.Series(a).rank()
        rb = pd.Series(b).rank()
        if ra.std() == 0 or rb.std() == 0:
            return float("nan")
        return float(ra.corr(rb))
    except Exception:
        return float("nan")

def monthly_backtest(p: pd.DataFrame, top: int, d0: str, d1: str,
                     sign_mode: str = "prior", is_months: int = 36,
                     cost_bps: float = 0.0, neutralize: str = "",
                     timing: str = "none", timing_win: int = 3,
                     capital: float = 0.0) -> dict:
    """月末调仓回测。

    sign_mode:
      prior  —— 用先验符号（动量+、反转-、波动-、换手-、流动性+）
      ic     —— 用**样本内前 is_months 个月**的 IC 均值定符号，再把该符号用到样本外
                （这是标准做法：方向不许用未来数据决定）
    """
    p = p[(p["date"] >= pd.Timestamp(d0)) & (p["date"] <= pd.Timestamp(d1))].copy()
    if p.empty:
        return {"error": "区间内无数据"}
    if neutralize and neutralize in p.columns:
        p["grp"] = p.groupby("date")[neutralize].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))

    p = zscore_by_date(p, list(FACTORS))
    if neutralize and neutralize in p.columns:
        # 风格中性化：把每个 z 因子在该日该组内去均值，剥离组间差异（如大小盘）
        g = p.groupby(["date", "grp"])
        for f in FACTORS:
            col = f + "_z"
            p[col] = p[col] - g[col].transform("mean")
        p[list(FACTORS.keys())[0] + "_z"] = p[list(FACTORS.keys())[0] + "_z"].fillna(0.0)
        for f in FACTORS:
            p[f + "_z"] = p[f + "_z"].fillna(0.0)
    p["ym"] = p["date"].dt.to_period("M")

    # 每票每月的：末收盘价、末因子值、复合分
    p_sorted = p.sort_values("date")
    last_px = p_sorted.groupby(["code", "ym"])["close"].last().unstack("ym")
    last_f = {f: p_sorted.groupby(["code", "ym"])[f].last().unstack("ym") for f in FACTORS}
    last_z = {f: p_sorted.groupby(["code", "ym"])[f + "_z"].last().unstack("ym") for f in FACTORS}

    yms = sorted(p["ym"].unique())
    if len(yms) < 3:
        return {"error": "月份太少"}

    # ---- 逐月：算下月收益 + 各因子 Rank IC ----
    month_rows = []
    for i in range(len(yms) - 1):
        cur, nxt = yms[i], yms[i + 1]
        px_cur, px_nxt = last_px[cur], last_px[nxt]
        base = px_cur.dropna().index.intersection(px_nxt.dropna().index)
        if len(base) < 10:
            continue
        fwd = (px_nxt[base] / px_cur[base] - 1.0)
        row = {"ym": cur, "fwd": fwd, "base": base}
        ics = {}
        for f in FACTORS:
            fv = last_f[f][cur].reindex(base)
            ok = fv.notna()
            if ok.sum() > 10:
                ics[f] = _spearman(fv[ok].values, fwd[ok].values)
        row["ic"] = ics
        month_rows.append(row)

    if not month_rows:
        return {"error": "没有可回测的月份"}

    # ---- 决定符号 ----
    if sign_mode == "ic":
        head = month_rows[:max(6, int(is_months))]
        signs = {}
        for f in FACTORS:
            vals = [r["ic"][f] for r in head if f in r["ic"] and r["ic"][f] == r["ic"][f]]
            mean_ic = float(np.mean(vals)) if vals else 0.0
            signs[f] = 1 if mean_ic >= 0 else -1
        trade_rows = month_rows[len(head):]      # 样本外
    else:
        signs = dict(FACTORS)
        trade_rows = month_rows

    if not trade_rows:
        return {"error": "样本外月份为空"}

    # ---- 组合 ----
    rets, bench, picks = [], [], []
    cash_months = 0
    for r in trade_rows:
        cur, base, fwd = r["ym"], r["base"], r["fwd"]
        score = None
        for f in FACTORS:
            z = last_z[f][cur].reindex(base).fillna(0.0) * signs[f]
            score = z if score is None else score + z
        sel = score.nlargest(min(top, len(score))).index
        # 换手成本：按与上期持仓的重叠度估算（近似：全换时扣 cost_bps）
        prev = set(picks[-1]["picks"]) if picks and "all_picks" in picks[-1] else set()
        turnover = 1.0 if not prev else (1 - len(prev & set(sel)) / max(1, len(sel)))
        cost = (cost_bps / 10000.0) * turnover
        base_ret = float(fwd[sel].mean()) - cost

        # ---- 择时覆盖层 ----
        # 依据：过去 timing_win 个月的滚动表现，若为负则空仓（收益 0，省掉成本）
        if timing != "none" and len(rets) >= timing_win:
            hist = bench[-timing_win:] if timing == "bench_ma" else rets[-timing_win:]
            if float(np.sum(hist)) < 0:
                base_ret = 0.0
                cash_months += 1
        rets.append(base_ret)
        bench.append(float(fwd.mean()))
        picks.append({"month": str(cur), "ym": str(cur), "ret": round(rets[-1] * 100, 2),
                      "bench": round(bench[-1] * 100, 2),
                      "picks": list(sel[:8]), "all_picks": list(sel),
                      "_sel": list(sel)})

    # ---- 逐笔调仓明细（本金 capital>0 时）----
    trades = []
    if capital and capital > 0:
        equity = capital
        prev = set()
        for pk, ret_ in zip(picks, rets):
            cur_s = pk.get("ym") or pk["month"]
            sel = pk.get("_sel") or []
            _pmap = {str(cc): cc for cc in last_px.columns}
            cur_px = last_px[_pmap[cur_s]]
            w = 1.0 / max(1, len(sel))

            def _rec(c, side):
                try:
                    px = float(cur_px.get(c, float("nan")))
                except Exception:
                    return None
                if px != px or px <= 0:
                    return None
                budget = equity * w
                sh = int(budget / px / 100) * 100        # A 股最小 100 股
                if sh <= 0:
                    return None                          # 买不起一手，跳过（真实约束）
                amt = sh * px
                comm = max(5.0, amt * 0.00025)           # 佣金万2.5，最低5元
                stamp = amt * 0.0005 if side == "卖出" else 0.0
                return {"月份": cur_s, "代码": c, "方向": side,
                        "价格": round(px, 3), "股数": sh, "金额": round(amt, 2),
                        "佣金": round(comm, 2), "印花税": round(stamp, 2),
                        "权重%": round(w * 100, 1), "账户总值": round(equity, 0)}

            for c in sorted(prev - set(sel)):
                r_ = _rec(c, "卖出")
                if r_:
                    trades.append(r_)
            for c in sorted(set(sel) - prev):
                r_ = _rec(c, "买入")
                if r_:
                    trades.append(r_)
            prev = set(sel)
            equity *= (1 + ret_ / 100.0)

    eq = np.cumprod([1 + x for x in rets])
    bq = np.cumprod([1 + x for x in bench])
    dd = float((pd.Series(eq) / pd.Series(eq).cummax() - 1).min())

    # ---- 因子 IC 汇总（全样本）----
    ic_summary = {}
    for f in FACTORS:
        vals = [r["ic"][f] for r in month_rows if f in r["ic"] and r["ic"][f] == r["ic"][f]]
        if vals:
            arr = np.array(vals)
            ic_summary[f] = {
                "mean_ic": round(float(arr.mean()), 4),
                "ic_ir": round(float(arr.mean() / arr.std()), 3) if arr.std() > 0 else None,
                "pos_rate": round(float((arr > 0).mean()) * 100, 1),
                "n": int(len(arr)),
            }

    _RES_TRADES.clear()
    _RES_TRADES.extend(trades)
    return {
        "months_oos": len(rets),
        "months_total": len(month_rows),
        "top": top,
        "sign_mode": sign_mode,
        "cost_bps": cost_bps,
        "neutralize": neutralize,
        "timing": timing,
        "timing_win": timing_win,
        "signs": signs,
        "ret": round(float(eq[-1] - 1) * 100, 2),
        "bench": round(float(bq[-1] - 1) * 100, 2),
        "excess": round(float((eq[-1] - 1) - (bq[-1] - 1)) * 100, 2),
        "dd": round(dd * 100, 2),
        "cash_months": int(cash_months),
        "win_months": round(float(np.mean([1 if x > 0 else 0 for x in rets])) * 100, 1),
        "bench_win_months": round(float(np.mean([1 if x > 0 else 0 for x in bench])) * 100, 1),
        "avg_month": round(float(np.mean(rets)) * 100, 2),
        "avg_bench_month": round(float(np.mean(bench)) * 100, 2),
        "ic": ic_summary,
        "trades_dumped": len(trades),
        "last_picks": picks[-3:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--bt-start", default="2020-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--sign-mode", default="ic", choices=["prior", "ic"],
                    help="因子方向：prior=先验 / ic=用前若干月 IC 定符号并做样本外")
    ap.add_argument("--is-months", type=int, default=36, help="定符号用的样本内月数")
    ap.add_argument("--factors", default="",
                    help="只用这些因子，逗号分隔（空=全部）。例：f_mom60,f_mom20,f_rev5")
    ap.add_argument("--capital", type=float, default=0.0,
                    help="初始资金（元）；>0 时额外输出逐笔调仓明细（含股数与金额）")
    ap.add_argument("--timing", default="none",
                    choices=["none", "bench_ma", "strat_ma"],
                    help="择时：none=不择时 / bench_ma=基准趋势向下时空仓 / strat_ma=策略自身趋势向下时空仓")
    ap.add_argument("--timing-win", type=int, default=3, help="择时回看月数")
    ap.add_argument("--neutralize", default="",
                    help="按该因子做分组中性化（如 f_amt20：成交额五分组内再排序），空=不中性化")
    ap.add_argument("--cost-bps", type=float, default=0.0,
                    help="每月调仓双边成本（基点，15=0.15%%）")
    a = ap.parse_args()

    if a.factors.strip():
        keep = {k.strip() for k in a.factors.split(",") if k.strip()}
        for k in list(FACTORS):
            if k not in keep:
                FACTORS.pop(k)
        print(f"只用因子：{sorted(FACTORS)}")

    p = load_panel()
    print(f"面板：{len(p)} 行，{p['code'].nunique() if len(p) else 0} 只票")
    if p.empty:
        raise SystemExit("没有 pv_parts 数据")

    res = monthly_backtest(p, a.top, a.bt_start, a.end,
                           sign_mode=a.sign_mode, is_months=a.is_months,
                           cost_bps=a.cost_bps, neutralize=a.neutralize,
                           timing=a.timing, timing_win=a.timing_win,
                           capital=a.capital)
    print(json.dumps(res, ensure_ascii=False, indent=1))

    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    with open(os.path.join(HERE, "out", f"pv_top{a.top}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    if _RES_TRADES:
        td = pd.DataFrame(_RES_TRADES)
        td.to_csv(os.path.join(HERE, "out", "pv_trades.csv"),
                  index=False, encoding="utf-8-sig")
        print(f"逐笔明细 {len(td)} 行 -> out/pv_trades.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
