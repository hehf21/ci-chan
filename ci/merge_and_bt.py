"""CI：合并各票信号 → 同口径回测 → 输出统计。

用法：python ci/merge_and_bt.py --variant ep1
输出：out/ci_<variant>_stats.json、out/ci_<variant>_equity.csv、out/ci_<variant>_trades.csv
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "ci"))

import patch_data  # noqa: E402

patch_data.install()

import portfolio_bt as PB  # noqa: E402

BASE_CFG = {
    "trailing_stop": False,
    "trailing_mode": "off",
    "panzheng_exit": False,
    "regime_ma": 0,
    "time_exit_bars": 0,
    "require_daily_up": False,
}


def round_trips(tr: pd.DataFrame) -> pd.DataFrame:
    if tr.empty:
        return pd.DataFrame(columns=["code", "entry", "exit", "ret"])
    open_lot, rows = {}, []
    for r in tr.itertuples():
        if r.side == "buy":
            open_lot[r.code] = r
        else:
            b = open_lot.pop(r.code, None)
            if b is not None and b.price > 0:
                rows.append({"code": r.code, "entry": b.date, "exit": r.date,
                             "ret": (r.price / b.price - 1) * 100 - 0.3})
    return pd.DataFrame(rows)


def seg(eq, rts, d0, d1, name):
    e = eq[(eq["date"] >= d0) & (eq["date"] <= d1)]
    t = rts[(rts["exit"] >= d0) & (rts["exit"] <= d1)] if not rts.empty else rts
    if e.empty:
        return {"name": name, "ret": None}
    ret = e["equity"].iloc[-1] / e["equity"].iloc[0] - 1
    dd = ((e["equity"] - e["equity"].cummax()) / e["equity"].cummax()).min()
    return {"name": name, "ret": round(ret * 100, 2), "dd": round(dd * 100, 2),
            "trades": int(len(t)),
            "win": round((t["ret"] > 0).mean() * 100, 1) if len(t) else None,
            "avg_win": round(t.loc[t["ret"] > 0, "ret"].mean(), 2) if len(t) and (t["ret"] > 0).any() else None,
            "avg_loss": round(t.loc[t["ret"] <= 0, "ret"].mean(), 2) if len(t) and (t["ret"] <= 0).any() else None}




def buy_hold_benchmark(codes, d0, d1) -> dict:
    """同池等权买入持有：用 merge 阶段已有的 data/min30/*.csv.gz，不额外取数。

    没有基准就没法判断"策略赚的是能力还是市场"。这一步必须做。
    """
    import glob
    import gzip

    data_dir = os.path.join(HERE, "data", "min30")
    rets, used = [], 0
    for c in sorted(set(codes)):
        path = None
        for sub in ("min30", "daily", "d", "min5"):
            cand = os.path.join(HERE, "data", sub, f"{c}.csv.gz")
            if os.path.exists(cand):
                path = cand
                break
        if path is None:
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                df = pd.read_csv(f)
        except Exception:
            continue
        if "date" not in df.columns or "close" not in df.columns or len(df) < 10:
            continue
        df["date"] = pd.to_datetime(df["date"])
        m = (df["date"] >= pd.Timestamp(d0)) & (df["date"] <= pd.Timestamp(d1))
        s_ = df.loc[m, "close"]
        if len(s_) < 2:
            continue
        rets.append(float(s_.iloc[-1]) / float(s_.iloc[0]) - 1)
        used += 1
    if not rets:
        return {"name": "同池买入持有", "ret": None, "codes": 0}
    avg = sum(rets) / len(rets)
    win = sum(1 for r in rets if r > 0) / len(rets) * 100
    return {"name": "同池买入持有", "ret": round(avg * 100, 2),
            "win": round(win, 1), "codes": used}

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="ep1", choices=["ep0", "ep1"])
    ap.add_argument("--freq", default="30", help="30=30分钟 / d=日线（须与信号一致）")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-10-29")
    ap.add_argument("--trade-start", default="2020-07-01")
    ap.add_argument("--max-positions", type=int, default=3,
                    help="同时持仓上限（默认3；调大能把更多信号用起来）")
    ap.add_argument("--max-weight", type=float, default=0.30,
                    help="单票权重上限")
    ap.add_argument("--min-zs-seg", type=int, default=0,
                    help="第54课：只做中枢段数 >= N 的买卖点（级别更大，0=关）")
    ap.add_argument("--only-first-zs", action="store_true",
                    help="第79课：只做本走势第一个中枢的第三类买点")
    ap.add_argument("--kinds", default="",
                    help="只保留这些买卖点类型，逗号分隔，如 B3,S1,S2,S3；空=全部")
    a = ap.parse_args()

    parts = os.path.join(HERE, "signals_parts")
    frames = []
    for f in sorted(os.listdir(parts)):
        if f.startswith(a.variant) and f.endswith(".csv"):
            frames.append(pd.read_csv(os.path.join(parts, f), dtype={"code": str, "kind": str, "freq": str}))
    if not frames:
        raise SystemExit(f"没有找到 {a.variant} 的信号分片")
    sig = pd.concat(frames, ignore_index=True)
    sig["code"] = sig["code"].astype(str).str.zfill(6)

    before = len(sig)
    # ---- 第79课：只介入第一个中枢的第三类买点 ----
    # 原文：尽量只介入第一个中枢的第三类买点，因为第二个中枢以后形成大级别中枢的概率急促加大。
    # 卖点不筛（持仓遇到卖点就要走）。
    if a.only_first_zs and "zs_in_trend" in sig.columns:
        keep = (~sig["kind"].eq("B3")) | (pd.to_numeric(sig["zs_in_trend"], errors="coerce") == 1)
        sig = sig[keep].copy()
        print(f"[79课过滤] B3 只保留本走势第1个中枢：{before} -> {len(sig)} 条")

    # ---- 第 54 课：按中枢级别（段数）筛选 ----
    if a.min_zs_seg and a.min_zs_seg > 0 and "zs_nseg" in sig.columns:
        _n0 = len(sig)
        _ok = pd.to_numeric(sig["zs_nseg"], errors="coerce")
        sig = sig[_ok.isna() | (_ok >= a.min_zs_seg)].copy()
        print(f"[级别过滤] 中枢段数>={a.min_zs_seg}：{_n0} -> {len(sig)} 条")

    # ---- 按买卖点类型筛 ----
    if a.kinds:
        want = {k.strip().upper() for k in a.kinds.split(",") if k.strip()}
        sig = sig[sig["kind"].str.upper().isin(want)].copy()
        print(f"[类型过滤] {sorted(want)}：{before} -> {len(sig)} 条")

    if sig.empty:
        raise SystemExit("过滤后没有信号，检查口径参数")

    cfg = dict(BASE_CFG)
    cfg["max_positions"] = a.max_positions
    cfg["max_weight_per_stock"] = a.max_weight
    res = PB.run(sig, a.freq, a.start, a.end, 1_000_000.0, cfg=cfg, trade_start=a.trade_start)
    eq, tr = res["equity"], res["trades"]
    eq["date"] = pd.to_datetime(eq["date"])
    if not tr.empty:
        tr["date"] = pd.to_datetime(tr["date"])
    rts = round_trips(tr)

    out = os.path.join(HERE, "out")
    os.makedirs(out, exist_ok=True)
    eq.to_csv(os.path.join(out, f"ci_{a.variant}_equity.csv"), index=False, encoding="utf-8-sig")
    tr.to_csv(os.path.join(out, f"ci_{a.variant}_trades.csv"), index=False, encoding="utf-8-sig")
    rts.to_csv(os.path.join(out, f"ci_{a.variant}_roundtrips.csv"), index=False, encoding="utf-8-sig")

    summary = {
        "variant": a.variant,
        "signals": int(len(sig)),
        "codes": int(sig["code"].nunique()),
        "kinds": sig["kind"].value_counts().to_dict(),
        "segs": [
            seg(eq, rts, pd.Timestamp(a.trade_start), pd.Timestamp("2023-12-31"), "样本内"),
            seg(eq, rts, pd.Timestamp("2024-01-01"), pd.Timestamp(a.end), "样本外"),
            seg(eq, rts, pd.Timestamp(a.trade_start), pd.Timestamp(a.end), "全段"),
            # 基准：同池等权买入持有（同区间、同股票池）——判断"赚的是能力还是市场"
            buy_hold_benchmark(sorted(set(sig["code"])), a.trade_start, a.end),
        ],
    }
    with open(os.path.join(out, f"ci_{a.variant}_stats.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
