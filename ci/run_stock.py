"""CI：单只票生成信号 → signals_parts/<variant>_<code>.csv

用法：
    python ci/run_stock.py --code 600519 --variant ep1
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "ci"))

import patch_data  # noqa: E402

patch_data.install()

import signal_gen as SG  # noqa: E402

VARIANTS = {"ep0": False, "ep1": True}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--variant", default="ep1", choices=sorted(VARIANTS))
    ap.add_argument("--freq", default="30", help="30=30分钟 / d=日线")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-10-29")
    ap.add_argument("--window", type=int, default=3000)
    ap.add_argument("--step", type=int, default=1, help="扫描步长（日线建议 5）")
    ap.add_argument("--require-above-ma", type=int, default=0,
                    help="第8课能搞：只在收盘站上 N 日线时发买点（0=关）")
    a = ap.parse_args()

    out_dir = os.path.join(HERE, "signals_parts")
    os.makedirs(out_dir, exist_ok=True)
    try:
        df = SG.generate(a.code, a.freq, a.start, a.end, a.window, verbose=False,
                         require_end_peak=VARIANTS[a.variant],
                         require_above_ma=a.require_above_ma, step=a.step)
    except FileNotFoundError as e:
        # 退市 / 停牌太久 / baostock 无数据的票：写空分片，不让整个矩阵挂掉
        print(f"SKIP {a.code}: {e}")
        df = None
    path = os.path.join(out_dir, f"{a.variant}_{a.code}.csv")
    if df is None or df.empty:
        # 空分片也要写，merge 时统一跳过
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("ts,code,freq,kind,px,stop\n")
        print(f"{a.code} 无信号（无数据或结构不足） -> {path}")
        return 0
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"{a.code} variant={a.variant} signals={len(df)} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
