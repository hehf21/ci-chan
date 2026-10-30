"""单票：取 30 分钟数据 → 跑 chan_law → 导出逐 bar 状态序列（给横截面 IC 合并用）。

输出 ci/_states/<code>.csv.gz，列：
    dt, close, seg_dir, broken, pos_in_zs, dist_zd, dist_zg, zs_width, zs_age, seg_age

注意：baostock 会往 stdout 打印 login/logout success，必须 redirect_stdout，
否则会污染矩阵里用 stdout 传参的脚本（历史上吃过这个亏）。
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import chan_law as L  # noqa: E402
from fetch_bars import fetch_min30  # noqa: E402

OUT = os.path.join(HERE, "_states")
START = "2020-01-01"
END = "2026-10-29"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    df = fetch_min30(args.code, args.start, args.end)
    if df is None or len(df) < 500:
        print(f"{args.code}: 数据不足（{0 if df is None else len(df)}），跳过")
        return 0
    df = df.reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    bars = [L.Bar(t=i, high=float(df["high"][i]), low=float(df["low"][i]),
                  open=float(df["open"][i]), close=float(df["close"][i]))
            for i in range(len(df))]
    c = L.run(bars)
    tl = L.timeline(c)
    rows = []
    for k, s in enumerate(tl):
        zd, zg, cl = s["zd"], s["zg"], s["close"]
        rows.append({
            "dt": df["date"][k].strftime("%Y-%m-%d %H:%M:%S"),
            "close": cl,
            "seg_dir": s["seg_dir"],
            "broken": 1 if s["broken"] else 0,
            "pos_in_zs": ((cl - zd) / (zg - zd)) if (zd is not None and zg is not None and zg > zd) else None,
            "dist_zd": ((cl - zd) / zd) if zd else None,
            "dist_zg": ((cl - zg) / zg) if zg else None,
            "zs_width": ((zg - zd) / zd) if (zd and zg is not None and zg > zd) else None,
            "zs_age": s["zs_age"],
            "seg_age": s["seg_age"],
        })
    out = pd.DataFrame(rows)
    path = os.path.join(OUT, f"{args.code}.csv.gz")
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        out.to_csv(fh, index=False)
    print(f"{args.code}: {len(out)} 行，结构 {c.summarize()} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
