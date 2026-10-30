"""
三类买卖点（严格实现）
======================
原文依据（逐字见 _规则依据-原文摘录.md 第九、十、十一节）：

一类买点（第 21 课 + 第 53 课）：
    「只有在下跌确立后的中枢下方才可能出现买点。这就是第一类买点。」
    「第一类买卖点，就是该级别的背驰点」
    → 必须同时满足：① 该级别的背驰（趋势背驰）② 位置在中枢下方 ③ 处在下跌确立的走势里
二类买点（第 17 课 + 第 53 课）：
    「第一类买点出现后第一次次级别回调制造的低点」
    「高点一次级别向下后一次级别向上，如果不创新高或盘整背驰，都构成第二类卖点，买点反过来」
    → 实现两个并列判据：不破前低 + 可选「盘整背驰」
三类买点（第 20 课 + 38 课 + 61 课）：
    「一个次级别走势类型向上离开缠中说禅走势中枢，然后以一个次级别走势类型回试，
      其低点不跌破 ZG，则构成第三类买点」
    「并不是任何回调回抽都是第三类买卖点，必须是第一次」（20 课）
    「第一次回抽不到中枢……上去再回抽，如果同样不回到中枢里，但这就不是第三类买卖点了」（38 课）
    「一个第三类买卖点，至少需要有5段次级别的，前三段构成中枢，第四段离开中枢，第5段构成第三类买卖点」（61 课）

## 本实现的单位与近似（必须知道）
本实现的「次级别走势类型」用**线段**代理（与中枢模块一致）。因此：
  · 三买的「至少 5 段」在结构上就是：中枢 3 段 + 离开 1 段 + 回抽 1 段 —— 可以严格对上；
  · 但「次级别」这个级别概念本身是代理出来的（原文没给线段→级别的映射算法）。
  · 38 课的「第一次回抽」用「中枢结束后的第 1 段离开 + 第 2 段回抽」严格实现，
    之后再回抽不回到中枢的，不再算三买（这一点 v1 做错了，会把第二次回抽也当三买）。
"""

from __future__ import annotations

from . import bei as BEI


def find_points(
    zs_list: list[dict],
    segs: list[dict],
    beichi: list[dict],
    trend_types: list[dict] | None = None,
    *,
    require_b1_in_down: bool = True,
    allow_panzheng_b1: bool = False,
    hist=None,
) -> list[dict]:
    """找出所有买卖点。

    allow_panzheng_b1: 一类买卖点是否接受「盘整背驰」（默认 False = 严格口径：只认趋势背驰）。
        打开后 1p 类信号会大幅增加（对齐 chan.py 的 bs_type 里的 '1p'）。
        这是**口径开关**，不是 bug 修复：原文 27 课说盘整背驰给的是「一类买卖点」的降级版，
        严格派认为一类点必须是趋势背驰，所以两套都实现、由调用方选。

    返回每项：
        kind      B1/B2/B3/S1/S2/S3
        level     结构级别（本实现只有 1 级，字段留着为将来多级别用）
        px        买点/卖点价格（线段端点价）
        raw       该价对应的原始 K 线下标（用于回测定位）
        stop      结构失效价（跌破即该点作废）
        evidence  判定依据
        approx    近似说明（无近似则为 None）
    """
    pts: list[dict] = []
    if len(segs) < 3:
        return pts

    def _in_down(t: dict | None) -> bool:
        return bool(t and t.get("type") == "下跌")

    # ---------------- 一类买卖点：该级别的背驰点（第 53 课）
    for b in beichi:
        if b["kind"] == "趋势背驰":
            pass                          # 严格口径：趋势背驰 → 一类点
        elif b["kind"] == "盘整背驰" and allow_panzheng_b1:
            pass                          # 放宽口径：盘整背驰也算一类点（chan.py 的 1p）
        else:
            continue                      # 27 课：盘整背驰默认只给二三买，不给一类点
        idx_c = b["seg_c"]
        seg_c = segs[idx_c]
        zs_b = zs_list[b["zs_b"]] if b.get("zs_b") is not None else None
        t = None
        if trend_types:
            t = next((x for x in trend_types if x["zs_from"] <= (b.get("zs_b") or 0) <= x["zs_to"]), None)
        if b["dir"] < 0:                 # 向下背驰 → 一类买点
            # 第 21 课原话：「中枢下的买点，只可能存在于下跌与盘整的走势类型中」——
            # 所以**盘整也允许**一买，只有上涨趋势才排除。
            # 之前写成「必须等于下跌」，把盘整里的一类买点全滤掉了（实测 B1 恒为 0 的一层原因）。
            if require_b1_in_down and t is not None and t.get("type") == "上涨":
                continue
            # 位置条件：必须在中枢下方（第 21 课）
            if zs_b is not None and not (seg_c["end_px"] < zs_b["ZD"]):
                continue
            pts.append(
                {
                    "kind": "B1", "level": 1,
                    "px": float(seg_c["end_px"]), "raw": int(seg_c["end_raw"]),
                    "stop": float(seg_c["end_px"]),
                    "evidence": f"下跌{b['kind']}（{b['evidence']}），且位于中枢下方",
                    "approx": b.get("engine_approx"),
                }
            )
        else:                            # 向上背驰 → 一类卖点
            if t is not None and t.get("type") == "下跌":
                continue                 # 与买点对称：下跌走势里不出一类卖点
            if zs_b is not None and not (seg_c["end_px"] > zs_b["ZG"]):
                continue
            pts.append(
                {
                    "kind": "S1", "level": 1,
                    "px": float(seg_c["end_px"]), "raw": int(seg_c["end_raw"]),
                    "stop": float(seg_c["end_px"]),
                    "evidence": f"上涨{b['kind']}（{b['evidence']}），且位于中枢上方",
                    "approx": b.get("engine_approx"),
                }
            )

    # ---------------- 二类买卖点：一买/一卖后第一次次级别回调（第 17/53 课）
    b1s = [p for p in pts if p["kind"] in ("B1", "S1")]
    for p in b1s:
        idx = next((i for i, s in enumerate(segs) if int(s["end_raw"]) == p["raw"]), None)
        if idx is None or idx + 2 >= len(segs):
            continue
        rebound, back = segs[idx + 1], segs[idx + 2]
        if p["kind"] == "B1" and int(rebound["dir"]) > 0 and int(back["dir"]) < 0:
            # 53 课给了两个并列判据：不创前低 **或** 盘整背驰。
            not_new_low = float(back["end_px"]) > float(p["px"])
            pan_beichi = False
            if hist is not None:
                try:
                    from . import bei as _b
                    a_reb = _b.seg_area(rebound, hist)      # 反弹段力度
                    a_back = _b.seg_area(back, hist)        # 回调段力度
                    pan_beichi = a_reb > 0 and a_back < a_reb   # 回调力度小于反弹 → 盘整背驰
                except Exception:
                    pan_beichi = False
            if not_new_low or pan_beichi:
                pts.append(
                    {
                        "kind": "B2", "level": 1,
                        "px": float(back["end_px"]), "raw": int(back["end_raw"]),
                        # 止损取「一买低点」与「本次回调低点」中更低的那个：
                        # 回调创新低（靠盘整背驰成立）时，失效位必须跟着新低走，
                        # 否则会出现 stop 高于买点价的倒挂，次日必被止损打掉（质检抓到的 bug）。
                        "stop": float(min(p["px"], back["end_px"])),
                        "evidence": (
                            f"一买后回调不破前低 {p['px']:.2f}（第 17 课）" if not_new_low
                            else f"一买后回调出现盘整背驰（回调力度<反弹力度，第 53 课）"
                        ),
                        "approx": None if not_new_low else "盘整背驰用线段 MACD 面积比较代理",
                    }
                )
        if p["kind"] == "S1" and int(rebound["dir"]) < 0 and int(back["dir"]) > 0:
            not_new_high = float(back["end_px"]) < float(p["px"])
            pan_beichi_s = False
            if hist is not None:
                try:
                    from . import bei as _b
                    a_reb = _b.seg_area(rebound, hist)
                    a_back = _b.seg_area(back, hist)
                    pan_beichi_s = a_reb > 0 and a_back < a_reb
                except Exception:
                    pan_beichi_s = False
            if not_new_high or pan_beichi_s:
                pts.append(
                    {
                        "kind": "S2", "level": 1,
                        "px": float(back["end_px"]), "raw": int(back["end_raw"]),
                        "stop": float(max(p["px"], back["end_px"])),
                        "evidence": (
                            f"一卖后反弹不创新高 {p['px']:.2f}（第 53 课）" if not_new_high
                            else "一卖后反弹出现盘整背驰（反弹力度<下跌力度，第 53 课）"
                        ),
                        "approx": None if not_new_high else "盘整背驰用线段 MACD 面积比较代理",
                    }
                )

    # ---------------- 中枢在「所属走势类型」中的序号（第 79 课要用）
    # 79 课：「尽量只介入第一个中枢的第三类买点。因为第二个中枢以后，
    #          形成大级别中枢的概率将急促加大」——必须知道该中枢是不是本走势的第一个。
    trend_of: dict[int, tuple[int, int]] = {}
    for _ti, _t in enumerate(trend_types or []):
        for _kk in range(int(_t["zs_from"]), int(_t["zs_to"]) + 1):
            trend_of[_kk] = (_ti, _kk - int(_t["zs_from"]) + 1)

    # ---------------- 三类买卖点：第一次回抽不回中枢（第 20/38/61 课）
    for k, z in enumerate(zs_list):
        e = int(z["end_seg"])
        if e + 2 >= len(segs):
            continue
        leave, pull = segs[e + 1], segs[e + 2]
        # 61 课：至少 5 段（中枢 3 段 + 离开 + 回抽）
        if int(z["n_seg"]) < 3:
            continue
        if int(leave["dir"]) > 0 and float(leave["end_px"]) > float(z["ZG"]):
            if int(pull["dir"]) < 0 and float(pull["end_px"]) > float(z["ZG"]):
                pts.append(
                    {
                        "kind": "B3", "level": 1,
                        "px": float(pull["end_px"]), "raw": int(pull["end_raw"]),
                        "stop": float(z["ZG"]),
                        "zs_in_trend": trend_of.get(k, (None, None))[1],
                        "trend_idx": trend_of.get(k, (None, None))[0],
                        # 第 54 课「买点级别决定仓位」的量化代理：中枢段数越多，级别越大
                        "zs_nseg": int(z["n_seg"]),
                        "evidence": (
                            f"中枢(第{k+1}个,本走势第{trend_of.get(k, (None, None))[1]}个,"
                            f"[{z['ZD']:.2f},{z['ZG']:.2f}])后第 1 段向上离开、"
                            f"第 1 段回抽低点 {pull['end_px']:.2f} > ZG，共 {int(z['n_seg'])+2} 段"
                        ),
                        "approx": "次级别走势类型用线段代理",
                    }
                )
        if int(leave["dir"]) < 0 and float(leave["end_px"]) < float(z["ZD"]):
            if int(pull["dir"]) > 0 and float(pull["end_px"]) < float(z["ZD"]):
                pts.append(
                    {
                        "kind": "S3", "level": 1,
                        "px": float(pull["end_px"]), "raw": int(pull["end_raw"]),
                        "stop": float(z["ZD"]),
                        "evidence": (
                            f"中枢(第{k+1}个,[{z['ZD']:.2f},{z['ZG']:.2f}])后第 1 段向下离开、"
                            f"第 1 段反抽高点 {pull['end_px']:.2f} < ZD，共 {int(z['n_seg'])+2} 段"
                        ),
                        "approx": "次级别走势类型用线段代理",
                    }
                )

    pts.sort(key=lambda x: x["raw"])
    return pts


def invariants(pts: list[dict], segs: list[dict]) -> list[str]:
    """买卖点的硬性质（测试与回归用）。"""
    errs = []
    ends = {int(s["end_raw"]) for s in segs}
    for i, p in enumerate(pts):
        if p["kind"] not in ("B1", "B2", "B3", "S1", "S2", "S3"):
            errs.append(f"第{i}个点类型非法 {p['kind']}")
        if p["raw"] not in ends:
            errs.append(f"第{i}个点不在任何线段端点上（不可回溯）")
        if p["kind"].startswith("B") and not (float(p["px"]) > 0):
            errs.append(f"第{i}个买点价格非正")
        if "stop" not in p:
            errs.append(f"第{i}个点缺结构止损价")
        # 只拦「倒挂」：买点价低于止损价 / 卖点价高于止损价。
        # 允许相等——B1/S1 的结构失效位就是该背驰笔的低/高点，等于买点价本身，
        # 实际入场在下一根开盘价，止损距离由 stop_buffer 提供。
        elif p["kind"].startswith("B") and float(p["px"]) < float(p["stop"]):
            errs.append(f"第{i}个买点价 {p['px']} 低于止损价 {p['stop']}（止损倒挂）")
        elif p["kind"].startswith("S") and float(p["px"]) > float(p["stop"]):
            errs.append(f"第{i}个卖点价 {p['px']} 高于止损价 {p['stop']}（止损倒挂）")
    # 排序单调
    raws = [p["raw"] for p in pts]
    if raws != sorted(raws):
        errs.append("买卖点未按时间排序")
    return errs
