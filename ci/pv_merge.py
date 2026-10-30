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


def monthly_backtest(p: pd.DataFrame, top: int, d0: str, d1: str) -> dict:
    p = p[(p["date"] >= pd.Timestamp(d0)) & (p["date"] <= pd.Timestamp(d1))].copy()
    if p.empty:
        return {"error": "区间内无数据"}

    p = zscore_by_date(p, list(FACTORS))
    p["score"] = sum(sign * p[f + "_z"] for f, sign in FACTORS.items())

    p["ym"] = p["date"].dt.to_period("M")
    # 每票每月最后一个交易日的收盘价 + 因子分数
    last_px = p.sort_values("date").groupby(["code", "ym"])["close"].last().unstack("ym")
    last_score = p.sort_values("date").groupby(["code", "ym"])["score"].last().unstack("ym")

    yms = sorted(p["ym"].unique())
    rets, bench, ic_rows = [], [], []
    for i in range(len(yms) - 1):
        cur, nxt = yms[i], yms[i + 1]
        sc = last_score[cur].dropna()
        px_cur, px_nxt = last_px[cur], last_px[nxt]
        common = sc.index.intersection(px_cur.dropna().index).intersection(px_nxt.dropna().index)
        if len(common) < 10:
            continue
        fwd = (px_nxt[common] / px_cur[common] - 1.0)

        # 组合：取分数最高的 top 只
        sel = sc[common].nlargest(min(top, len(common))).index
        rets.append(float(fwd[sel].mean()))
        bench.append(float(fwd.mean()))

        # 每月各因子的 Rank IC
        row = {"month": str(cur), "n": int(len(common))}
        for f in FACTORS:
            col = last_score.columns  # 占位
            try:
                fv = p[p["ym"] == cur].sort_values("date").groupby("code")[f].last()
                cc = fv.index.intersection(fwd.index)
                if len(cc) > 10:
                    row[f] = round(float(pd.Series(fv[cc]).corr(pd.Series(fwd[cc]), method="spearman")), 4)
            except Exception:
                pass
        ic_rows.append(row)

    if not rets:
        return {"error": "没有可回测的月份"}

    eq = np.cumprod([1 + x for x in rets])
    bq = np.cumprod([1 + x for x in bench])
    dd = float((pd.Series(eq) / pd.Series(eq).cummax() - 1).min())

    ic_df = pd.DataFrame(ic_rows)
    ic_summary = {}
    for f in FACTORS:
        if f in ic_df.columns:
            s = ic_df[f].dropna()
            ic_summary[f] = {
                "mean_ic": round(float(s.mean()), 4),
                "ic_ir": round(float(s.mean() / s.std()), 3) if s.std() > 0 else None,
                "pos_rate": round(float((s > 0).mean()) * 100, 1),
            }

    return {
        "months": len(rets),
        "top": top,
        "ret": round((eq[-1] - 1) * 100, 2),
        "bench": round((bq[-1] - 1) * 100, 2),
        "excess": round(((eq[-1] - 1) - (bq[-1] - 1)) * 100, 2),
        "dd": round(dd * 100, 2),
        "win_months": round(float(np.mean([1 if x > 0 else 0 for x in rets])) * 100, 1),
        "bench_win_months": round(float(np.mean([1 if x > 0 else 0 for x in bench])) * 100, 1),
        "avg_month": round(float(np.mean(rets)) * 100, 2),
        "avg_bench_month": round(float(np.mean(bench)) * 100, 2),
        "ic": ic_summary,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--bt-start", default="2020-01-01")
    ap.add_argument("--end", default="2026-10-30")
    a = ap.parse_args()

    p = load_panel()
    print(f"面板：{len(p)} 行，{p['code'].nunique() if len(p) else 0} 只票")
    if p.empty:
        raise SystemExit("没有 pv_parts 数据")

    res = monthly_backtest(p, a.top, a.bt_start, a.end)
    print(json.dumps(res, ensure_ascii=False, indent=1))

    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    with open(os.path.join(HERE, "out", f"pv_top{a.top}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
