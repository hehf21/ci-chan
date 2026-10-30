"""CI 数据补丁：把 chan_data.get_min30 换成读仓库里的 gz CSV。

为什么不直接让 CI 联网取数：baostock 在境外机房能否连通未验证，
而 30 分钟数据已按前复权固化在 data/min30/*.csv.gz（20 只票，6.3MB），离线可复现。
本地跑不受影响——本模块只在 CI 脚本里显式 import。
"""
from __future__ import annotations

import gzip
import os

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data", "min30")


def load_min30(code, start=None, end=None, adjust=None):
    path = os.path.join(DATA, f"{code}.csv.gz")
    if not os.path.exists(path):
        raise FileNotFoundError(f"没有该票的数据文件：{path}")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        df = pd.read_csv(fh)
    df["date"] = pd.to_datetime(df["date"])
    if start is not None:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["date"] <= pd.Timestamp(end) + pd.Timedelta(days=1)]
    return df.sort_values("date").reset_index(drop=True)


def install() -> None:
    import chan_data

    chan_data.get_min30 = load_min30
