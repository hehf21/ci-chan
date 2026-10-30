"""
背驰（严格实现）
================
原文依据（逐字见 _规则依据-原文摘录.md 第八节）：
  24 课：A/B/C 三段结构 + MACD 面积比较 + 「面积乘 2」处理则 + 盘整背驰的两种情形
  25 课：趋势背驰 vs 盘整背驰的分工；趋势背驰「一定至少回跌到 B 段中」
  27 课：「背驰段」定义；第一个中枢就出现的只能算盘整背驰
  29 课：背驰-转折定理
  37 课：把成立条件写死（**与 24 课冲突的一处，见下**）
  43 课：转折的两种级别关系

## 原文最矛盾的一处（不许悄悄选边）
24 课：「显然，B的中枢级别比A、C里的中枢级别都要大，否则A、B、C就连成一个大的趋势或大的中枢了」
37 课：「首先要 a+A+b+B+c 是一个趋势。而一个趋势，就意味着A、B是同级别的中枢，否则……
       这样，是不存在背驰的，最多就是盘整背驰」
→ 一处要求 B 更大、一处要求 A、B 同级。本模块用 `level_rule` 开关实现两套，默认 "37"
  （37 课更晚、更严，且是"再分辨/把条件写死"性质），"24" 只作对照。
  在单一结构层上，中枢的"级别"只能用 `n_seg`（含段数）做工程代理——原文没有给
  「如何判定两个中枢级别相同」的算法（摘录 14.1 第 5 条），此处属工程近似。

## 37 课另外三条硬条件，本模块逐条实现
① c 必然是次级别的（至少包含对 B 的一个第三类买卖点）
   → 单层结构里无法真验"含第三类买卖点"，退化为：c 必须**离开 B 的区间**
     （向上段终点 > B.ZG，向下段终点 < B.ZD）。属近似，已标 engine_approx。
② b 在级别上不能大于 c → 用线段数代理（b 的跨越笔数 ≤ c 的跨越笔数）。
③ 上涨 c 必须创新高、下跌 c 必须创新低（相对 a 的终点）→ 严格实现，可用开关关掉。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- MACD
def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD(12,26,9)。第 24 课用它做背驰的辅助判断。"""
    s = pd.Series(np.asarray(close, dtype=float))
    dif = s.ewm(span=fast, adjust=False).mean() - s.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2.0
    return dif.to_numpy(), dea.to_numpy(), hist.to_numpy()


def _slice(seg: dict, hist: np.ndarray) -> np.ndarray:
    a, b = int(seg["start_raw"]), int(seg["end_raw"])
    if b < a:
        a, b = b, a
    return hist[a : b + 1]


def seg_area(seg: dict, hist: np.ndarray) -> float:
    """一段的 MACD 力度 = 该段区间内同向柱面积（向上取红柱、向下取绿柱）。"""
    h = _slice(seg, hist)
    if h.size == 0:
        return 0.0
    if int(seg["dir"]) > 0:
        return float(np.clip(h, 0, None).sum())
    return float(np.clip(-h, 0, None).sum())


def area_with_24_rule(seg: dict, hist: np.ndarray, is_forming: bool = False) -> dict:
    """第 24 课的「面积乘 2」口径。

    原文：「看MACD柱子的面积不需要全出来，一般柱子伸长的力度变慢时，把已经出现的面积乘2，
          就可以当成是该段的面积」
    实现：若该段最后一个位置时，同向柱子已经「见顶回落」（峰值出现在末尾之前），
          则把已有面积乘 2 作为估算值；否则用原值。
    """
    h = _slice(seg, hist)
    raw = seg_area(seg, hist)
    if h.size == 0:
        return {"area": raw, "estimated": raw, "peak_passed": False, "applied": False}
    signed = np.clip(h, 0, None) if int(seg["dir"]) > 0 else np.clip(-h, 0, None)
    peak_idx = int(np.argmax(signed))
    peak_passed = peak_idx < len(signed) - 1
    applied = bool(is_forming and peak_passed)
    return {
        "area": raw,
        "estimated": raw * 2 if applied else raw,
        "peak_passed": peak_passed,
        "applied": applied,
    }


# ---------------------------------------------------------------- 背驰判定
def _zs_overlap(a: dict, b: dict, rule: str = "ZG_ZD") -> bool:
    """趋势要求中枢之间绝对无重叠；rule="GG_DD" 为第 20 课严口径，"ZG_ZD" 为第 18 课字面口径。"""
    if rule == "ZG_ZD":
        return not (b["ZD"] > a["ZG"] or b["ZG"] < a["ZD"])
    return not (b["DD"] > a["GG"] or b["GG"] < a["DD"])


def find_beichi(
    zs_list: list[dict],
    segs: list[dict],
    hist: np.ndarray,
    *,
    level_rule: str = "37",
    overlap_rule: str = "ZG_ZD",
    require_new_extreme: bool = True,
    require_leave_range: bool = True,
    c_is_forming: bool = False,
) -> list[dict]:
    """找出所有背驰点。

    返回每项：
        kind        "趋势背驰" / "盘整背驰"
        dir         +1 向上背驰（对应一类卖点）/ -1 向下背驰（对应一类买点）
        zs_a, zs_b  参与比较的中枢下标（盘整背驰只有 zs_a）
        seg_a, seg_c 参与比较的线段下标
        area_a, area_c, ratio
        level_rule, engine_approx, evidence
    """
    if level_rule not in ("37", "24"):
        raise ValueError("level_rule 只支持 '37' 或 '24'")
    out: list[dict] = []
    n = len(zs_list)
    if n == 0 or len(segs) < 3:
        return out

    def _check(seg_a_idx: int, seg_c_idx: int, zs_a: dict, zs_b: dict | None, kind: str):
        seg_a, seg_c = segs[seg_a_idx], segs[seg_c_idx]
        # a、c 必须同向（同一趋势方向）
        if int(seg_a["dir"]) != int(seg_c["dir"]):
            return
        dirn = int(seg_c["dir"])
        # 37 课③：上涨 c 必须创新高、下跌 c 必须创新低
        if require_new_extreme:
            if dirn > 0 and not (seg_c["end_px"] > seg_a["end_px"]):
                return
            if dirn < 0 and not (seg_c["end_px"] < seg_a["end_px"]):
                return
        # 37 课①：c 必须离开 B 的区间（近似「含对 B 的第三类买卖点」）
        if require_leave_range and zs_b is not None:
            if dirn > 0 and not (seg_c["end_px"] > zs_b["ZG"]):
                return
            if dirn < 0 and not (seg_c["end_px"] < zs_b["ZD"]):
                return
        # 24 课：C 段面积 < A 段面积
        # 第 24 课「柱子伸长力度变慢时面积乘 2」：c 段若还没走完（末根就是当前 bar）则自动启用，
        # 不再依赖调用方传 c_is_forming（之前默认 False 且没人传，导致这条规则是死代码）。
        c_forming = bool(c_is_forming) or int(seg_c["end_raw"]) >= len(hist) - 1
        a_info = area_with_24_rule(seg_a, hist, is_forming=False)
        c_info = area_with_24_rule(seg_c, hist, is_forming=c_forming)
        area_a, area_c = a_info["estimated"], c_info["estimated"]
        if area_a <= 0:
            return
        if not (area_c < area_a):
            return
        ev = [
            f"A 段(第{seg_a_idx}段,{'向上' if dirn > 0 else '向下'}) 面积 {area_a:.1f}",
            f"C 段(第{seg_c_idx}段) 面积 {area_c:.1f}，比值 {area_c/area_a:.2f} < 1",
        ]
        if require_new_extreme:
            ev.append("c 创出新高/新低（37 课）")
        if require_leave_range and zs_b is not None:
            ev.append("c 已离开 B 中枢区间（近似 37 课『含第三类买卖点』）")
        if c_info["applied"]:
            ev.append("C 段未走完，按第 24 课面积乘 2 估算")
        out.append(
            {
                "kind": kind,
                "dir": dirn,
                "zs_a": zs_list.index(zs_a),
                "zs_b": zs_list.index(zs_b) if zs_b is not None else None,
                "seg_a": seg_a_idx,
                "seg_c": seg_c_idx,
                "area_a": round(area_a, 3),
                "area_c": round(area_c, 3),
                "ratio": round(area_c / area_a, 3),
                "level_rule": level_rule,
                "engine_approx": "中枢级别用 n_seg 代理；c 是否含第三类买卖点用『离开区间』近似",
                "evidence": "；".join(ev),
            }
        )

    # 趋势背驰：A、B 两个中枢（B 在后），a 为进入 A 的段，c 为离开 B 的段
    for k in range(1, n):
        A, B = zs_list[k - 1], zs_list[k]
        if _zs_overlap(A, B, overlap_rule):
            continue                                  # 有重叠 → 不是趋势（18/20 课）
        # a / c 的取法（第 24 课的「同向两段」）：
        #   a = A 中枢之前、与趋势同向的最后一段；c = B 中枢之后、与趋势同向的第一段。
        # 之前机械取「中枢前一笔 / 中枢后一笔」，在下跌趋势里经常取到反向段，
        # 导致 a 与 c 不同向直接 return，向下背驰一条都出不来（实测 19 对里 10 对死在这）。
        trend_dir = 1 if B["ZD"] > A["ZG"] else -1
        a_idx = next((i for i in range(A["start_seg"] - 1, -1, -1) if int(segs[i]["dir"]) == trend_dir), None)
        c_idx = next((i for i in range(B["end_seg"] + 1, len(segs)) if int(segs[i]["dir"]) == trend_dir), None)
        if a_idx is None or c_idx is None:
            continue
        if level_rule == "24":
            # 24 课口径：B 的中枢级别要比 A、C 大（用含段数代理）
            if not (B["n_seg"] > A["n_seg"]):
                continue
        # 37 课②：b 在级别上不能大于 c。b 是 A、B 之间的连接段（正常应为 1 段），
        # 若 A 与 B 之间夹了多个线段，说明 b 比 c 大 → 按 37 课不能算趋势背驰。
        b_segs = B["start_seg"] - A["end_seg"] - 1
        if b_segs > 1:
            continue
        _check(a_idx, c_idx, A, B, "趋势背驰")

    # 盘整背驰：中枢震荡内部的背驰（27 课：第一个中枢就出现的背驰只能算盘整背驰）
    # 2026-10-30 扩到「所有不在趋势对里的中枢」：原先只算 zs_list[0]，
    # 导致 1p（盘整背驰一类点）几乎不可能出现，买卖点密度只有 chan.py 的 1/3~1/5。
    # 判据不变（进入段 vs 离开段同向、C 面积 < A 面积），只是不再只认第一个中枢。
    for k, A in enumerate(zs_list):
        if k > 0 and not _zs_overlap(zs_list[k - 1], A, overlap_rule):
            continue                      # 前一个中枢与本中枢构成趋势 → 交给趋势背驰
        if k + 1 < n and not _zs_overlap(A, zs_list[k + 1], overlap_rule):
            continue                      # 后面进入趋势 → 交给趋势背驰
        if A["start_seg"] - 1 >= 0 and A["end_seg"] + 1 < len(segs):
            _check(A["start_seg"] - 1, A["end_seg"] + 1, A, A, "盘整背驰")

    return out
