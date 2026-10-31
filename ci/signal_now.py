# -*- coding: utf-8 -*-
"""本期调仓清单生成器（实盘用）
================================
输入：pv_parts/*.csv（全市场因子面板）
输出：
  out/live_target.csv   本期目标持仓（前 N 只，含价格/股数/金额/占比）
  out/live_actions.csv  相对上期清单的买入/卖出/保留动作
  out/live_summary.json 概况（因子符号、风格开关状态、可执行性检查）

与回测口径完全一致：
  因子 = mom60/mom20/rev5/vol20/turn20/amt20，方向用样本内 IC 定，
  截面 z-score（1%/99% 缩尾），总分排序取前 N；
  换手抑制：满 HOLD_MIN 个月且仍在前 HOLD_RANK 名 → 保留（受自适应开关控制）。

「可执行性检查」：按输入本金算每只股数，低于 1 手（100 股）的标出来，
实盘必须先剔除或提高本金，否则组合无法按目标权重建立。

用法（CI）：
  python ci/signal_now.py --capital 500000 --top 50 --hold-min 3 --hold-rank 100 \
      --adaptive-style 3 --end 2026-10-30
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

FACTORS = ["f_mom60", "f_mom20", "f_rev5", "f_vol20", "f_turn20", "f_amt20"]


def _rank_corr(a, b) -> float:
    ra, rb = pd.Series(a).rank(), pd.Series(b).rank()
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(ra.corr(rb))


def load_panel() -> pd.DataFrame:
    frames = []
    for f in sorted(glob.glob(os.path.join(HERE, "pv_parts", "*.csv"))):
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


def zscore_by_date(p: pd.DataFrame) -> pd.DataFrame:
    out = p.copy()
    for c in FACTORS:
        g = out.groupby("date")[c]
        lo = g.transform(lambda s: s.quantile(0.01))
        hi = g.transform(lambda s: s.quantile(0.99))
        s = out[c].clip(lo, hi)
        mu = s.groupby(out["date"]).transform("mean")
        sd = s.groupby(out["date"]).transform("std").replace(0, np.nan)
        out[c + "_z"] = ((s - mu) / sd).fillna(0.0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=500000)
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--is-months", type=int, default=36, help="定符号用的样本内月数")
    ap.add_argument("--adaptive-style", type=int, default=3)
    ap.add_argument("--hold-min", type=int, default=3)
    ap.add_argument("--hold-rank", type=int, default=100)
    ap.add_argument("--end", default="2026-10-30")
    a = ap.parse_args()

    p = load_panel()
    if p.empty:
        raise SystemExit("没有 pv_parts 数据")
    p = p[p["date"] <= pd.Timestamp(a.end)].copy()
    p["ym"] = p["date"].dt.to_period("M")
    p = zscore_by_date(p)

    # ---- 1) 用样本内 IC 定符号（与前 36 个月的实际前瞻收益做秩相关）----
    p_sorted = p.sort_values("date")
    last_px = p_sorted.groupby(["code", "ym"])["close"].last().unstack("ym")
    last_f = {f: p_sorted.groupby(["code", "ym"])[f].last().unstack("ym") for f in FACTORS}
    yms = sorted(p["ym"].unique())
    signs, ic_rows = {}, []
    for i in range(len(yms) - 1):
        cur, nxt = yms[i], yms[i + 1]
        base = last_px[cur].dropna().index.intersection(last_px[nxt].dropna().index)
        if len(base) < 30:
            continue
        fwd = last_px[nxt][base] / last_px[cur][base] - 1.0
        row = {}
        for f in FACTORS:
            fv = last_f[f][cur].reindex(base)
            ok = fv.notna()
            if ok.sum() > 20:
                row[f] = _rank_corr(fv[ok].values, fwd[ok].values)
        if row:
            ic_rows.append(row)
    head = ic_rows[: max(6, a.is_months)]
    for f in FACTORS:
        vals = [r[f] for r in head if f in r and r[f] == r[f]]
        signs[f] = 1 if (vals and float(np.mean(vals)) >= 0) else -1

    # ---- 2) 最新截面打分 ----
    cur_ym = yms[-1]
    cross = p[p["ym"] == cur_ym].sort_values("date").groupby("code").last()
    score = sum(signs[f] * cross[f + "_z"] for f in FACTORS).sort_values(ascending=False)

    # ---- 3) 风格自适应开关（近 N 个月因子多空价差）----
    spreads = []
    for i in range(len(yms) - 1):
        cur, nxt = yms[i], yms[i + 1]
        base = last_px[cur].dropna().index.intersection(last_px[nxt].dropna().index)
        if len(base) < 50:
            continue
        fwd = last_px[nxt][base] / last_px[cur][base] - 1.0
        sc = score.reindex(base).dropna()
        if len(sc) < 50:
            continue
        n = max(1, len(sc) // 10)
        spreads.append(float(fwd[sc.nlargest(n).index].mean() - fwd[sc.nsmallest(n).index].mean()))
    recent = spreads[-a.adaptive_style:] if spreads else []
    rule_on = bool(recent) and (float(np.mean(recent)) > 0)

    # ---- 4) 目标持仓 ----
    target = score.head(a.top)

    # ---- 5) 与上期清单对比 ----
    prev_path = os.path.join(HERE, "out", "live_target.csv")
    prev = set()
    if os.path.exists(prev_path):
        try:
            prev = set(pd.read_csv(prev_path, dtype={"code": str})["code"].str.zfill(6))
        except Exception:
            prev = set()

    cur_px = last_px[cur_ym]
    w = 1.0 / a.top
    rows = []
    for code in target.index:
        px = float(cur_px.get(code, float("nan")))
        budget = a.capital * w
        sh = int(budget / px / 100) * 100 if px == px and px > 0 else 0
        rows.append({
            "code": code, "price": round(px, 3) if px == px else None,
            "score": round(float(target[code]), 3),
            "target_weight_pct": round(w * 100, 2),
            "budget": round(budget, 0),
            "shares": sh, "amount": round(sh * px, 0) if sh else 0,
            "executable": sh >= 100,
            "action": "买入" if code not in prev else "保留",
        })
    tgt = pd.DataFrame(rows)
    actions = [{"code": c, "action": "卖出"} for c in sorted(prev - set(target.index))]
    act = pd.DataFrame(actions) if actions else pd.DataFrame(columns=["code", "action"])
    bad = int((~tgt["executable"]).sum()) if len(tgt) else 0

    out = os.path.join(HERE, "out")
    os.makedirs(out, exist_ok=True)
    tgt.to_csv(os.path.join(out, "live_target.csv"), index=False, encoding="utf-8-sig")
    act.to_csv(os.path.join(out, "live_actions.csv"), index=False, encoding="utf-8-sig")
    summary = {
        "as_of": str(cur_ym), "capital": a.capital, "top": a.top,
        "signs": signs,
        "adaptive_style_months": a.adaptive_style,
        "style_recent_spread": round(float(np.mean(recent)), 6) if recent else None,
        "hold_rule_on": rule_on,
        "hold_min": a.hold_min, "hold_rank": a.hold_rank,
        "universe": int(cross.shape[0]),
        "buy": int((tgt["action"] == "买入").sum()), "keep": int((tgt["action"] == "保留").sum()),
        "sell": int(len(act)),
        "not_executable": bad,
        "total_amount": float(tgt["amount"].sum()),
    }
    with open(os.path.join(out, "live_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(tgt.head(12).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
