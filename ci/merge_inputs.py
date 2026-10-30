"""CI：合并信号分片 + 中枢分片，供 bt_gg.py 做 A0/A1/ZG 对照。

输入（由矩阵 job 上传的工件解压到）：
    signals_parts/{variant}_{code}.csv
    zs_parts/{code}.csv
输出：
    out/signals_merged.csv      合并去重前的信号表（bt_gg 自带 rule40 去重）
    out/zs_series_gg.csv        逐 bar 中枢序列（含 GG/DD/prev_*）
用法：
    python ci/merge_inputs.py --variant ep1
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="ep1", choices=["ep0", "ep1"])
    ap.add_argument("--signals-dir", default=os.path.join(ROOT, "signals_parts"))
    ap.add_argument("--zs-dir", default=os.path.join(ROOT, "zs_parts"))
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "out"))
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)

    sigs = []
    if os.path.isdir(a.signals_dir):
        for f in sorted(os.listdir(a.signals_dir)):
            if f.startswith(a.variant) and f.endswith(".csv"):
                d = pd.read_csv(os.path.join(a.signals_dir, f),
                                dtype={"code": str, "kind": str, "freq": str})
                if not d.empty:
                    sigs.append(d)
    if not sigs:
        raise SystemExit(f"没有 {a.variant} 信号分片")
    sig = pd.concat(sigs, ignore_index=True)
    sig["code"] = sig["code"].astype(str).str.zfill(6)
    sig = sig.drop_duplicates(subset=["ts", "code", "kind", "px"]).sort_values(["ts", "code", "kind"])
    sp = os.path.join(a.out_dir, "signals_merged.csv")
    sig.to_csv(sp, index=False, encoding="utf-8-sig")
    print(f"[信号] {len(sig)} 条 / {sig['code'].nunique()} 只 → {sp}")
    print(sig["kind"].value_counts().to_string())

    zs = []
    if os.path.isdir(a.zs_dir):
        for f in sorted(os.listdir(a.zs_dir)):
            if f.endswith(".csv"):
                d = pd.read_csv(os.path.join(a.zs_dir, f), dtype={"code": str})
                if not d.empty:
                    zs.append(d)
    if not zs:
        raise SystemExit(f"没有中枢分片（{a.zs_dir}），A1 臂无法运行")
    z = pd.concat(zs, ignore_index=True)
    z["code"] = z["code"].astype(str).str.zfill(6)
    z = z.drop_duplicates(subset=["code", "ts"]).sort_values(["code", "ts"])
    zp = os.path.join(a.out_dir, "zs_series_gg.csv")
    z.to_csv(zp, index=False, encoding="utf-8-sig")
    both = z.dropna(subset=["GG", "prev_GG"]).shape[0]
    print(f"[中枢] {len(z)} 采样点 / {z['code'].nunique()} 只 / 可判趋势点 {both} → {zp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
