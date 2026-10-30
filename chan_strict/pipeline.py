"""
全链路（pipeline）：一段 K 线 → 全部结构 → 买卖点
==================================================
把各严格模块串起来，供策略层与回测调用。

**口径统一的一条硬规矩**：中枢、背驰、买卖点必须建立在**同一份线段列表**上。
`level.build_levels` 内部用的是「已确认线段」，所以这里也用 `confirmed` 过滤后的列表，
否则中枢的段下标与背驰/买卖点用的线段下标会对不上（这类错位极难发现，属于必须防的坑）。
"""

from __future__ import annotations

from . import bei as BEI
from . import level as LV
from . import points as PT
from . import trend as TR
from . import zs as ZS


def analyze(
    df,
    *,
    max_level: int = 2,
    formula: str = "20",
    gap_mode: str = "strict",
    level_rule: str = "37",
    overlap_rule: str = "ZG_ZD",
    require_end_peak: bool = False,
    allow_panzheng_b1: bool = False,
) -> dict:
    """一次性算出所有结构。df 需含 open/high/low/close（升序）。

    require_end_peak:  工程增补 —— 笔端点须为区间极值（chan.py 的 end_is_peak 口径，默认关）
    allow_panzheng_b1: 口径开关 —— 一类买卖点是否接受「盘整背驰」（默认为原严格口径：只认趋势背驰）

    返回：
        levels      多级别结构（L1..Ln）
        segs        本级别使用的**已确认**线段
        zhongshus   中枢
        trends      走势类型
        trend       当前走势类型
        beichi      背驰
        points      买卖点（B1/B2/B3/S1/S2/S3）
        hist/dif/dea  MACD
        meta        口径与近似声明
    """
    need = {"open", "high", "low", "close"}
    if not need.issubset(df.columns):
        raise ValueError(f"df 缺列：{need - set(df.columns)}")
    df = df.reset_index(drop=True)

    lv = LV.build_levels(df, max_level=max_level, formula=formula, gap_mode=gap_mode,
                         require_end_peak=require_end_peak)
    L1 = lv["levels"][0] if lv["levels"] else None
    if L1 is None:
        raise RuntimeError("结构解析失败：没有任何级别生成")

    # 中枢只许用「已确认」线段（78 课：未确认段随时会变）
    segs = [s for s in L1["segs"] if s.get("confirmed", False)]
    # 但背驰与买卖点必须能看到**最后那根未确认段**——它才是"当前正在走"的离开段。
    # 第 24 课明说「看MACD柱子的面积不需要全出来……柱子的力度变慢时把已出现的面积乘2」，
    # 就是给这种情况用的。之前把未确认段一律排除，导致末中枢永远没有 c 段，
    # 趋势背驰实测恒为 0、一类买卖点不可达（质检第 2 条）。
    tail = [s for s in L1["segs"] if not s.get("confirmed", False)]
    segs_all = segs + tail[:1]
    zss = ZS.build_zhongshus(segs, formula=formula) if len(segs) >= 3 else []
    ZS.classify_fate(zss)
    hist = BEI.macd(df["close"].to_numpy(float))[2]
    dif, dea, _ = BEI.macd(df["close"].to_numpy(float))
    bei = BEI.find_beichi(zss, segs_all, hist, level_rule=level_rule, overlap_rule=overlap_rule) if zss else []
    trends = TR.classify(zss, overlap_rule=overlap_rule) if zss else []
    cur = trends[-1] if trends else {"type": "无", "dir": 0, "complete": False, "evidence": "无中枢"}
    pts = PT.find_points(zss, segs_all, bei, trends, hist=hist,
                         allow_panzheng_b1=allow_panzheng_b1) if (zss and segs) else []

    return {
        "levels": lv["levels"],
        "segs": segs,
        "segs_all": segs_all,
        "zhongshus": zss,
        "trends": trends,
        "trend": cur,
        "beichi": bei,
        "points": pts,
        "hist": hist,
        "dif": dif,
        "dea": dea,
        "n_raw": len(df),
        "last_close": float(df["close"].iloc[-1]),
        "meta": {
            "max_level": max_level,
            "formula": formula,
            "gap_mode": gap_mode,
            "level_rule": level_rule,
            "overlap_rule": overlap_rule,
            "require_end_peak": require_end_peak,
            "allow_panzheng_b1": allow_panzheng_b1,
            "approx": [
                "次级别走势类型用线段代理（原文未给线段→级别的映射算法）",
                "中枢级别用 n_seg 代理（原文未给中枢级别的判定算法）",
                "中枢 G/D 未实现（需先切 Z 走势段，原文无算法）",
                "级别递归中「线段→上层笔」是工程近似",
            ],
        },
    }


def invariants(res: dict) -> list[str]:
    """全链路一致性检查：把各模块的检查串起来，外加跨模块的一致性。"""
    errs = LV.level_invariants({"levels": res["levels"], "meta": {}})
    errs += ZS.zs_invariants(res["zhongshus"])
    errs += TR.invariants(res["trends"], res["zhongshus"])
    errs += PT.invariants(res["points"], res.get("segs_all") or res["segs"])
    # 跨模块：每个中枢的段下标必须落在 segs 范围内
    for k, z in enumerate(res["zhongshus"]):
        if z["end_seg"] >= len(res["segs"]):
            errs.append(f"中枢{k} 的段下标越界：{z['end_seg']} >= {len(res['segs'])}")
    # 跨模块：每个买卖点必须能在 segs 里找到对应端点
    ends = {int(s["end_raw"]) for s in (res.get("segs_all") or res["segs"])}
    for k, p in enumerate(res["points"]):
        if int(p["raw"]) not in ends:
            errs.append(f"买卖点{k} 的 raw={p['raw']} 不在线段端点上")
    return errs
