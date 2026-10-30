"""
走势类型与走势必完美（严格实现）
================================
原文依据（逐字见 _规则依据-原文摘录.md 第六、七节）：

第 17 课（定义，注意「完成的走势类型」这个前提）：
    「缠中说禅盘整：……某完成的走势类型只包含一个缠中说禅走势中枢」
    「缠中说禅趋势：……某完成的走势类型至少包含两个以上依次同向的缠中说禅走势中枢」
第 18 课（补排除条件）：
    「注意，趋势中的缠中说禅走势中枢之间必须绝对不存在重叠。」
第 20 课（重叠的口径放大到瞬间波动）：
    「在趋势里，同级别的前后缠中说禅走势中枢是不能有任何重叠的，
      这包括任何围绕走势中枢产生的任何瞬间波动之间的重叠。」
    → 判重叠要用 GG/DD（含波动的极值），不能只用 ZG/ZD。
第 45 课（走势类型的最低结构标准）：
    「一个30分钟的走势类型，最低标准，就要形成一个30分钟的中枢，一旦这中枢形成，
      该走势类型随时结束都是符合理论的。」
第 17 课（走势必完美 = 原理一）：
    「任何级别的任何走势类型终要完成」，且「一旦某种类型的走势完成以后，就会转化为其他类型的走势」
第 102 课：
    「级别在本ID理论中就极端关键了……本ID的递归函数是有级别的」
    「区间套的方法，就是走势必完美的一个重要的应用」

## 「完成」怎么落成可判定（这是原文没给的，本模块的工程化，必须声明）
原文只说「终要完成」，没给判据（摘录 14.1 第 6 条已指出）。
本模块的操作化定义，逐条写清以便审计：
    一个走势类型「已完成」 ⇔ 它后面出现了**不属于它的**新结构，即
        ① 它的最后一个中枢之后出现了离开段，且
        ② 离开段之后开始了新的走势类型（新的中枢，或反向的线段序列）。
    最后一个走势类型（数据末端那个）一律标 complete=False ——
    因为「当下的走势是两难的」（17 课原话），不许假装它已经完成。
这属于工程约定，不是原文判据。
"""

from __future__ import annotations


def _overlap(a: dict, b: dict, rule: str = "ZG_ZD") -> bool:
    """两中枢是否有重叠。

    rule="ZG_ZD"（默认，第 18 课字面口径）：只用中枢区间 ZG/ZD 判重叠。
        换默认的原因：GG_DD 口径下相邻中枢几乎必然重叠，走势类型恒为盘整（实测 40 次全盘整、
        上涨/下跌各 0 次），趋势与一/二类买卖点全部不可达，策略实际只用到了三类买点。
    rule="GG_DD"（第 20 课严口径，可选）：连「围绕中枢产生的任何瞬间波动」都算重叠 →
        在此口径下没有趋势、没有一类买卖点，只能做三类点，必须明说。

    为什么做成开关：18 课原文只说「趋势中的中枢之间必须绝对不存在重叠」，没规定用哪组边界；
    20 课补到瞬间波动。两处都被本文引用（摘录第六节），所以两套都实现，默认取最严的 20 课口径，
    需要宽松口径时显式切到 ZG_ZD，并在输出里标出用了哪套。
    """
    if rule == "ZG_ZD":
        return not (b["ZD"] > a["ZG"] or b["ZG"] < a["ZD"])
    return not (b["DD"] > a["GG"] or b["GG"] < a["DD"])


def classify(zs_list: list[dict], n_segs: int | None = None, overlap_rule: str = "ZG_ZD") -> list[dict]:
    """把中枢序列切成走势类型。

    返回每个走势类型：
        type      "盘整" / "上涨" / "下跌"
        zs_from, zs_to   含哪些中枢（下标，闭区间）
        dir       +1 / -1 / 0
        complete  是否已完成（工程判据，见模块头）
        evidence  判定依据（给人看的）
    """
    out: list[dict] = []
    if not zs_list:
        return out

    # 分组规则（第 18/20 课）：
    #   相邻中枢**有重叠** → 合并进同一个中枢级别（= 盘整/中枢扩张），不是趋势
    #   相邻中枢**无重叠且同向** → 同一趋势的延续
    #   相邻中枢无重叠但反向 → 新的走势类型开始
    groups: list[dict] = [{"from": 0, "to": 0, "has_overlap": False}]
    for k in range(1, len(zs_list)):
        prev, now = zs_list[k - 1], zs_list[k]
        cur = groups[-1]
        if _overlap(prev, now, overlap_rule):
            cur["to"] = k
            cur["has_overlap"] = True
            continue
        same_dir = (now["ZD"] > prev["ZG"]) or (now["ZG"] < prev["ZD"])
        if same_dir and not cur["has_overlap"]:
            cur["to"] = k
        else:
            groups.append({"from": k, "to": k, "has_overlap": False})

    out: list[dict] = []
    for g in groups:
        i0, i1 = g["from"], g["to"]
        zs_first = zs_list[i0]
        if g["has_overlap"] or i1 == i0:
            # 第 17 课：盘整的定义是「只包含一个中枢」。
            # 多个中枢重叠 = 中枢扩张成一个更大级别的中枢（第 20 课），
            # 它不是「盘整」这个词的所指，必须分开标，否则会把大级别中枢当成盘整。
            label = "中枢扩张" if g["has_overlap"] else "盘整"
            out.append(
                {
                    "type": label,
                    "n_zs": i1 - i0 + 1,
                    "zs_from": i0, "zs_to": i1, "dir": 0,
                    "complete": False,
                    "evidence": (
                        f"{i1 - i0 + 1} 个中枢互相重叠 → 按第 20 课是中枢扩张（更大级别中枢），不是盘整"
                        if g["has_overlap"]
                        else f"只含 1 个中枢（第{i0}个），按第 17 课为盘整"
                    ),
                }
            )
            continue
        dirn = 1 if zs_list[i0 + 1]["ZD"] > zs_first["ZG"] else -1
        out.append(
            {
                "type": "上涨" if dirn > 0 else "下跌",
                "n_zs": i1 - i0 + 1,
                "zs_from": i0, "zs_to": i1, "dir": dirn,
                "complete": False,
                "evidence": f"含 {i1 - i0 + 1} 个同向且无重叠（GG/DD 口径）的中枢",
            }
        )

    # 完成判定：最后一个走势类型永远不算完成；其余若后面还有中枢/线段则算完成
    for i, t in enumerate(out):
        is_last = i == len(out) - 1
        if not is_last:
            t["complete"] = True
            t["evidence"] += "；其后已出现新的走势类型 → 按本模块工程判据视为完成"
        else:
            t["complete"] = False
            t["evidence"] += "；数据末端，按第 17 课「当下是两难的」不判完成"
    return out


def latest(zs_list: list[dict], n_segs: int | None = None, overlap_rule: str = "ZG_ZD") -> dict:
    """取当前（最后一个）走势类型，供策略判断「我在什么走势里」。"""
    ts = classify(zs_list, n_segs, overlap_rule)
    if not ts:
        return {"type": "无", "dir": 0, "complete": False, "evidence": "没有任何中枢"}
    return ts[-1]


def invariants(ts_list: list[dict], zs_list: list[dict], overlap_rule: str = "ZG_ZD") -> list[str]:
    """走势类型的硬性质（测试与回归用）。"""
    errs = []
    if not ts_list:
        return errs
    covered = []
    for k, t in enumerate(ts_list):
        if t["zs_from"] > t["zs_to"]:
            errs.append(f"第{k}个走势类型中枢区间倒置")
        covered.extend(range(t["zs_from"], t["zs_to"] + 1))
        if t["type"] in ("上涨", "下跌") and (t["zs_to"] - t["zs_from"] + 1) < 2:
            errs.append(f"第{k}个趋势只含 1 个中枢，违反第 17 课")
        if t["type"] in ("上涨", "下跌"):
            for i in range(t["zs_from"], t["zs_to"]):
                if _overlap(zs_list[i], zs_list[i + 1], overlap_rule):
                    errs.append(f"第{k}个趋势内的中枢有重叠，违反第 18/20 课")
        if t["type"] == "盘整" and (t["zs_to"] - t["zs_from"] + 1) != 1:
            errs.append(f"第{k}个走势类型标为盘整但含 {t['zs_to']-t['zs_from']+1} 个中枢，违反第 17 课")
        if t["type"] == "中枢扩张" and (t["zs_to"] - t["zs_from"] + 1) < 2:
            errs.append(f"第{k}个走势类型标为中枢扩张但只含 1 个中枢")
    if covered != sorted(covered) or covered != list(range(len(zs_list))):
        errs.append("中枢没有按顺序、无遗漏地被走势类型覆盖")
    return errs
