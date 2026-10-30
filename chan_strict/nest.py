"""
区间套（严格实现 + 工程约定）
=============================
原文依据（逐字见 _规则依据-原文摘录.md 第八、十二节）：

第 27 课（类比，不是定义）：
    「先补充一个定义，就是在某级别的某类型走势，如果构成背驰或盘整背驰，
      就把这段走势类型称为某级别的背驰段。」
第 61 课（唯一的操作程序，四重背驰段递推示范）：
    在日线背驰段里找 30 分钟背驰段，再在里面找 5 分钟背驰段，逐级缩小定位。
第 102 课（定位）：
    「区间套的方法，就是走势必完美的一个重要的应用……有了区间套，买卖点的精确定位才有可能」

## 原文没有给的东西（必须声明，不许糊）
原文**从未给出**：
  ① 区间套的终止条件（递归到哪一级停）；
  ② 「区间套成立」的判定式（摘录 12.7 已指出：只有类比与操作程序，没有定义）。
本模块的工程约定，逐条写清：
  · 终止条件 = 本实现只有两个数据级别（大级别日线 / 次级别 30 分钟），递归到次级别即停；
  · 「确认」= 次级别在父级买卖点附近出现**同向**买卖点，且时间不晚于父级点；
  · 找不到同向次级别点 → 明确返回落空（confirmed=False + reason），不许拿"最近的一个点"凑数。
"""

from __future__ import annotations

import pandas as pd

from . import pipeline as PL


def locate(
    parent_res: dict,
    parent_df: pd.DataFrame,
    child_df: pd.DataFrame,
    *,
    lookback_days: int = 15,
    min_child_bars: int = 150,
    level_rule: str = "37",
    overlap_rule: str = "ZG_ZD",
) -> list[dict]:
    """对父级别的每个买卖点，在次级别里做区间套定位。

    parent_res: chan_strict.pipeline.analyze(parent_df) 的结果
    parent_df / child_df: 需含 date/open/high/low/close，升序

    返回每项：
        parent        父级点（kind/px/date）
        child         次级别确认点（None 表示落空）
        confirmed     bool
        reason        落空原因或确认说明
        path          递归路径（本实现两层，所以只有两级）
    """
    out: list[dict] = []
    if parent_df is None or child_df is None or len(parent_df) == 0 or len(child_df) == 0:
        return out
    if "date" not in parent_df.columns or "date" not in child_df.columns:
        raise ValueError("区间套需要 date 列（用于两级时间对齐）")

    cdf = child_df.copy()
    cdf["date"] = pd.to_datetime(cdf["date"])
    pdf = parent_df.copy()
    pdf["date"] = pd.to_datetime(pdf["date"])

    for p in parent_res.get("points", []):
        raw = int(p["raw"])
        if raw >= len(pdf):
            continue
        p_date = pdf["date"].iloc[raw]
        is_buy = p["kind"].startswith("B")

        win_start = p_date - pd.Timedelta(days=lookback_days)
        win = cdf[(cdf["date"] > win_start) & (cdf["date"] <= p_date)]
        rec = {
            "parent": {"kind": p["kind"], "px": float(p["px"]), "date": str(p_date.date())},
            "child": None,
            "confirmed": False,
            "path": [f"大级别 {p['kind']} @ {p_date.date()} 价 {float(p['px']):.2f}"],
            "reason": "",
        }
        if len(win) < min_child_bars:
            rec["reason"] = f"次级别窗口只有 {len(win)} 根（需 {min_child_bars} 根），无法构造结构"
            out.append(rec)
            continue
        try:
            cres = PL.analyze(
                win[["open", "high", "low", "close"]].reset_index(drop=True),
                max_level=1, level_rule=level_rule, overlap_rule=overlap_rule,
            )
        except Exception as e:  # noqa: BLE001
            rec["reason"] = f"次级别结构解析失败：{type(e).__name__}: {e}"
            out.append(rec)
            continue

        same = [q for q in cres["points"] if q["kind"].startswith("B") == is_buy]
        if not same:
            # 第 61 课用的是「次级别**背驰段**」，不限于次级别买卖点：
            # 若次级别没长出买卖点（结构未完成），但出现了同向背驰，也算次级别确认。
            # 仍然要求同向，不许拿反向的凑数。
            want = -1 if is_buy else 1
            bei_same = [b for b in cres.get("beichi", []) if int(b.get("dir", 0)) == want]
            if bei_same:
                b0 = bei_same[-1]
                seg_c = cres["segs"][int(b0["seg_c"])] if int(b0["seg_c"]) < len(cres["segs"]) else None
                if seg_c is not None:
                    c_date = win["date"].iloc[int(seg_c["end_raw"])]
                    rec["child"] = {"kind": f"背驰{'买' if is_buy else '卖'}",
                                    "px": float(seg_c["end_px"]), "date": str(c_date.date()),
                                    "level": 1, "via": "beichi"}
                    rec["confirmed"] = True
                    rec["path"].append(f"次级别背驰 @ {c_date.date()} 价 {float(seg_c['end_px']):.2f}")
                    rec["reason"] = "次级别同向背驰确认（第 61 课用的是背驰段，不要求买卖点）"
                    out.append(rec)
                    continue
            rec["reason"] = "次级别在窗口内既无同向买卖点、也无同向背驰（区间套落空，按原文口径就是等，不许凑数）"
            out.append(rec)
            continue
        q = same[-1]                      # 取窗口内最后一个同向点（最靠近父级点）
        q_date = win["date"].iloc[int(q["raw"])]
        rec["child"] = {"kind": q["kind"], "px": float(q["px"]), "date": str(q_date.date()),
                        "level": q.get("level", 1)}
        rec["confirmed"] = True
        rec["path"].append(f"次级别 {q['kind']} @ {q_date.date()} 价 {float(q['px']):.2f}")
        rec["reason"] = "次级别同向买卖点确认"
        out.append(rec)
    return out


def summary(recs: list[dict]) -> dict:
    """区间套结果汇总。"""
    total = len(recs)
    ok = sum(1 for r in recs if r["confirmed"])
    return {
        "父级点数": total,
        "次级别确认": ok,
        "落空": total - ok,
        "确认率": round(ok / total, 3) if total else 0.0,
        "说明": "落空即按原文口径等待，不降级凑数",
    }
