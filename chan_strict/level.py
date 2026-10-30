"""
级别递归（严格实现）
====================
原文依据（逐字见 _规则依据-原文摘录.md 第十二节）：

第 17 课：中枢由「三个连续次级别走势类型」重叠构成 → 级别是**递归定义**出来的。
第 18 课：中枢的重述与「前三个次级别走势类型必须完成」。
第 63 课：级别是「显微镜倍数」的比喻（放大了看，结构自相似）。
第 84 课（最关键的一句）：
    「级别，本质上与时间无关……只是按照本ID的规则，自生长出来的一种分类方法」
    → **本模块的 L1/L2/L3 是结构级别，与 5 分钟/30 分钟/日线等时间周期没有任何对应关系。**
      不要看到 L1 就以为是日线级别，也不要拿"30 分钟图"来对齐。

递归方案（工程实现，与原文的关系写清楚）：
    L1：原始 K 线 → 笔 → 线段 → 中枢（中枢由线段构成，前三个次级别走势类型≈三个线段）
    L2：把 L1 的**线段**当作 L2 的「笔」，再走一遍「笔 → 线段 → 中枢」
    L3：同法，把 L2 的线段当作 L3 的笔……

为什么这么接：原文的「次级别走势类型」在结构上最接近的就是「更高一级的笔」，
这是全行业实现缠论级别时的通行接口，也是本工程唯一可行的接法。
**这仍然是一个工程近似**（原文没给"如何从线段升级成笔"的算法），所以：
    · 每层输出都带 level 字段与 derived_from 字段（可回溯到下层元素）
    · 文档里必须写明这是近似，不许说成"原文的级别定义"

终止条件（原文未给）：层数上限 max_level，或下层元素不足以构成 3 个时停。
"""

from __future__ import annotations

from . import bi as BI
from . import kline as KL
from . import seg as SEG
from . import zs as ZS


def seg_to_bi(segs: list[dict]) -> list[dict]:
    """把「线段序列」映射成下一级别的「笔序列」。

    这是级别递归的接口，也是本实现最需要声明的一处**工程近似**：
    原文只说"次级别走势类型构成中枢"，没有给出"什么样的线段可以升格成上一级的一笔"。
    """
    out = []
    for k, s in enumerate(segs):
        out.append(
            {
                "dir": s["dir"],
                "start_bar": s.get("start_bar", s["start_raw"]),
                "end_bar": s.get("end_bar", s["end_raw"]),
                "start_px": s["start_px"],
                "end_px": s["end_px"],
                "start_raw": s["start_raw"],
                "end_raw": s["end_raw"],
                "derived_from_seg": k,      # 可回溯：这一"笔"来自下层第 k 根线段
            }
        )
    return out


def build_levels(df, max_level: int = 3, formula: str = "20", gap_mode: str = "strict",
                 require_end_peak: bool = False) -> dict:
    """递归构造多级别结构。

    require_end_peak: 透传给 bi.build_bis 的工程增补钩子（笔端点须为区间极值）。

    返回：
        levels: [{level, bars, fractals, bis, segs, zhongshus, n_bi, n_seg, n_zs, complete}]
        meta:   数据区间、递归终止原因、近似声明
    """
    levels: list[dict] = []

    bars = KL.merge_bars(df)
    fractals = KL.find_fractals(bars)
    bis = BI.build_bis(bars, fractals, require_end_peak=require_end_peak)

    L = 1
    terminated_by = ""
    while L <= max_level:
        segs = SEG.build_segments(bis, gap_mode=gap_mode) if len(bis) >= 3 else []
        # 中枢用「已确认」的线段构造：未确认尾段按 78 课随时会变，不许当已完成线段用
        confirmed_segs = [s for s in segs if s.get("confirmed", False)]
        zss = ZS.build_zhongshus(confirmed_segs, formula=formula) if len(confirmed_segs) >= 3 else []
        levels.append(
            {
                "level": L,
                "bars": bars,
                "fractals": fractals,
                "bis": bis,
                "segs": segs,
                "zhongshus": zss,
                "n_bi": len(bis),
                "n_seg": len(segs),
                "n_seg_confirmed": len(confirmed_segs),
                "n_zs": len(zss),
            }
        )
        if len(confirmed_segs) < 3:
            terminated_by = f"L{L} 的已确认线段只有 {len(confirmed_segs)} 根，不足以构造中枢"
            break
        if L == max_level:
            terminated_by = f"达到 max_level={max_level}"
            break
        # 升一级：本层线段 → 上层笔
        bis = seg_to_bi(confirmed_segs)
        fractals = []                        # 上层不再用分型，直接用线段当笔
        L += 1

    return {
        "levels": levels,
        "meta": {
            "n_bars": len(df),
            "max_level": max_level,
            "n_levels_built": len(levels),
            "terminated_by": terminated_by,
            "formula": formula,
            "gap_mode": gap_mode,
            "require_end_peak": require_end_peak,
            "ENGINEERED_NOTE": (
                "级别 = 结构递归（17/84 课），与时间周期无关（84 课原话）；"
                "「线段→上层笔」这一步原文未给算法，属工程近似"
            ),
        },
    }


def level_invariants(res: dict) -> list[str]:
    """层级一致性检查（测试与回归用）。"""
    errs = []
    lv = res["levels"]
    for k, L in enumerate(lv):
        if L["n_seg_confirmed"] != sum(1 for s in L["segs"] if s.get("confirmed", False)):
            errs.append(f"L{L['level']} 已确认线段数不自洽")
        if L["n_zs"] > 0 and L["n_seg_confirmed"] < 3:
            errs.append(f"L{L['level']} 线段不足 3 根却出现了中枢")
        if L["n_seg"] > L["n_bi"]:
            errs.append(f"L{L['level']} 线段数大于笔数")
        if k > 0:
            prev = lv[k - 1]
            # 上一层的笔数必须等于下一层送进去的已确认线段数
            if L["n_bi"] != prev["n_seg_confirmed"]:
                errs.append(
                    f"L{L['level']} 的笔数({L['n_bi']})不等于 L{prev['level']} 的已确认线段数({prev['n_seg_confirmed']})"
                )
    return errs
