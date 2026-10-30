"""
走势中枢（严格实现，用线段而非笔）
==================================
原文依据（逐字见 _规则依据-原文摘录.md 第五节）：

第 17 课：中枢由「三个连续次级别走势类型」的重叠定义（递归来源）。
第 18 课：中枢的定义 + 区间公式（用 A、B、C 三段的**全部**高低点）：
    `（max（a2,b2,c2），min（a1,b1,c1））`
第 20 课：ZD/ZG、GG/G/D/DD 全套符号 + 中心定理一/二 + 级别延续定理一/二；
    并把区间公式**简化**为只取同向段：`[max（a2,c2），min（a1,c1）]`，
    原文自己说「但无论是哪种情况，中枢的公式都可以简化为」。
    → **18 课与 20 课两式在 A、C 与 B 方向不一致时结果不同，原文没说以哪个为准**
      （摘录 14.2 第 3 条）。本模块两套都实现，用 formula 开关切换，默认 "20"。
第 21 课：中枢的三种命运 —— 延续、扩张、新生。
第 33 课：中枢延伸不得超过 5 段，6 段延伸 + 形成中枢的 3 段即构成更大级别中枢。
    → 与第 20 课「只给几何判据、不设上限」并列存在（摘录 14.2 第 4 条）；
      本模块默认取 33 课上限，max_seg 可调，设成 None 即退化为 20 课口径。

诚实标注（原文没有、本模块不敢代拟的部分）：
① 第 20 课的 G / D（Z 走势段的高低点）需要先切出「与中枢方向一致的次级别走势段」，
   而原文对「Z走势段」怎么从实际走势里切出来只有定性描述、没有算法（摘录 14.3 第 3 条）。
   本模块**不输出 G / D**，只输出可严格算出的 ZG / ZD / GG / DD。
② 「中枢扩展」原文说「是在两个中枢都完全走出来的情况下定义的」，但没写出定义本身
   （摘录 14.1 第 5 条）。本模块用「相邻两中枢区间有重叠」作为工程判据，并标 engineered=True。
"""

from __future__ import annotations

ENGINEERED_NOTES = {
    "expansion": "原文未给出中枢扩展的定义句，本实现用『相邻两中枢区间有重叠』作工程判据",
    "G_D": "第20课的 G/D 需先切出 Z 走势段，原文无算法，本模块不输出",
}


def _seg_range(seg: dict) -> tuple[float, float]:
    """一段的高低区间 (low, high)。"""
    a, b = float(seg["start_px"]), float(seg["end_px"])
    return (min(a, b), max(a, b))


def build_zhongshus(segs: list[dict], formula: str = "20", max_seg: int | None = 8,
                    require_confirmed: bool = True) -> list[dict]:
    """用线段构造中枢。

    segs: 线段序列，每段需含 dir(+1/-1)、start_px、end_px、start_raw、end_raw，
          可选 confirmed（缺失按 True 处理，方便单测直接喂手造线段）
    formula: "20" = 只取同向段（第 20 课简化式）；"18" = 取三段全部高低点（第 18 课原式）
    max_seg: 中枢含段数上限（第 33 课 5 段延伸 + 3 段本体 = 8）；None = 不限（第 20 课口径）
    require_confirmed: 默认 True —— 未确认尾段按 78 课「随时会变」，**不许**进中枢。
        这一层保护是必须的：只靠调用方记得过滤，迟早有人忘（子代理在 seg 交付里专门提醒过）。

    返回每个中枢：
        start_seg, end_seg    首末段在 segs 中的下标（闭区间）
        ZG, ZD                中枢上沿 / 下沿
        GG, DD                中枢区间内最高 / 最低
        dir                   中枢方向：以进入段方向命名（+1 上涨中枢 / -1 下跌中枢）
        n_seg                 含段数
        extended              是否发生延伸（超过 3 段）
        upgraded              是否因触及 max_seg 而升级（第 33 课）
        start_raw, end_raw
    """
    if formula not in ("18", "20"):
        raise ValueError("formula 只支持 '18' 或 '20'")
    if require_confirmed:
        segs = [s for s in segs if s.get("confirmed", True)]

    out: list[dict] = []
    i, n = 0, len(segs)
    while i + 2 < n:
        tri = segs[i : i + 3]
        lows = [_seg_range(s)[0] for s in tri]
        highs = [_seg_range(s)[1] for s in tri]

        if formula == "20":
            # 第 20 课：只用同向段 A、C（下标 0、2）
            zg = min(highs[0], highs[2])
            zd = max(lows[0], lows[2])
        else:
            # 第 18 课：三段全部高低点
            zg = min(highs)
            zd = max(lows)

        if zd >= zg:                      # 三段无重叠 → 不是中枢
            i += 1
            continue

        j = i + 3
        upgraded = False
        left = False
        while j < n:                      # 延伸 / 结束判定
            if max_seg is not None and (j - i) >= max_seg:
                upgraded = True           # 第 33 课：达到上限 → 升级为更大级别
                break
            s = segs[j]
            end_px = float(s["end_px"])
            if zd <= end_px <= zg:        # 段收在中枢区间内 → 仍在中枢震荡，延伸
                j += 1
                continue
            # 段收在区间外：看下一段有没有「回到中枢」
            # （第 20 课：离开中枢后不回到中枢区间，中枢才结束；这就是三买的起点）
            if j + 1 < n and zd <= float(segs[j + 1]["end_px"]) <= zg:
                j += 1                    # 又回来了 → 仍算延伸
                continue
            left = True                   # 离开且不回来 → 中枢结束，segs[j] 是离开段
            break

        n_seg = j - i
        # 中枢内各段的实际极值（GG/DD），按第 20 课定义取中枢区间内最高/最低
        inner_hi = max(highs + [_seg_range(s)[1] for s in segs[i + 3 : j]])
        inner_lo = min(lows + [_seg_range(s)[0] for s in segs[i + 3 : j]])

        enter_dir = -1 if i > 0 and segs[i - 1]["dir"] > 0 else (1 if i > 0 else 1)

        out.append(
            {
                "start_seg": i,
                "end_seg": j - 1,
                "ZG": zg,
                "ZD": zd,
                "GG": inner_hi,
                "DD": inner_lo,
                "dir": enter_dir,
                "n_seg": n_seg,
                "extended": n_seg > 3,
                "upgraded": upgraded,
                "start_raw": segs[i]["start_raw"],
                "end_raw": segs[j - 1]["end_raw"],
                "formula": formula,
            }
        )
        # 关键：中枢结束后，**离开段要留给下一个结构当连接段 b**。
        # 之前写成 i = j，把离开段当成了下一中枢的第一段，结果 b_segs 恒为 0，
        # a+A+b+B+c 结构永远凑不出来，趋势背驰/一类买卖点全废（质检第 2 条真正根因）。
        i = j + 1 if left else j
    return out


def classify_fate(zs_list: list[dict]) -> list[dict]:
    """第 21 课：中枢的三种命运（延续 / 扩张 / 新生）。

    用相邻中枢的空间关系判定：
        区间有重叠          → 扩张（形成更大级别中枢）
        无重叠且后中枢更高  → 新生（构成上涨趋势）
        无重叠且后中枢更低  → 新生（构成下跌趋势）
    注意：这是工程判据（见 ENGINEERED_NOTES['expansion']），原文只给了「扩张」「新生」
    这两个名字的用法，没有给可计算的判别式。
    """
    for k, z in enumerate(zs_list):
        if k == 0:
            z["fate"] = "首个中枢"
            z["engineered"] = False
            continue
        prev = zs_list[k - 1]
        overlap = not (z["ZD"] > prev["ZG"] or z["ZG"] < prev["ZD"])
        if overlap:
            z["fate"] = "扩张"
            z["engineered"] = True
        elif z["ZD"] > prev["ZG"]:
            z["fate"] = "新生（上涨）"
            z["engineered"] = False
        else:
            z["fate"] = "新生（下跌）"
            z["engineered"] = False
    return zs_list


def zs_invariants(zs_list: list[dict]) -> list[str]:
    """中枢的硬性质（测试与回归用）。"""
    errs = []
    for k, z in enumerate(zs_list):
        if not (z["ZD"] < z["ZG"]):
            errs.append(f"第{k}个中枢 ZD({z['ZD']}) >= ZG({z['ZG']})")
        if z["DD"] > z["ZD"]:
            errs.append(f"第{k}个中枢 DD 应 <= ZD")
        if z["GG"] < z["ZG"]:
            errs.append(f"第{k}个中枢 GG 应 >= ZG")
        if z["end_seg"] - z["start_seg"] + 1 != z["n_seg"]:
            errs.append(f"第{k}个中枢段数不自洽")
        if k > 0 and z["start_seg"] <= zs_list[k - 1]["end_seg"]:
            errs.append(f"第{k}个中枢与前一个重叠了线段")
    return errs
