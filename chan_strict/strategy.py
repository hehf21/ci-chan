"""
严格版策略（chan_strict 的决策层）
==================================
只用严格买卖点，不掺近似规则。每条规则标原文出处。

操作级别：**30 分钟**（实测日线线段级别 6.5 年只有 0~1 个买卖点，没法做策略；
详见 进度.md「买卖点密度」）。大级别用日线走势类型做方向过滤。

规则与原文对应：
  ① 买点买、卖点卖（41 课）：只在 B1/B2/B3 上买，只在 S1/S2/S3 或结构失效上卖
  ② 对应级别（41 课）：30 分钟买卖点 vs 日线方向相反且日线下跌未完成时，不接 B1/B2（只接 B3）
  ③ 不参与中枢震荡（53 课）：中枢内部不出手，只在中枢完成后的第一次回抽（B3/S3）动手
  ④ 不加码、一次性买入、留机动资金（31 课）：已有持仓不再买；现金保留 reserve_cash
  ⑤ 止损用结构失效价，不用百分比（13 课：走势从能搞变成不能搞就走，反对按盈亏止蚀）
  ⑥ 卖出后要能等，买点没来就空仓（45 课：操作只有两个字——等待）
"""

from __future__ import annotations

DEFAULT_CFG = {
    "max_positions": 3,
    "max_weight_per_stock": 0.30,
    "order_weight": {"B1": 0.10, "B2": 0.10, "B3": 0.15},
    "cooldown_bars": 8,          # 同一票卖出后至少隔 8 根 30 分钟（约 1 个交易日）再买
    "reserve_cash": 0.10,        # 31 课：留 1/10 机动资金
    "use_daily_filter": True,    # 41 课：大级别方向过滤
    "no_add_position": True,     # 31 课：不加码
    # 入场不追价（41 课「买点只在下跌中形成」）：只在买点价上方这么一点以内挂限价单接，
    # 接不到就作废。这样入场价贴近买点，结构止损的距离才收得住。
    "entry_slack": 0.005,
    "limit_valid_bars": 8,       # 限价单有效期（8 根 30 分钟 ≈ 1 个交易日）
    # 风险预算（31 课：资金管理比技术更重要）：每笔最多亏总资产的这个比例。
    # 止损位按原文不动（B3=中枢上沿 ZG），改成「止损远就自动减仓」，
    # 这样才不用为了控制亏损去篡改原文的结构失效定义。
    "risk_per_trade": 0.01,
    # 区间套闸门（61 课）：大级别（日线）近期出现过同向买卖点，才允许本级别买点下单。
    # 默认关闭：实测日线买卖点极稀有（6.5 年 0~1 个），打开会把交易掐到几乎为零。
    # 打开方式：cfg["require_daily_parent"]=True，并在 state["daily_parent_ok"] 传布尔。
    "require_daily_parent": False,
}


def _pkey(p: dict, offset: int = 0) -> tuple:
    """买卖点的稳定身份。

    **必须用绝对 bar 位置**：结构是在滑动窗口上算的，同一个逻辑点在不同 bar 上
    的 raw（窗口内下标）会变，只用 raw 去重会把同一个点当成新点反复触发
    （实测：2 只票 2.8 年跑出 132 笔成交，胜率 24%，全是重复进出）。
    offset = 窗口起点在原序列中的下标，由调用方传入。
    """
    return (p["kind"], int(offset) + int(p["raw"]), round(float(p["px"]), 4))


def _fresh(res: dict, state: dict) -> list[dict]:
    """取「结构上第一次出现」的买卖点。

    为什么不按「价格极值距当前多少根」限制：严格结构要等线段确认（78 课），
    信号出生时价格极值往往已经过去几十根，用 TTL 会把信号全部误杀
    （实测：30 分钟 5456 根回测 0 笔成交，就是这个原因）。
    正确时点是**结构确认的那一根 bar**，所以用「首次出现」判定，见 seen 集合。
    """
    seen = state.setdefault("seen", set())
    out = []
    for p in res["points"]:
        k = ("fire",) + _pkey(p, state.get("bar_offset", 0))
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def decide(pos: dict | None, res: dict, state: dict, cfg: dict | None = None) -> list[dict]:
    """给出当前 bar 的动作。

    pos: None 或 {"shares","cost","stop","entry_ts","entry_kind"}
    res: chan_strict.pipeline.analyze() 的结果
    state: {"fired": set, "daily_trend": {...}, "n_positions": int, "total_asset": float,
            "bars_since_exit": int}
    """
    c = {**DEFAULT_CFG, **(cfg or {})}
    acts: list[dict] = []
    close = float(res["last_close"])
    fired = state.setdefault("fired", set())
    # 每根 bar 只取一次「首次出现的点」，买卖两侧共用（重复调用会把买点也标记成已出现）
    _off = int(state.get("bar_offset", 0))
    new_pts = _fresh(res, state)

    # ---------------- 一、持仓：先卖（53 课）
    if pos:
        stop = pos.get("stop")
        if stop and close < float(stop):
            acts.append({"action": "SELL", "qty": "all",
                         "reason": f"跌破结构失效价 {float(stop):.2f}（13 课：走势不能搞了就走）",
                         "mark": None})
            return acts
        for p in new_pts:
            if p["kind"] in ("S1", "S2", "S3") and ("fire",) + _pkey(p, _off) not in fired:
                acts.append({"action": "SELL", "qty": "all",
                             "reason": f"{p['kind']} 卖点：{p['evidence'][:60]}",
                             "mark": ("fire",) + _pkey(p, _off)})
                return acts

    # ---------------- 二、空仓/建仓：只认买点（41 课）
    if pos and c["no_add_position"]:
        return acts                                   # 31 课：已有持仓不加码
    if state.get("paused"):
        return acts
    if pos is None and state.get("n_positions", 0) >= c["max_positions"]:
        return acts
    if state.get("bars_since_exit", 9999) < c["cooldown_bars"]:
        return acts

    daily = state.get("daily_trend") or {}
    daily_dir = daily.get("type")            # "上涨"/"下跌"/"盘整"/"无"
    # 注意：走势类型的"完成"判据原文没有（摘录 14.1 第 6 条），本实现的 complete 字段对末段恒 False，
    # 拿它做条件等于恒真（质检 F3 已证）。这里只用方向，不再假装用了"完成"。

    if c.get("require_daily_parent") and not state.get("daily_parent_ok", False):
        return acts                            # 区间套闸门：大级别没有点就不做（61 课）
    order = {"B3": 1, "B2": 2, "B1": 3}      # 21 课：三买最不易亏，优先
    cands = [p for p in new_pts if p["kind"] in order]
    cands.sort(key=lambda x: (order[x["kind"]], -int(x["raw"])))

    for p in cands:
        mark = ("fire",) + _pkey(p, _off)
        if mark in fired:
            continue
        # 41 课：大级别下跌时不接逆势的一、二类买点（只接顺势的三买）
        if c["use_daily_filter"] and daily_dir == "下跌":
            if p["kind"] in ("B1", "B2"):
                continue
        w = c["order_weight"][p["kind"]]
        total_asset = state.get("total_asset") or 0.0
        if total_asset > 0:
            w = min(w, c["max_weight_per_stock"])
        # 按风险预算收缩：仓位 = 单笔风险预算 / 止损距离比例
        rp = c.get("risk_per_trade")
        if rp:
            lim = float(p["px"]) * (1 + c["entry_slack"])
            risk_frac = max((lim - float(p["stop"])) / lim, 1e-6)
            w = min(w, rp / risk_frac)
        acts.append({
            "action": "BUY", "weight": w,
            "limit_px": float(p["px"]) * (1 + c["entry_slack"]),
            "reason": f"{p['kind']} 买点：{p['evidence'][:70]}",
            "stop": float(p["stop"]),
            "entry_kind": p["kind"],
            "mark": mark,
        })
        return acts                               # 一根 bar 只开一个仓
    return acts


def market_filter(daily_res: dict | None) -> dict:
    """把日线结构压成一个大级别过滤结果（41 课「对应级别」）。"""
    if not daily_res:
        return {"type": "无", "complete": False}
    t = daily_res.get("trend") or {}
    return {"type": t.get("type", "无"), "complete": bool(t.get("complete", False))}
