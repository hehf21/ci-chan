"""合并 80 只票的逐 bar 状态 → 横截面 IC（状态量有没有信息）

输入：_art/**/<code>.csv.gz（state_one.py 产出）
输出：ci/因子-汇总.md、ci/因子-汇总.json

口径（写死，跑之前定）：
  · 前视 H=20 根（30 分钟，约 2.5 个交易日），时间戳按**非重叠**取样（每 H 根一次）
  · 因子：seg_dir / zs_width / pos_in_zs / dist_zd / dist_zg / broken / zs_age / seg_age
  · IC = 当期横截面 Spearman(因子, 未来 H 根收益)；报 IC 均值、t 值、|IC|>0.02 占比
  · 另报分年度 IC（看稳定性）与二分组的收益差（看经济含义）
  · 零价/缺数据的样本点整点剔除
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import statistics as st

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
H = 20
FACTORS = ["seg_dir", "zs_width", "pos_in_zs", "dist_zd", "dist_zg", "broken", "zs_age", "seg_age"]


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(a, b):
    if len(a) < 5:
        return None
    ra, rb = rank(a), rank(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return None if (va == 0 or vb == 0) else cov / (va * vb)


def load(indir):
    fames = {}
    for f in sorted(glob.glob(os.path.join(indir, "**", "*.csv.gz"), recursive=True)):
        code = os.path.basename(f).split(".")[0]
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            d = pd.read_csv(fh)
        if len(d) < 1000:
            continue
        fames[code] = d.set_index("dt")
    return fames


def main() -> int:
    ap_dir = os.environ.get("ART_DIR", "_art")
    fames = load(ap_dir)
    print(f"读入 {len(fames)} 只票")
    min_codes = int(os.environ.get("MIN_CODES", "10"))
    if len(fames) < min_codes:
        raise SystemExit(f"票数太少（<{min_codes}），先看各 job 的日志")

    idx = sorted(set.intersection(*[set(d.index) for d in fames.values()]))
    print(f"共同时间戳 {len(idx)} 个（约 {len(idx)/8:.0f} 个交易日）")

    closes = pd.DataFrame({c: d.loc[idx, "close"].astype(float) for c, d in fames.items()}, index=idx)
    fwd = closes.shift(-H) / closes - 1.0

    recs = {f: [] for f in FACTORS}
    by_year = {f: {} for f in FACTORS}
    for i in range(0, len(idx) - H, H):
        t = idx[i]
        fr = fwd.loc[t].dropna()
        fr = fr[fr.abs() < 1.0]                       # 挡掉零价/异常
        if len(fr) < 10:
            continue
        year = t[:4]
        for f in FACTORS:
            vals, ys = [], []
            for c in fr.index:
                v = fames[c].loc[t, f]
                if pd.isna(v):
                    continue
                vals.append(float(v))
                ys.append(float(fr[c]))
            ic = spearman(vals, ys)
            if ic is None:
                continue
            recs[f].append(ic)
            by_year[f].setdefault(year, []).append(ic)

    print(f"\n{'因子':<12}{'IC均值':>9}{'标准差':>9}{'t值':>7}{'|IC|>0.02':>11}{'样本':>7}")
    out = {"n_codes": len(fames), "n_ts": len(idx), "horizon": H, "factors": {}}
    for f in FACTORS:
        v = recs[f]
        if not v:
            continue
        m, sd = st.mean(v), st.pstdev(v)
        t = m / (sd / len(v) ** 0.5) if sd else 0.0
        share = sum(1 for x in v if abs(x) > 0.02) / len(v)
        out["factors"][f] = {"ic_mean": m, "ic_std": sd, "t": t, "share": share, "n": len(v),
                            "by_year": {y: st.mean(xs) for y, xs in by_year[f].items()}}
        print(f"{f:<12}{m:>9.4f}{sd:>9.4f}{t:>7.2f}{share:>10.1%}{len(v):>7}")

    # 分组（二分）收益差
    print(f"\n{'因子':<12}{'低组':>9}{'高组':>9}{'高-低':>9}")
    for f in FACTORS:
        lo, hi = [], []
        for i in range(0, len(idx) - H, H):
            t = idx[i]
            fr = fwd.loc[t].dropna()
            fr = fr[fr.abs() < 1.0]
            pairs = []
            for c in fr.index:
                v = fames[c].loc[t, f]
                if pd.isna(v):
                    continue
                pairs.append((float(v), float(fr[c])))
            if len(pairs) < 8:
                continue
            pairs.sort()
            m = len(pairs) // 2
            lo.append(sum(x[1] for x in pairs[:m]) / m)
            hi.append(sum(x[1] for x in pairs[m:]) / (len(pairs) - m))
        if lo and hi:
            out["factors"][f]["bucket_low"] = st.mean(lo)
            out["factors"][f]["bucket_high"] = st.mean(hi)
            out["factors"][f]["bucket_spread"] = st.mean(hi) - st.mean(lo)
            print(f"{f:<12}{st.mean(lo):>9.3%}{st.mean(hi):>9.3%}{st.mean(hi) - st.mean(lo):>9.3%}")

    os.makedirs(HERE, exist_ok=True)
    with open(os.path.join(HERE, "因子-汇总.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    lines = ["# 状态量横截面 IC（CI 全池）", "",
             f"票数 {len(fames)}；共同时间戳 {len(idx)}；前视 {H} 根（非重叠）；"
             f"判据：|IC|<0.02 噪声，0.02~0.05 弱信号，>0.05 有意义", "",
             "| 因子 | IC均值 | t值 | 高-低组收益差 | 逐年IC |", "|---|---|---|---|---|"]
    for f, d in out["factors"].items():
        yr = " / ".join(f"{y}:{v:+.3f}" for y, v in sorted(d.get("by_year", {}).items()))
        sp = "—" if "bucket_spread" not in d else f"{d['bucket_spread']:+.3%}"
        lines.append(f"| {f} | {d['ic_mean']:+.4f} | {d['t']:+.2f} | {sp} | {yr} |")
    with open(os.path.join(HERE, "因子-汇总.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n已写 ci/因子-汇总.md / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
