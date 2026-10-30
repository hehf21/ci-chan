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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="ep1", choices=["ep0", "ep1"])
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-10-29")
    ap.add_argument("--trade-start", default="2020-07-01")
    ap.add_argument("--max-positions", type=int, default=3,
                    help="同时持仓上限（默认3；调大能把更多信号用起来）")
    ap.add_argument("--max-weight", type=float, default=0.30,
                    help="单票权重上限")
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

    cfg = dict(BASE_CFG)
    cfg["max_positions"] = a.max_positions
    cfg["max_weight_per_stock"] = a.max_weight
    res = PB.run(sig, "30", a.start, a.end, 1_000_000.0, cfg=cfg, trade_start=a.trade_start)
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
        ],
    }
    with open(os.path.join(out, f"ci_{a.variant}_stats.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
