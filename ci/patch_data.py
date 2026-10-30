"""CI 数据补丁：把 chan_data.get_min30 换成「仓库 gz CSV → 缺失时从 baostock 现取」。

两种模式：
- 仓库里有 data/min30/<code>.csv.gz → 直接读（离线、可复现、跑 20 只老票用这个）
- 没有 → 用 ci/fetch_bars.py 现场取（沪深300 扩样本用），取到后写回 data/min30 供同 job 复用
"""
from __future__ import annotations

import gzip
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data", "min30")
sys.path.insert(0, os.path.join(HERE, "ci"))


def _read_gz(path: str) -> pd.DataFrame:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        df = pd.read_csv(fh)
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_min30(code, start=None, end=None, adjust=None):
    path = os.path.join(DATA, f"{code}.csv.gz")
    if os.path.exists(path):
        df = _read_gz(path)
    else:
        import fetch_bars

        df = fetch_bars.fetch_min30(str(code), start or "2020-01-01", end)
        if df is None or df.empty:
            raise FileNotFoundError(f"取不到 {code} 的数据")
        fetch_bars.save_gz(df, path)
        df["date"] = pd.to_datetime(df["date"])
    if start is not None:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["date"] <= pd.Timestamp(end) + pd.Timedelta(days=1)]
    return df.sort_values("date").reset_index(drop=True)


def install() -> None:
    import chan_data

    chan_data.get_min30 = load_min30
