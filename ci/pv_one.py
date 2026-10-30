# -*- coding: utf-8 -*-
"""单票价量因子（CI 矩阵用）
==========================
每只票一个 job：取日线（含换手率），算 6 个经典价量因子，落到 pv_parts/<code>.csv。

为什么用这些因子：
  · 动量/反转/波动/换手/量能是 A 股公开数据里最稳定的一批异象，
    且完全不需要额外数据源（baostock 日线自带 turn 换手率、pctChg 涨跌幅）。
  · 与缠论结构择时的区别：这里是**截面**用法——每月对所有票排序取前 N，
    而不是逐票等某个买点出现。

输出列（顺序写死）：
  code,date,close,f_mom20,f_mom60,f_rev5,f_vol20,f_turn20,f_amt20
  f_mom20/f_mom60/f_rev5 单位=小数收益；f_vol20=日收益标准差；
  f_turn20=20日平均换手率(%)；f_amt20=log(20日平均成交额)

无未来函数：所有因子在 T 日只用 T 日及之前的数据，月末调仓时取月末那天的值。
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


def bs_code(code: str) -> str:
    c = str(code).zfill(6)
    return ("sh." if c.startswith(("6", "9")) else "sz.") + c


def fetch_daily(code: str, start: str, end: str) -> pd.DataFrame | None:
    import baostock as bs

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lg = bs.login()
        if lg.error_code != "0":
            print(f"login fail {lg.error_msg}")
            return None
        rows = []
        try:
            rs = bs.query_history_k_data_plus(
                bs_code(code),
                "date,close,pctChg,turn,amount",
                start_date=start, end_date=end,
                frequency="d", adjustflag="2",      # 2=前复权
            )
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "close", "pctChg", "turn", "amount"])
    for c in ("close", "pctChg", "turn", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)


def add_factors(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    c = d["close"]
    d["f_mom20"] = c / c.shift(20) - 1.0
    d["f_mom60"] = c / c.shift(60) - 1.0
    d["f_rev5"] = c / c.shift(5) - 1.0
    d["f_vol20"] = d["pctChg"].rolling(20).std()
    d["f_turn20"] = d["turn"].rolling(20).mean()
    d["f_amt20"] = d["amount"].rolling(20).mean().apply(lambda x: __import__("math").log(x) if x and x > 0 else float("nan"))
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--outdir", default="pv_parts")
    a = ap.parse_args()

    df = fetch_daily(a.code, a.start, a.end)
    if df is None or len(df) < 120:
        print(f"SKIP {a.code}: 数据不足")
        return 0

    d = add_factors(df)
    d["code"] = str(a.code).zfill(6)
    cols = ["code", "date", "close", "f_mom20", "f_mom60", "f_rev5",
            "f_vol20", "f_turn20", "f_amt20"]
    out = d[cols].dropna(subset=["f_mom60"])

    os.makedirs(os.path.join(HERE, a.outdir), exist_ok=True)
    path = os.path.join(HERE, a.outdir, f"{a.code}.csv")
    out.to_csv(path, index=False, encoding="utf-8")
    print(f"{a.code}: {len(out)} 行 -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
