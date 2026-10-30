"""把缺失的 30 分钟行情一次性补齐到 data/min30/（4 进程并行，供本地复算用）。

用法：python ci/prefetch.py
"""
from __future__ import annotations

import os
import sys
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data", "min30")


def one(code: str) -> str:
    sys.path.insert(0, HERE)
    import fetch_bars
    path = os.path.join(DATA, f"{code}.csv.gz")
    if os.path.exists(path):
        return f"{code} 已有"
    try:
        df = fetch_bars.fetch_min30(code, "2020-01-01", "2026-10-29")
        if df is None or df.empty:
            return f"{code} 空"
        fetch_bars.save_gz(df, path)
        return f"{code} {len(df)} 行"
    except Exception as e:  # noqa: BLE001
        return f"{code} 失败 {type(e).__name__}: {e}"


def main() -> int:
    os.makedirs(DATA, exist_ok=True)
    codes = sorted({f.split("_", 1)[1].replace(".csv", "").zfill(6)
                    for f in os.listdir(os.path.join(ROOT, "signals_parts"))
                    if f.endswith(".csv") and "_" in f})
    todo = [c for c in codes if not os.path.exists(os.path.join(DATA, f"{c}.csv.gz"))]
    print(f"共 {len(codes)} 只，缺 {len(todo)} 只，开始补…", flush=True)
    with Pool(4) as p:
        for i, r in enumerate(p.imap_unordered(one, todo), 1):
            print(f"[{i}/{len(todo)}] {r}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
