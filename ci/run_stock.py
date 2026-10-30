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
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-10-29")
    ap.add_argument("--window", type=int, default=3000)
    a = ap.parse_args()

    out_dir = os.path.join(HERE, "signals_parts")
    os.makedirs(out_dir, exist_ok=True)
    df = SG.generate(a.code, "30", a.start, a.end, a.window, verbose=False,
                     require_end_peak=VARIANTS[a.variant])
    path = os.path.join(out_dir, f"{a.variant}_{a.code}.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"{a.code} variant={a.variant} signals={len(df)} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
