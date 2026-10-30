# -*- coding: utf-8 -*-
"""单票 GG/DD 中枢序列生成器（CI 矩阵用）
==========================================
用途：给 A1 臂提供「逐 bar 最新中枢 + 前一中枢」，使 20 课中心定理二
      `后DD > 前GG`（上涨及其延续）/ `后DD <= 前GG`（未形成趋势）可判定。

仿 `策略/gen_zs_series.py`：从第 MIN_BARS 根起，每 STEP 根取一次窗口
（末根为当前 bar，长度 <= WINDOW），跑 `chan_strict.pipeline.analyze(max_level=1)`，
取 `zhongshus[-1]` 为 cur、`zhongshus[-2]` 为 prev。

字段来源（`chan_strict/zs.py:114-129` 的输出字典）：
    ZG / ZD = 中枢区间上下沿（20 课 ZG=min(g1,g2)、ZD=max(d1,d2)）
    GG      = `inner_hi`（中枢内各段实际最高，20 课 GG=max(gn)）
    DD      = `inner_lo`（中枢内各段实际最低，20 课 DD=min(dn)）
    n_zs    = 该窗口内已成形中枢个数；n_seg = 已确认线段数
    本脚本**不输出 G / D**（说明书-v2 §四近似清单第 3 条：G/D 未实现）

输出列（顺序写死，勿改）：
    code,ts,ZG,ZD,GG,DD,prev_ZG,prev_ZD,prev_GG,prev_DD,n_zs,n_seg
    只有 1 个中枢时 prev_* 留空。

用法：
    python run_zs_gg.py --code 600519 --start 2020-01-01 --end 2026-10-29 --step 4
    → zs_parts/600519.csv
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)             # CI 仓库根
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import patch_data                        # noqa: E402

patch_data.install()                     # 行情改读 data/min30/*.csv.gz，缺失才现取

import chan_data                        # noqa: E402
from chan_strict import pipeline as PL   # noqa: E402

COLS = ["code", "ts", "ZG", "ZD", "GG", "DD",
        "prev_ZG", "prev_ZD", "prev_GG", "prev_DD", "n_zs", "n_seg"]


def _f(x, nd: int = 4):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else round(v, nd)


def one(code: str, start: str, end: str, step: int, window: int,
        min_bars: int, outdir: str) -> str:
    bars = chan_data.get_min30(code, start, end)
    if bars is None or len(bars) < min_bars + 10:
        raise SystemExit(f"[失败] {code} 30 分钟数据不足（{0 if bars is None else len(bars)} 根）")
    bars = bars.copy()
    bars["date"] = pd.to_datetime(bars["date"])
    bars = bars.sort_values("date").reset_index(drop=True)
    ohlc = bars[["open", "high", "low", "close"]]

    rows, t0, err, n_gg = [], time.time(), 0, 0
    for i in range(min_bars, len(bars), step):
        lo = max(0, i + 1 - window)
        win = ohlc.iloc[lo: i + 1].reset_index(drop=True)
        try:
            res = PL.analyze(win, max_level=1)
        except Exception:                      # noqa: BLE001  单点失败不毁整条序列
            err += 1
            continue
        zs = res.get("zhongshus") or []
        n_seg = len(res.get("segs") or [])
        if not zs:
            rows.append([code, bars["date"].iloc[i], None, None, None, None,
                         None, None, None, None, 0, n_seg])
            continue
        cur = zs[-1]
        prev = zs[-2] if len(zs) >= 2 else {}
        gg = _f(cur.get("GG"))
        n_gg += 0 if gg is None else 1
        rows.append([code, bars["date"].iloc[i],
                     _f(cur.get("ZG")), _f(cur.get("ZD")), gg, _f(cur.get("DD")),
                     _f(prev.get("ZG")), _f(prev.get("ZD")),
                     _f(prev.get("GG")), _f(prev.get("DD")),
                     len(zs), n_seg])

    df = pd.DataFrame(rows, columns=COLS)
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"{code}.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")

    z = df.dropna(subset=["ZG", "ZD", "GG", "DD"])
    bad = int(((z.GG < z.ZG - 1e-9) | (z.ZG < z.ZD - 1e-9) | (z.ZD < z.DD - 1e-9)).sum()) if len(z) else 0
    both = int(df.dropna(subset=["prev_GG", "DD"]).shape[0])
    print(f"[完成] {code}: {len(bars)} 根 → {len(df)} 采样点 / GG 非空 {n_gg} / "
          f"可判趋势点 {both} / 不变式违规 {bad} / 异常 {err} / {time.time()-t0:.0f}s",
          flush=True)
    if bad:
        raise SystemExit(f"[失败] {code} 中枢不变式违规 {bad} 条（GG>=ZG>=ZD>=DD），拒绝产出")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-10-29")
    ap.add_argument("--step", type=int, default=4)
    ap.add_argument("--window", type=int, default=3000)
    ap.add_argument("--min-bars", type=int, default=800)
    ap.add_argument("--outdir", default=os.path.join(HERE, "zs_parts"))
    a = ap.parse_args()
    p = one(a.code.zfill(6), a.start, a.end, a.step, a.window, a.min_bars, a.outdir)
    print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
