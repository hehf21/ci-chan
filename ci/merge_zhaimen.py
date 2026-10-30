"""把 20 票分片跑出来的 结果-闸门-<code>.json 合并成一张汇总表。

用法（CI）：python ci/merge_zhaimen.py --indir _art
输出：ci/汇总-闸门.json、ci/汇总-闸门.md
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--indir", default="_art")
    ap.add_argument("--outdir", default="ci")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.indir, "**", "结果-闸门-*.json"), recursive=True))
    # 按 code 去重（同一票可能被上传多次）
    per_code = {}
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        for code in d.get("signals", {}):
            per_code[code] = d
    print(f"读到 {len(files)} 个分片，覆盖 {len(per_code)} 只票：{','.join(sorted(per_code))}")
    if not per_code:
        # 静默「成功但 0 条」最危险：上一轮 20 个 job 全绿、结果全是空跑，就是这么漏过去的
        raise SystemExit("没有读到任何分片：先看各 job 的 log_*.txt，不要相信 job 的绿色状态")

    arms = ["naive_known", "naive_t"]
    arms2 = ["A", "B", "C"]
    rows = {}
    for a in arms:
        for b in arms2:
            agg = {}
            for seg in ("all", "is", "oos"):
                rets, mdds, rounds, wins, gates = [], [], [], [], []
                for code, d in per_code.items():
                    s = d["arms"].get(a, {}).get(b, {}).get(seg, {})
                    if not s:
                        continue
                    rets.append(s.get("ret"))
                    mdds.append(s.get("mdd"))
                    rounds.append(s.get("rounds"))
                    wins.append(s.get("win_rate"))
                    gates.append(d["arms"].get(a, {}).get(b, {}).get("reject", {}).get("gate_blocked", 0))
                agg[seg] = {"ret_mean": _mean(rets), "ret_median": _median(rets),
                            "mdd_mean": _mean(mdds), "rounds_mean": _mean(rounds),
                            "win_mean": _mean(wins), "n_stock": len(rets),
                            "gate_blocked": sum(gates)}
            rows[f"{a}/{b}"] = agg

    bench = {}
    for seg in ("all", "is", "oos"):
        vals = [_mean([d["benchmark"][seg]["per_stock"].get(c)]) for c, d in per_code.items()
                if d.get("benchmark", {}).get(seg, {}).get("per_stock", {}).get(c) is not None]
        bench[seg] = {"mean": _mean(vals), "median": _median(vals), "n": len(vals)}

    # 信号统计
    lag, gap, nbuy, nsell, kinds = [], [], 0, 0, {}
    for code, d in per_code.items():
        s = d["signals"][code]
        lag.append(s.get("lag_median"))
        gap.append(s.get("gap_median"))
        nbuy += s.get("buy", 0)
        nsell += s.get("sell", 0)
        for k, v in s.get("kinds", {}).items():
            kinds[k] = kinds.get(k, 0) + v

    # 自检
    checks_bad = 0
    for code, d in per_code.items():
        for key, c in d.get("checks", {}).items():
            if not c.get("identity_ok", True) or c.get("t1_violations", 0) > 0 or c.get("stop_above_entry", 0) > 0:
                checks_bad += 1

    res = {"n_code": len(per_code), "codes": sorted(per_code), "arms": rows,
           "benchmark": bench, "signals": {"buy": nbuy, "sell": nsell, "kinds": kinds,
                                           "lag_median_mean": _mean(lag), "gap_median_mean": _mean(gap)},
           "checks_bad": checks_bad}

    os.makedirs(args.outdir, exist_ok=True)
    jp = os.path.join(args.outdir, "汇总-闸门.json")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)

    L = []
    L.append("# 汇总：结构闸门与止损口径对照（CI 20 票）")
    L.append("")
    L.append(f"票数 {len(per_code)}；信号 买 {nbuy} / 卖 {nsell}；"
             f"确认滞后中位（各票中位数的均值）{(_mean(lag) or 0):.0f} 根；"
             f"入场缺口中位 {(_mean(gap) or 0):.2%}")
    L.append("")
    L.append("| 臂 | 全段收益 | 样本内 | 样本外 | 回撤(全段) | 回合数 | 胜率 | 闸门拦截 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for key, agg in rows.items():
        a, seg = agg["all"], agg
        L.append(f"| {key} | {_pct(seg['all']['ret_mean'])} | {_pct(seg['is']['ret_mean'])} | "
                 f"{_pct(seg['oos']['ret_mean'])} | {_pct(seg['all']['mdd_mean'])} | "
                 f"{_num(seg['all']['rounds_mean'])} | {_pct(seg['all']['win_mean'])} | {seg['all']['gate_blocked']} |")
    L.append("")
    L.append("## 基准（同池等权买入持有）")
    L.append("")
    for seg in ("all", "is", "oos"):
        L.append(f"- {seg}: 均值 {_pct(bench[seg]['mean'])} / 中位 {_pct(bench[seg]['median'])}（n={bench[seg]['n']}）")
    L.append("")
    L.append(f"## 自检：身份恒等式/ T+1 / 止损高于成交价的违规项 {checks_bad} 处（应为 0）")
    L.append("")
    L.append("口径提醒：`naive_known` = 按 known_t 进场（可执行口径）；`naive_t` = 按价格时点 t 进场"
             "（含未来函数，只能用来量化偏差，不能当结论）。walk-forward 未在本轮矩阵里跑。")
    mp = os.path.join(args.outdir, "汇总-闸门.md")
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\n已写 {jp} / {mp}")
    return 0


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _pct(x):
    return "—" if x is None else f"{x:+.2%}"


def _num(x, nd=1):
    return "—" if x is None else f"{x:.{nd}f}"


if __name__ == "__main__":
    raise SystemExit(main())
