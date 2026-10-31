# -*- coding: utf-8 -*-
"""point-in-time 股票池：按月回看「当时真实在交易」的标的
=======================================================
为什么必须做：现在所有回测都用「今天还在上市」的股票池，
退市的票根本没进来 —— 这是幸存者偏差，会让收益系统性虚高。

做法：对每个采样日的 `query_all_stock(day=...)` 取当日**正常交易**的标的，
按月存一份成员表；同时输出所有出现过的代码并集（含后来退市的），
供 calc 阶段一次性取数。

输出：
  pit/pit_membership.csv.gz  列：ym, code
  pit/pit_codes.txt          所有出现过的代码（逗号分隔一行）

用法：
  python ci/pit_universe.py --start 2013-01-01 --end 2026-10-30 --outdir pit
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def _clean(code: str, name: str) -> str | None:
    c = str(code).split(".")[-1]
    if not (c.isdigit() and len(c) == 6):
        return None
    if name.startswith(("ST", "*ST", "PT")) or "退" in name:
        return None
    # 剔除指数、B 股、场内基金
    if c.startswith(("000001",)) and name.endswith("指数"):
        return None
    if c.startswith(("200", "900", "51", "58", "15", "16")):
        return None
    return c


def main() -> int:
    import baostock as bs

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2013-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--outdir", default="pit")
    a = ap.parse_args()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lg = bs.login()
    if lg.error_code != "0":
        raise SystemExit(f"login failed: {lg.error_msg}")

    # 月末采样：每月最后一个自然日去查（baostock 会返回该日之前最近交易日的快照）
    months = pd.period_range(pd.Timestamp(a.start).to_period("M"),
                             pd.Timestamp(a.end).to_period("M"), freq="M")
    rows = []
    try:
        for mp in months:
            # 月末若是周末/节假日，往前找到最近一个有数据的交易日（最多回溯 12 天）
            data, day = [], None
            probe = mp.end_time.date()
            for _ in range(12):
                cand = str(probe)
                with contextlib.redirect_stdout(buf):
                    rs = bs.query_all_stock(day=cand)
                    tmp = []
                    while rs.error_code == "0" and rs.next():
                        tmp.append(rs.get_row_data())
                if rs.error_code == "0" and tmp:
                    data, day = tmp, cand
                    break
                probe = probe - pd.Timedelta(days=1)
                if isinstance(probe, pd.Timestamp):
                    probe = probe.date()
            if not data:
                print(f"[跳过] {mp}: 12 天内无交易日数据", file=sys.stderr)
                continue
            seen = 0
            for r in data:
                if len(r) < 3:
                    continue
                raw_code, status, name = str(r[0]), str(r[1]), str(r[2])
                if status != "1":            # 停牌/退市当日不算
                    continue
                c = _clean(raw_code, name)
                if c:
                    rows.append((str(mp), c))
                    seen += 1
            print(f"{mp} ({day}): {seen} 只", file=sys.stderr)
    finally:
        with contextlib.redirect_stdout(buf):
            bs.logout()

    if not rows:
        raise SystemExit("没有取到任何成员数据")

    df = pd.DataFrame(rows, columns=["ym", "code"]).drop_duplicates()
    os.makedirs(os.path.join(HERE, a.outdir), exist_ok=True)
    df.to_csv(os.path.join(HERE, a.outdir, "pit_membership.csv.gz"),
              index=False, compression="gzip", encoding="utf-8")
    codes = sorted(df["code"].unique())
    with open(os.path.join(HERE, a.outdir, "pit_codes.txt"), "w", encoding="utf-8") as f:
        f.write(",".join(codes))
    print(f"月份 {df['ym'].nunique()} 个，代码并集 {len(codes)} 只，成员行 {len(df)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
