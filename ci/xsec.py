# -*- coding: utf-8 -*-
"""截面选股：把缠论结构当因子，而不是当择时信号
================================================
与之前所有测试的根本区别：
  · 之前 = 逐票择时（出现买点就买，不管别的票怎么样）
  · 现在 = 截面排序（每天/每月对全市场按结构打分，只买排序靠前的一篮子）

因子全部来自 108 课原文定义（20 课中心定理二、中枢位置、中枢级别）：
  f_pos     = (close - ZD) / (ZG - ZD)      收盘价在最近中枢中的相对位置
  f_break   = close > ZG                    是否突破中枢上沿
  f_up_trend= DD > prev_GG                  20 课中心定理二：后 DD > 前 GG ⇒ 上涨及延续
  f_level   = n_seg                         中枢级别（线段数越多级别越大）

组合构建：每月最后一个交易日调仓，按综合分取前 TOP_N 只等权持有下月；
          与「全样本等权买入持有」对比。

用法（CI 里跑）：
  python ci/xsec.py --top 20 --start 2020-07-01 --end 2026-10-29
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "ci"))


def load_price(code: str) -> pd.DataFrame | None:
    """读缓存的行情（data/min30 或 data/daily），返回 date/close。"""
    for sub in ("min30", "daily", "d"):
        p = os.path.join(HERE, "data", sub, f"{code}.csv.gz")
        if os.path.exists(p):
            try:
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    df = pd.read_csv(f)
            except Exception:
                continue
            if "date" in df.columns and "close" in df.columns and len(df) > 50:
                df["date"] = pd.to_datetime(df["date"])
                return df[["date", "close"]].sort_values("date").reset_index(drop=True)
    return None


def load_zs(code: str) -> pd.DataFrame | None:
    p = os.path.join(HERE, "zs_parts", f"{code}.csv")
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_csv(p)
    except Exception:
        return None
    if df.empty or "ts" not in df.columns:
        return None
    df["ts"] = pd.to_datetime(df["ts"])
    return df.sort_values("ts").reset_index(drop=True)


def build_panel(codes: list[str]) -> pd.DataFrame:
    """把每票的中枢序列 + 收盘价拼成长表：code, date, close, ZG, ZD, DD, prev_GG, n_seg"""
    rows = []
    for c in codes:
        zs = load_zs(c)
        px = load_price(c)
        if zs is None or px is None:
            continue
        # 把中枢序列对齐到每个中枢采样日期的收盘价（用当日及之前最近的收盘价）
        m = pd.merge_asof(zs[["ts", "ZG", "ZD", "GG", "DD", "prev_GG", "n_seg"]],
                          px.rename(columns={"date": "ts"}),
                          on="ts", direction="backward")
        m["code"] = c
        rows.append(m.dropna(subset=["close"]))
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True).sort_values(["ts", "code"]).reset_index(drop=True)


def score_columns(p: pd.DataFrame) -> pd.DataFrame:
    p = p.copy()
    span = (p["ZG"] - p["ZD"]).replace(0, np.nan)
    p["f_pos"] = (p["close"] - p["ZD"]) / span
    p["f_break"] = (p["close"] > p["ZG"]).astype(float)
    p["f_up_trend"] = (p["DD"] > p["prev_GG"]).astype(float)
    p["f_level"] = pd.to_numeric(p["n_seg"], errors="coerce")
    # 截面标准化后再合成：位置 + 突破 + 上涨延续（各 1/3），级别作 0.5 权重
    g = p.groupby("ts")
    def z(s):
        return (s - g[s.name].transform("mean")) / g[s.name].transform("std").replace(0, np.nan)
    p["score"] = (z(p["f_pos"]).fillna(0) + z(p["f_break"]).fillna(0)
                  + z(p["f_up_trend"]).fillna(0) + 0.5 * z(p["f_level"]).fillna(0))
    return p


def monthly_backtest(p: pd.DataFrame, top_n: int, d0: str, d1: str) -> dict:
    """月末调仓：取每个自然月最后一个交易日的截面，按 score 排序买入前 top_n 只。"""
    p = p[(p["ts"] >= pd.Timestamp(d0)) & (p["ts"] <= pd.Timestamp(d1))].copy()
    if p.empty:
        return {"error": "面板为空"}
    p["ym"] = p["ts"].dt.to_period("M")

    # 每票每月最后一个观测的收盘价，用于算下月收益
    last_px = (p.sort_values("ts").groupby(["code", "ym"])["close"].last()
               .unstack("ym"))

    picks_log, rets, months = [], [], []
    yms = sorted(p["ym"].unique())
    for i in range(len(yms) - 1):
        cur, nxt = yms[i], yms[i + 1]
        cross = p[p["ym"] == cur].sort_values("ts").groupby("code").last()
        if cross.empty:
            continue
        sel = cross.nlargest(min(top_n, len(cross)), "score").index.tolist()
        r = []
        for c in sel:
            try:
                a, b = last_px.at[c, cur], last_px.at[c, nxt]
            except Exception:
                continue
            if pd.notna(a) and pd.notna(b) and a > 0:
                r.append(b / a - 1)
        if not r:
            continue
        ret = float(np.mean(r))
        rets.append(ret)
        months.append(str(cur))
        picks_log.append({"month": str(cur), "n": len(r), "ret": round(ret * 100, 2),
                          "picks": sel[:10]})

    # 基准：同月全样本等权
    bench = []
    for i in range(len(yms) - 1):
        cur, nxt = yms[i], yms[i + 1]
        r = []
        for c in last_px.index:
            try:
                a, b = last_px.at[c, cur], last_px.at[c, nxt]
            except Exception:
                continue
            if pd.notna(a) and pd.notna(b) and a > 0:
                r.append(b / a - 1)
        bench.append(float(np.mean(r)) if r else 0.0)

    eq = np.cumprod([1 + x for x in rets]) if rets else np.array([1.0])
    bq = np.cumprod([1 + x for x in bench]) if bench else np.array([1.0])
    dd = float((pd.Series(eq) / pd.Series(eq).cummax() - 1).min()) if len(eq) else 0.0
    return {
        "months": len(rets),
        "ret": round((eq[-1] - 1) * 100, 2),
        "bench": round((bq[-1] - 1) * 100, 2),
        "excess": round(((eq[-1] - 1) - (bq[-1] - 1)) * 100, 2),
        "dd": round(dd * 100, 2),
        "win_months": round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1) if rets else None,
        "bench_win_months": round(sum(1 for x in bench if x > 0) / len(bench) * 100, 1) if bench else None,
        "avg_month": round(float(np.mean(rets)) * 100, 2) if rets else None,
        "picks_sample": picks_log[-3:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--start", default="2020-07-01")
    ap.add_argument("--end", default="2026-10-29")
    ap.add_argument("--outdir", default="out")
    a = ap.parse_args()

    codes = sorted(os.path.splitext(os.path.basename(f))[0]
                   for f in glob.glob(os.path.join(HERE, "zs_parts", "*.csv")))
    print(f"中枢序列文件：{len(codes)} 个")
    if not codes:
        raise SystemExit("没有 zs_parts，先跑 calc 阶段")

    panel = build_panel(codes)
    print(f"面板行数：{len(panel)}，票数：{panel['code'].nunique() if len(panel) else 0}")
    if panel.empty:
        raise SystemExit("面板为空")

    panel = score_columns(panel)
    res = monthly_backtest(panel, a.top, a.start, a.end)
    print(json.dumps(res, ensure_ascii=False, indent=1))

    os.makedirs(os.path.join(HERE, a.outdir), exist_ok=True)
    with open(os.path.join(HERE, a.outdir, f"xsec_top{a.top}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
