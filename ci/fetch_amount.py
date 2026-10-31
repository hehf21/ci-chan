"""CI 取数：全市场真实成交额日线（含已退市票）—— 修 F1 与 F3

为什么在 CI 跑：baostock 逐票约 25 秒，5270 只单线程要 36 小时；CI 60 片并行约 40 分钟。

做法：
  1. 用 query_all_stock 逐年（2010~2026）取"当年在市"的票 → 并集，**天然包含后来退市的票**（修 F3）
  2. 分片后逐票取 date,close,amount,turn 日线（adjustflag=3 不复权，用于成交额与换手）
  3. 存成 out/<code>.csv.gz，供 artifact 上传

用法（本地调试）：python fetch_amount.py --shard 0 --shards 60 --start 2010-01-01 --end 2026-10-30
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import os
import time

import pandas as pd


def universe(years):
    import baostock as bs
    rows = {}
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
    try:
        for y in years:
            for m in ("01-05", "07-05"):
                day = f"{y}-{m}"
                recs = []
                with contextlib.redirect_stdout(io.StringIO()):
                    rs = bs.query_all_stock(day=day)
                    while rs.error_code == "0" and rs.next():
                        recs.append(rs.get_row_data())
                for r in recs:
                    full, name = r[0], (r[2] if len(r) > 2 else "")
                    if "." not in full:
                        continue
                    mkt, code = full.split(".")[0], full.split(".")[-1]
                    if not code.isdigit() or not code.startswith(("00", "30", "60", "68")):
                        continue
                    if any(k in (name or "").upper() for k in ("指数", "ETF", "基金")):
                        continue
                    rows[(mkt, code)] = name
                print(f"{day}: 累计 {len(rows)} 只", flush=True)
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            bs.logout()
    return sorted(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=60)
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    years = list(range(int(args.start[:4]), int(args.end[:4]) + 1))
    codes = universe(years)
    mine = codes[args.shard::args.shards]
    print(f"全市场 {len(codes)} 只；本片 {len(mine)} 只", flush=True)

    import baostock as bs
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
    ok = fail = 0
    t0 = time.time()
    try:
        for k, (mkt, code) in enumerate(mine, 1):
            path = os.path.join(args.out, f"{code}.csv.gz")
            if os.path.exists(path):
                continue
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rs = bs.query_history_k_data_plus(
                        f"{mkt}.{code}", "date,close,amount,turn",
                        start_date=args.start, end_date=args.end,
                        frequency="d", adjustflag="3")
                    rows = []
                    while rs.error_code == "0" and rs.next():
                        rows.append(rs.get_row_data())
            except Exception:
                rows = []
            if len(rows) < 60:
                fail += 1
                continue
            df = pd.DataFrame(rows, columns=["date", "close", "amount", "turn"])
            with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
                df.to_csv(fh, index=False)
            ok += 1
            if k % 100 == 0:
                print(f"[{k}/{len(mine)}] ok={ok} fail={fail} "
                      f"{(time.time()-t0)/max(ok,1):.2f}s/票", flush=True)
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            bs.logout()
    print(f"完成：ok={ok} fail={fail}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
