# -*- coding: utf-8 -*-
"""单票「日线结构状态」时间序列（CI 矩阵用）
============================================
给换手抑制规则提供**真正的缠论日线结构**判定，替代「近 20 日最低」这个粗糙代理。

每一天输出一行：
  code, date,
  zs_zd, zs_zg     最近一个日线中枢的下沿/上沿（没有中枢则空）
  zs_n             窗口内已成形中枢个数
  seg_dir          最近一段已确认日线线段的方向（1 上 / -1 下）
  trend_type       当前日线走势类型（上涨/下跌/盘整）
  sell_recent      最近 SELL_LOOKBACK 根内是否出现过日线卖点（S1/S2/S3）
  broke_zs         收盘是否跌破最近中枢下沿（结构走坏的直接判据）

「结构是否走坏」（供 pv_merge 的换手抑制使用）：
  broke = (zs_zd 存在 且 close < zs_zd) 或 sell_recent == 1

性能：日线数据只有几千根，每 STEP 根重算一次窗口即可，单票 1~3 秒。
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import chan_data  # noqa: E402
from chan_strict import pipeline as PL  # noqa: E402

SELL_LOOKBACK = 20


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--start", default="2006-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=1200, help="日线分析窗口（根）")
    ap.add_argument("--min-bars", type=int, default=300)
    ap.add_argument("--step", type=int, default=5, help="每 N 根重算一次")
    ap.add_argument("--outdir", default="chan_parts")
    a = ap.parse_args()

    try:
        d = chan_data.get_daily(a.code, a.start, a.end)
    except Exception as e:  # noqa: BLE001
        print(f"SKIP {a.code}: {e}")
        return 0
    if d is None or len(d) < a.min_bars + 50:
        print(f"SKIP {a.code}: 数据不足")
        return 0

    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    ohlc = d[["open", "high", "low", "close"]]

    rows = []
    state = {"zs_zd": None, "zs_zg": None, "zs_n": 0, "seg_dir": 0,
             "trend_type": None, "sell_recent": 0}
    sell_flags = []                      # 逐 bar 是否刚出现卖点

    for i in range(a.min_bars, len(d)):
        if (i - a.min_bars) % max(1, a.step) == 0:
            lo = max(0, i + 1 - a.window)
            win = ohlc.iloc[lo: i + 1].reset_index(drop=True)
            try:
                res = PL.analyze(win, max_level=1)
            except Exception:
                res = None
            if res:
                zss = res.get("zhongshus") or []
                segs = res.get("segs") or []
                tr = res.get("trend") or {}
                state["zs_n"] = len(zss)
                if zss:
                    state["zs_zd"] = float(zss[-1]["ZD"])
                    state["zs_zg"] = float(zss[-1]["ZG"])
                state["seg_dir"] = int(segs[-1]["dir"]) if segs else 0
                state["trend_type"] = tr.get("type")
                # 刚出现的卖点（右端 bar 在最近 3 根内）
                fresh_sell = 0
                for p in (res.get("points") or []):
                    if str(p.get("kind", "")).startswith("S"):
                        raw = lo + int(p.get("raw", -1))
                        if i - raw <= 3:
                            fresh_sell = 1
                sell_flags.append(fresh_sell)
        sell_recent = 1 if any(sell_flags[-(SELL_LOOKBACK // max(1, a.step)):]) else 0
        close = float(d["close"].iloc[i])
        broke = 0
        if state["zs_zd"] is not None and close < state["zs_zd"]:
            broke = 1
        if sell_recent:
            broke = 1
        rows.append({
            "code": str(a.code).zfill(6), "date": d["date"].iloc[i],
            "zs_zd": state["zs_zd"], "zs_zg": state["zs_zg"], "zs_n": state["zs_n"],
            "seg_dir": state["seg_dir"], "trend_type": state["trend_type"],
            "sell_recent": sell_recent, "broke_zs": broke,
        })

    out = pd.DataFrame(rows)
    os.makedirs(os.path.join(HERE, a.outdir), exist_ok=True)
    path = os.path.join(HERE, a.outdir, f"{a.code}.csv")
    out.to_csv(path, index=False, encoding="utf-8")
    print(f"{a.code}: {len(out)} 行 -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
