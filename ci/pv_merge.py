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


def monthly_backtest(p: pd.DataFrame, top: int, d0: str, d1: str,
                     sign_mode: str = "prior", is_months: int = 36) -> dict:
    """月末调仓回测。

    sign_mode:
      prior  —— 用先验符号（动量+、反转-、波动-、换手-、流动性+）
      ic     —— 用**样本内前 is_months 个月**的 IC 均值定符号，再把该符号用到样本外
                （这是标准做法：方向不许用未来数据决定）
    """
    p = p[(p["date"] >= pd.Timestamp(d0)) & (p["date"] <= pd.Timestamp(d1))].copy()
    if p.empty:
        return {"error": "区间内无数据"}

    p = zscore_by_date(p, list(FACTORS))
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
                ics[f] = float(pd.Series(fv[ok]).corr(pd.Series(fwd[ok]), method="spearman"))
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
    for r in trade_rows:
        cur, base, fwd = r["ym"], r["base"], r["fwd"]
        score = None
        for f in FACTORS:
            z = last_z[f][cur].reindex(base).fillna(0.0) * signs[f]
            score = z if score is None else score + z
        sel = score.nlargest(min(top, len(score))).index
        rets.append(float(fwd[sel].mean()))
        bench.append(float(fwd.mean()))
        picks.append({"month": str(cur), "ret": round(rets[-1] * 100, 2),
                      "bench": round(bench[-1] * 100, 2), "picks": list(sel[:8])})

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

    return {
        "months_oos": len(rets),
        "months_total": len(month_rows),
        "top": top,
        "sign_mode": sign_mode,
        "signs": signs,
        "ret": round(float(eq[-1] - 1) * 100, 2),
        "bench": round(float(bq[-1] - 1) * 100, 2),
        "excess": round(float((eq[-1] - 1) - (bq[-1] - 1)) * 100, 2),
        "dd": round(dd * 100, 2),
        "win_months": round(float(np.mean([1 if x > 0 else 0 for x in rets])) * 100, 1),
        "bench_win_months": round(float(np.mean([1 if x > 0 else 0 for x in bench])) * 100, 1),
        "avg_month": round(float(np.mean(rets)) * 100, 2),
        "avg_bench_month": round(float(np.mean(bench)) * 100, 2),
        "ic": ic_summary,
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
    a = ap.parse_args()

    p = load_panel()
    print(f"面板：{len(p)} 行，{p['code'].nunique() if len(p) else 0} 只票")
    if p.empty:
        raise SystemExit("没有 pv_parts 数据")

    res = monthly_backtest(p, a.top, a.bt_start, a.end,
                           sign_mode=a.sign_mode, is_months=a.is_months)
    print(json.dumps(res, ensure_ascii=False, indent=1))

    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    with open(os.path.join(HERE, "out", f"pv_top{a.top}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
