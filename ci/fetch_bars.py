"""从 baostock 取 30 分钟前复权数据（CI 里用；本地有缓存时不走这里）。

字段口径与本地 `chan_data.get_min30` 保持一致：date/open/high/low/close/volume/amount，
时间用 bar 的结束时刻（10:00, 10:30, …），与 baostock 的 time 字段一致。
"""
from __future__ import annotations

import os

import pandas as pd


def _bs_code(code: str) -> str:
    code = str(code).zfill(6)
    return ("sh." if code[0] in "56" else "sz.") + code


def fetch_min30(code: str, start: str = "2020-01-01", end: str | None = None) -> pd.DataFrame:
    import baostock as bs

    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败：{lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            _bs_code(code), "date,time,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency="30", adjustflag="2")
        if rs.error_code != "0":
            raise RuntimeError(f"查询失败 {code}: {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()

    df = pd.DataFrame(rows, columns=["date", "time", "open", "high", "low", "close", "volume", "amount"])
    if df.empty:
        return df
    # time 形如 20200102100000000（17 位）→ 2020-01-02 10:00:00
    t = df["time"].str.slice(0, 12)
    df["date"] = pd.to_datetime(t, format="%Y%m%d%H%M")
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["date", "open", "high", "low", "close", "volume", "amount"]].dropna().reset_index(drop=True)


def save_gz(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    out.to_csv(path, index=False, encoding="utf-8", compression="gzip")


if __name__ == "__main__":
    import sys

    code = sys.argv[1]
    d = fetch_min30(code)
    print(code, len(d), d["date"].min(), d["date"].max())
