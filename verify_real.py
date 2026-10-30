"""CI 环境下的数据层：把 data/min30/<code>.csv.gz 读成 (bars, df)。

本地版 verify_real.py 读的是策略目录的 pkl 缓存；CI 仓库里没有那个缓存，
所以这里提供**同名同接口**的 shim，让 结构闸门回测.py 一行不改就能跑。
"""
from __future__ import annotations

import gzip
import os

import pandas as pd

import chan_law as L

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "min30")


def data_path(code: str) -> str:
    return os.path.join(DATA, f"{code}.csv.gz")


def has_code(code: str) -> bool:
    return os.path.exists(data_path(code))


def load_bars(code: str, limit: int | None = None):
    """返回 (bars, df)。df 需要含 date/open/high/low/close 列（回测脚本要用 open 成交）。"""
    path = data_path(code)
    if not os.path.exists(path):
        return None, None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        df = pd.read_csv(fh, skip_blank_lines=True)
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    # 数据先验：零价/负价整行剔除（601088 历史上出现过 80 根全 0 的行情洞）
    ok = (df[["open", "high", "low", "close"]] > 0).all(axis=1)
    df = df[ok].reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    if limit:
        df = df.iloc[-int(limit):].reset_index(drop=True)
    bars = [L.Bar(t=i, high=float(df["high"].iloc[i]), low=float(df["low"].iloc[i]),
                  open=float(df["open"].iloc[i]), close=float(df["close"].iloc[i]))
            for i in range(len(df))]
    return bars, df
