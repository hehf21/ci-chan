"""
信号生成器（信号层，与回测执行解耦）
====================================
职责单一：把某一级别的结构算清楚，**只输出信号表**，不碰资金、仓位、成交。
一行 = 一个"首次出现"的买卖点信号。

为什么要拆开（用户要求）：
  结构计算是最贵的一步（每只票 3~15 分钟），而资金规则经常要改。
  拆开后：结构只算一次 → 回测层可以反复换风控/仓位规则，几秒钟出结果，
  也避免"回测代码里塞满结构逻辑、改一处两处都要重跑"。

输出列：
  ts        信号出现的那根 K 线的收盘时间（结构确认时点，不是价格极值时点）
  code      6 位代码
  freq      "30" / "5"
  kind      B1/B2/B3/S1/S2/S3
  px        买卖点价格（结构价位）
  stop      结构失效价
  bar_idx   该 bar 在原始序列里的绝对下标（回测层用来定位成交时点）
  evidence  判定依据（人工复核用）

用法：
  python signal_gen.py --codes 600519,600036 --freq 30 --start 2024-01-01 --end 2026-10-29 --window 3000
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import chan_data
from chan_strict import pipeline as PL
from chan_strict import strategy as ST

OUT = os.path.join(HERE, "signals")


def load_bars(code: str, freq: str, start: str, end: str) -> pd.DataFrame | None:
    if str(freq) == "5":
        import chan_min5
        return chan_min5.get_min5(code, start, end)
    if str(freq) in ("d", "day", "daily"):
        return chan_data.get_daily(code, start, end)
    return chan_data.get_min30(code, start, end)


def generate(code: str, freq: str, start: str, end: str, window: int,
             min_bars: int = 800, verbose: bool = True,
             max_age_bars: int = 40,
             require_end_peak: bool = False, allow_panzheng_b1: bool = False,
             require_above_ma: int = 0, step: int = 1,
             require_daily_up: bool = False, daily_step_bars: int = 20,
             daily_window: int = 800, require_vol_ratio: float = 0.0,
             vol_short: int = 5, vol_long: int = 20) -> pd.DataFrame:
    """对一只票逐 bar 扫结构，输出首次出现的买卖点。

    require_end_peak   : 工程增补 —— 笔端点须为区间极值（对齐 chan.py 口径，默认关）
    allow_panzheng_b1  : 口径开关 —— 一类买卖点是否接受盘整背驰（对应 chan.py 的 1p，默认关）
    require_vol_ratio  : 第 7/8 课「量能突破」——买点要求近 vol_short 日平均成交量
                         达到前 vol_long 日均量的 require_vol_ratio 倍（0=关闭）。
                         原文：「周线上的成交量压力线的突破」才算能搞。
                         量能按日聚合、只用当日及之前的数据，无未来函数。
    require_daily_up   : 区间套（第 27/17 课）——只在**日线级别向上**时发买点。
                         日线状态用日线线段方向判定（比 MA250 更贴结构），
                         且**每 daily_step_bars 根 30 分钟 bar 重算一次**，避免用未来数据。
    step               : 扫描步长（根）。日线级别用 5，避免逐根重算 2500 根窗口。
                         30 分钟级别保持 1（信号粒度就是 bar）。
    require_above_ma   : 第 8 课「能搞」标准 —— 只在收盘价站上 N 日线时才发买点。
                         原文：「不能搞的就无论发生什么情况都不能搞」，用 250 日线（年线）卡门槛。
                         卖点不受此限（持仓该走就走）。0=关闭。
    """
    bars = load_bars(code, freq, start, end)
    if bars is None or len(bars) < min_bars:
        print(f"[跳过] {code} {freq}分钟 数据不足")
        return pd.DataFrame()
    bars = bars.copy()
    bars["date"] = pd.to_datetime(bars["date"])
    bars = bars.sort_values("date").reset_index(drop=True)
    ohlc = bars[["open", "high", "low", "close"]]

    # ---- 第 8 课「能搞」：N 日线过滤 ----
    ma_series = None
    if require_above_ma and require_above_ma > 0:
        _daily = (bars.set_index("date")["close"]
                  .resample("1D").last().dropna())
        _ma = _daily.rolling(int(require_above_ma)).mean()
        _df = pd.DataFrame({"close": _daily, "ma": _ma}).dropna()
        _df = _df.reindex(bars["date"], method="ffill")     # 按 bar 时间对齐
        ma_series = _df.reset_index(drop=True)

    # ---- 区间套：日线级别的方向状态（滚动重算，无未来函数）----
    daily_state = None
    if require_daily_up:
        try:
            _dd = chan_data.get_daily(code, start, end)
            _dd = _dd.copy()
            _dd["date"] = pd.to_datetime(_dd["date"])
            _dd = _dd.sort_values("date").reset_index(drop=True)
        except Exception:
            _dd = None
        if _dd is not None and len(_dd) > 200:
            _ohlc_d = _dd[["open", "high", "low", "close"]]
            # 对每根 30 分钟 bar 找它对应的日线可见数据末尾位置
            _dpos = _dd["date"].searchsorted(bars["date"], side="right")
            daily_state = [None] * len(bars)
            _last_dir = 0
            for _i in range(min_bars, len(bars)):
                _k = int(_dpos[_i])
                # 每 daily_step_bars 根重算一次，取「当时可见」的日线窗口
                if _i == min_bars or (_i - min_bars) % max(1, int(daily_step_bars)) == 0:
                    if _k >= 200:
                        _lo = max(0, _k - int(daily_window))
                        _w = _ohlc_d.iloc[_lo:_k].reset_index(drop=True)
                        try:
                            _r = PL.analyze(_w, max_level=1)
                            _segs = _r.get("segs") or []
                            _last_dir = int(_segs[-1]["dir"]) if _segs else 0
                        except Exception:
                            pass
                daily_state[_i] = _last_dir

    # ---- 第 7/8 课：量能突破（按日聚合，只用当日及之前）----
    vol_ratio = None
    if require_vol_ratio and require_vol_ratio > 0 and "volume" in bars.columns:
        _tmp = bars[["date", "volume"]].copy()
        _tmp["d"] = _tmp["date"].dt.normalize()
        _dv = _tmp.groupby("d")["volume"].sum()
        _ma_s = _dv.rolling(max(1, int(vol_short))).mean()
        _ma_l = _dv.rolling(max(2, int(vol_long))).mean().shift(1)   # shift(1)：不含当日
        _ratio_d = (_ma_s / _ma_l).replace([float("inf"), float("-inf")], None)
        _ratio_by_day = _ratio_d.to_dict()
        vol_ratio = [_ratio_by_day.get(d.normalize()) for d in bars["date"]]

    seen: set = set()
    rows: list[dict] = []
    t0 = time.time()
    for i in range(min_bars, len(bars), max(1, int(step))):
        lo = max(0, i + 1 - window)
        win = ohlc.iloc[lo : i + 1].reset_index(drop=True)
        try:
            res = PL.analyze(win, max_level=1, require_end_peak=require_end_peak,
                             allow_panzheng_b1=allow_panzheng_b1)
        except Exception:
            continue
        res["n_raw"] = len(win)
        for p in res["points"]:
            # 关键：只发「买点价所在 bar 足够新」的信号。
            # 结构有时会在价格过去几百根 bar 之后才把这个点算出来（实测均值 462 根、最长 2672 根），
            # 若照发，回测/实盘会拿几十天前的价格和止损位去下单（22.4% 的建仓价直接落在止损位下方）。
            age = i - (lo + int(p["raw"]))
            if age > max_age_bars:
                continue
            k = (p["kind"], lo + int(p["raw"]), round(float(p["px"]), 4))
            if k in seen:
                continue
            # 第 7/8 课：买点要求量能放大；卖点一律照发
            if vol_ratio is not None and str(p["kind"]).startswith("B"):
                _vr = vol_ratio[i]
                try:
                    if _vr is None or float(_vr) < float(require_vol_ratio):
                        continue
                except Exception:
                    continue
            # 区间套：买点要求日线级别此刻向上；卖点一律照发
            if daily_state is not None and str(p["kind"]).startswith("B"):
                if int(daily_state[i] or 0) <= 0:
                    continue
            # 第 8 课：买点要求已站上 N 日线；卖点一律照发
            if ma_series is not None and str(p["kind"]).startswith("B"):
                try:
                    _row = ma_series.iloc[i]
                    if pd.isna(_row["ma"]) or float(_row["close"]) < float(_row["ma"]):
                        continue
                except Exception:
                    pass
            seen.add(k)
            rows.append({
                "ts": bars["date"].iloc[i],
                "code": code, "freq": str(freq), "kind": p["kind"],
                "px": round(float(p["px"]), 4), "stop": round(float(p["stop"]), 4),
                "bar_idx": lo + int(p["raw"]),
                "zs_in_trend": p.get("zs_in_trend"),
                "zs_nseg": p.get("zs_nseg"),
                "trend_idx": p.get("trend_idx"),
                "age_bars": age,
                "confirm_idx": i,
                "evidence": (p.get("evidence") or "")[:200],
            })
        if verbose and (i - min_bars) % 1000 == 0:
            print(f"  {code} {freq}分钟 进度 {i}/{len(bars)}  信号 {len(rows)}  {time.time()-t0:.0f}s", flush=True)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["ts", "code", "kind"]).reset_index(drop=True)
    print(f"[完成] {code} {freq}分钟 {len(bars)} 根 → {len(df)} 个信号，耗时 {time.time()-t0:.0f}s", flush=True)
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True)
    ap.add_argument("--freq", default="30", choices=["5", "30"])
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-10-29")
    ap.add_argument("--window", type=int, default=3000)
    ap.add_argument("--tag", default="")
    ap.add_argument("--end-peak", action="store_true", help="笔端点须为区间极值（工程增补）")
    ap.add_argument("--panzheng-b1", action="store_true", help="一类买卖点接受盘整背驰（放宽口径 1p）")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    frames = []
    for c in [x.strip() for x in a.codes.split(",") if x.strip()]:
        df = generate(c, a.freq, a.start, a.end, a.window,
                      require_end_peak=a.end_peak, allow_panzheng_b1=a.panzheng_b1)
        if not df.empty:
            frames.append(df)
    if not frames:
        print("没有任何信号")
        return 1
    allsig = pd.concat(frames, ignore_index=True)
    tag = a.tag or f"{a.freq}min_w{a.window}"
    path = os.path.join(OUT, f"signals_{tag}.csv")
    allsig.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\n信号表：{path}  共 {len(allsig)} 行")
    print(allsig["kind"].value_counts().to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
