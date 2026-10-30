"""
线段（严格实现：第 62 / 65 / 67 / 71 / 77 / 78 / 81 课）
=======================================================
原文逐字见 `策略/_规则依据-原文摘录.md` 第四节（第 114~256 行），本文件每一处判断都
对应下面某条原文，行号对照表见 `策略/audit/seg-对照.md`。

规则链（按原文修订顺序，后者覆盖前者的冲突处）：
  62 课：线段＝至少三笔；图 8 是最基本形态，图 9 是线段破坏。
  65 课：线段前三笔**必须有重叠**；给出「线段被笔破坏」的可计算判据
         `j>=i+2 且 dj<=gi`（向上段），并声明「线段破坏的充要条件是被另一个线段破坏」。
  67 课：正式划分标准 —— 特征序列（向上段由向下笔构成，反过来也一样）→ 包含处理得到
         **标准特征序列** → 只考察与线段同向的那个分型（向上段只看顶分型）→
         按「分型第一、二元素间是否有缺口」分成两种情况。
  71 课：包含关系只能在**同一特征序列内部**做；假设转折点前后那两个元素不做包含，
         缺口就靠它们判定；转折点右侧的分型元素可以做包含。并给出程序化判别步骤。
  77 课：重申至少三笔、前三笔必须重叠，并给出「第二种情况的第二特征序列不再分情况」
         的简化理由（避免线段串无限收敛）。
  78 课：方向约束（线段必是奇数笔、起止同向、顶必须高于底）；线段的破坏可逆时间传递；
         笔破坏 ≠ 线段破坏；**第二种情况的第二特征序列必须严格做包含处理**。
  81 课：更正 71 课最后一张图（7 画高了）。正确口径：5=7 或 5 低于 7 是一段，
         5 高于 7 才是三段 —— 也就是「新高没有被特征序列分型确认，线段就不算断」。

刻意如实标注的三处工程口径（原文没给，不是假装严格）：
  ① 特征序列元素的下标算法：原文只有「假设转折点 → 用两种情况考察 → 不满足则延续」
     这段文字程序（71 课），没有完整伪代码。本实现取「候选终点逐笔右移 + 每个候选点
     都把左侧特征序列重新做一次包含处理」的等价做法，见 `_find_break`。
  ② 第二种情况的确认门槛：67 课字面要求「第二特征序列出现分型」，而特征序列的一个分型
     至少要三个元素，也就是反向至少要走出 6 笔才算确认。本实现照字面执行（gap_mode=
     "strict"）；另给 gap_mode="loose" 这一放宽口径（反向三笔有重叠即算），仅为对照，
     默认不用。
  ③ 分型判定的两条件：一般 K 线顶分型要求中间那根「高点最高且低点最高」。对特征序列，
     本实现只比较「线段方向上的极值」（向上段比高点、向下段比低点）。原因：做完包含
     处理后，非边界元素的两个条件等价；而 71 课明确规定假设转折点两侧元素**不能**做
     包含处理，若仍要求「低点也最高」会在这种边界上把合法的顶分型判掉。
"""

from __future__ import annotations

GAP_MODES = ("strict", "loose")


# =============================================================== 0. 入参校验
def _validate(bis: list[dict]) -> None:
    """把「笔序列」的隐含前提显式检查掉，免得后面算出似是而非的线段。"""
    need = ("dir", "start_bar", "end_bar", "start_px", "end_px", "start_raw", "end_raw")
    for i, b in enumerate(bis):
        miss = [k for k in need if k not in b]
        if miss:
            raise ValueError(f"第{i}笔缺字段 {miss}")
        if b["dir"] not in (1, -1):
            raise ValueError(f"第{i}笔 dir 必须是 ±1，实际 {b['dir']}")
        if b["end_bar"] < b["start_bar"]:
            raise ValueError(f"第{i}笔 end_bar < start_bar")
        # 78 课：向上笔一定从底走到顶（终点高于起点），向下笔反之。
        # 这条也顺带保证「未确认尾段取方向极值笔」时一定取得到合法的端点。
        if (b["end_px"] - b["start_px"]) * b["dir"] <= 0:
            raise ValueError(f"第{i}笔 dir={b['dir']} 与价格方向矛盾：{b['start_px']}→{b['end_px']}")
        if i and bis[i - 1]["dir"] == b["dir"]:
            raise ValueError(f"第{i}笔与第{i-1}笔同向：笔序列必须上下交替（78 课「线段和笔，都是有方向的」）")


# =============================================================== 1. 特征序列元素
def _elem(bis: list[dict], i: int) -> dict:
    """把第 i 笔看成一根「K 线」（67 课：「把每一元素看成是一K线」）。

    hi/lo 就是这根虚拟 K 线的高低；hi_bi/lo_bi 记住这个极值落在哪一笔上，
    因为「顶分型的高点」最终要换算回某一笔的端点（线段端点必须落在笔端点上）。
    """
    b = bis[i]
    hi = max(b["start_px"], b["end_px"])
    lo = min(b["start_px"], b["end_px"])
    return {"hi": hi, "lo": lo, "hi_bi": i, "lo_bi": i, "n": 1, "bis": [i]}


def _feat_idx(bis: list[dict], lo: int, hi: int, d: int) -> list[int]:
    """[lo, hi) 区间内属于「方向为 d 的线段」的特征序列笔：方向与线段相反（67 课）。"""
    return [i for i in range(lo, hi) if bis[i]["dir"] == -d]


def _merge(elems: list[dict], seed_up: bool | None = None) -> list[dict]:
    """特征序列的包含处理 → 标准特征序列（67 课）。

    合并方向按第 65 课的 K 线口径：拿「已合并的最后一根」与「再前一根」比高点定方向，
    向上取 (max hi, max lo)，向下取 (min hi, min lo)。顺序原则：永远拿合并结果去比下一根，
    不做传递律的一次性极值（65 课第 2 条）。
    `seed_up` 只用于右侧序列的第一对元素（此时左侧没有可比的第三根）。
    """
    out: list[dict] = []
    for e in elems:
        e = dict(e, bis=list(e["bis"]))
        if not out:
            out.append(e)
            continue
        last = out[-1]
        # 包含关系：一根完全套住另一根
        contained = (e["hi"] <= last["hi"] and e["lo"] >= last["lo"]) or (
            e["hi"] >= last["hi"] and e["lo"] <= last["lo"]
        )
        if not contained:
            out.append(e)
            continue
        if len(out) >= 2:
            up = last["hi"] > out[-2]["hi"]
        elif seed_up is not None:
            up = seed_up
        else:
            up = e["hi"] > last["hi"]
        if up:
            hi, lo = max(last["hi"], e["hi"]), max(last["lo"], e["lo"])
        else:
            hi, lo = min(last["hi"], e["hi"]), min(last["lo"], e["lo"])
        hi_bi = last["hi_bi"] if last["hi"] >= e["hi"] else e["hi_bi"]
        lo_bi = last["lo_bi"] if last["lo"] <= e["lo"] else e["lo_bi"]
        out[-1] = {
            "hi": hi,
            "lo": lo,
            "hi_bi": hi_bi,
            "lo_bi": lo_bi,
            "n": last["n"] + e["n"],
            "bis": last["bis"] + e["bis"],
        }
    return out


def feature_sequence(bis: list[dict], start_bi: int, end_bi: int, merged: bool = True) -> list[dict]:
    """线段 [start_bi, end_bi] 的特征序列（67 课）。

    merged=True 返回**标准特征序列**（已做包含处理）；False 返回未处理的原始元素，
    只用于对照测试 —— 原文说「以后没有特别说明，特征序列都是指标准特征序列」。
    """
    d = bis[start_bi]["dir"]
    idx = _feat_idx(bis, start_bi + 1, end_bi + 1, d)
    elems = [_elem(bis, i) for i in idx]
    return _merge(elems) if merged else elems


def _edge(e: dict, d: int) -> float:
    """元素在「线段方向」上的极值，取成「越大越极端」，方便向上/向下段共用一套比较。

    向上段看高点（段顶在最高处），向下段看低点（段底在最低处）——取负后同样「越大越极端」。
    """
    return e["hi"] if d > 0 else -e["lo"]


def _edge_bi(e: dict, d: int) -> int:
    return e["hi_bi"] if d > 0 else e["lo_bi"]


# =============================================================== 2. 65 课：笔破坏
def bi_breakdown(bis: list[dict], start_bi: int, end_bi: int | None = None) -> dict | None:
    """65 课「线段被笔破坏」的判据（**只作诊断，绝不用于判定线段结束**）。

    向上段：分型序列 d1g1d2g2…，若存在 i、j 且 j>=i+2 使 dj<=gi，则向上段被笔破坏。
    向下段：对称，gj>=di。
    返回第一个成立的 (i, j) 及其对应的笔下标；不成立返回 None。
    """
    if len(bis) == 0:
        return None
    d = bis[start_bi]["dir"]
    end = len(bis) - 1 if end_bi is None else end_bi
    tops: list[tuple[float, int]] = []
    bots: list[tuple[float, int]] = []
    for i in range(start_bi, end + 1):
        if bis[i]["dir"] != d:
            continue
        if d > 0:      # 向上笔：起点是底 d，终点是顶 g
            bots.append((bis[i]["start_px"], i))
            tops.append((bis[i]["end_px"], i))
        else:          # 向下笔：起点是顶 g，终点是底 d
            tops.append((bis[i]["start_px"], i))
            bots.append((bis[i]["end_px"], i))
    for i in range(1, len(tops) + 1):                 # i 从 1 开始，对应原文的序号
        for j in range(i + 2, len(bots) + 1):
            if d > 0 and bots[j - 1][0] <= tops[i - 1][0]:
                return {"i": i, "j": j, "i_bi": tops[i - 1][1], "j_bi": bots[j - 1][1],
                        "gap_broken": True}
            if d < 0 and tops[j - 1][0] >= bots[i - 1][0]:
                return {"i": i, "j": j, "i_bi": bots[i - 1][1], "j_bi": tops[j - 1][1],
                        "gap_broken": True}
    return None


# =============================================================== 3. 65 课：前三笔重叠
def three_bi_overlap(bis: list[dict], s: int) -> bool:
    """65 课：「线段的前三笔，必须有重叠的部分」。区间取三笔 [低, 高] 的交。"""
    if s + 2 >= len(bis):
        return False
    lo = max(min(b["start_px"], b["end_px"]) for b in bis[s : s + 3])
    hi = min(max(b["start_px"], b["end_px"]) for b in bis[s : s + 3])
    return lo < hi


# =============================================================== 4. 第二特征序列
def _second_feature_fractal(bis: list[dict], b: int, d: int) -> tuple[bool, int | None]:
    """第二种情况的「第二特征序列」分型（67 课 + 78 课）。

    b：假设转折点之后的第一笔（也就是反向走势的第一笔）。
    方向 d 的线段被反向破坏，反向段的方向是 -d，其特征序列由方向为 d 的笔构成，
    需要出现的分型是：向上段 → 底分型；向下段 → 顶分型（67 课「不分第一二种情况，
    只要有分型就可以」）。
    78 课：「必须严格按照包含关系的处理来」—— 这里整段做包含处理，**不做** 71 课那种
    边界豁免（71 课的边界豁免只针对第一种情况的第一、二元素）。
    """
    elems = [_elem(bis, i) for i in _feat_idx(bis, b + 1, len(bis), -d)]
    merged = _merge(elems)
    for k in range(1, len(merged) - 1):
        m, prev, nxt = merged[k], merged[k - 1], merged[k + 1]
        if d > 0:      # 原线段向上 → 反向段向下 → 找底分型（中间元素最低）
            if m["lo"] < prev["lo"] and m["lo"] < nxt["lo"]:
                return True, m["lo_bi"]
        else:          # 原线段向下 → 反向段向上 → 找顶分型（中间元素最高）
            if m["hi"] > prev["hi"] and m["hi"] > nxt["hi"]:
                return True, m["hi_bi"]
    return False, None


# =============================================================== 5. 分界点判别
def _judge_point(bis: list[dict], s: int, e: int, A: dict, gap_mode: str) -> dict | None:
    """71 课「假设某转折点 → 用两种情况考察」：单独考察「第 e 笔是否就是段的最后一笔」。

    s：线段起点（首笔下标）；e：候选终点；A：本段左侧标准特征序列的最后一个元素。
    通过返回 {"end", "kind", "gap", "confirm_bi"}，不通过返回 None。

      ① A 就是 71 课说的「第一元素：假设转折点前线段的最后一个特征元素」；
      ② 转折点后第一笔 B = 第 e+1 笔，即「第二元素」，**A 与 B 之间不做包含处理**，
         缺口只靠这两个原始元素判（71 课原文）；
      ③ B 及其右侧元素可做包含处理（71 课末段），并要求分型的中间元素仍落在 B 这一笔上、
         且比右侧下一个标准元素更极端（= 顶/底分型成立）；
      ④ 无缺口 → 第一种情况；有缺口 → 要第二特征序列出现分型（67 课）且必须做包含处理（78 课）；
      ⑤ 78 课「同一线段中，两端的一顶一底，顶肯定要高于底」也一并卡住。
    """
    d = bis[s]["dir"]
    n = len(bis)
    B = _elem(bis, e + 1)                   # ② 转折点后第一笔，与 A 不同特征序列，不合并
    if _edge(B, d) <= _edge(A, d):          # 中间元素不够极端，构不成顶/底分型
        return None
    # ③ 右侧元素可做包含处理（71 课：转折点后的顶分型元素可以考察包含关系）
    right = _merge(
        [B] + [_elem(bis, i) for i in _feat_idx(bis, e + 3, n, d)],
        seed_up=(B["hi"] > A["hi"]),
    )
    M1 = right[0]
    if _edge_bi(M1, d) != e + 1:            # 更高/更低的极值出现在后面 → 线段还在延伸
        return None
    if len(right) < 2:                      # 分型还缺第三个元素
        return None
    if _edge(M1, d) <= _edge(right[1], d):  # 不是分型（右侧还有更极端的）
        return None
    # ⑤ 78 课：向上段顶必须高于底，向下段底必须低于顶
    if (bis[e]["end_px"] - bis[s]["start_px"]) * d <= 0:
        return None
    # ④ 缺口判定：两个原始元素之间没有重合区间（67 课）
    gap = (B["hi"] < A["lo"]) or (B["lo"] > A["hi"])
    if not gap:
        return {"end": e, "kind": "第一种情况", "gap": False, "confirm_bi": right[1]["bis"][0]}
    if gap_mode == "loose":
        # 放宽口径（非原文）：反向三笔有重叠即认为新线段成立
        if e + 2 < n and three_bi_overlap(bis, e + 1):
            return {"end": e, "kind": "特征序列分型", "gap": True, "confirm_bi": e + 1}
        return None
    ok, mid = _second_feature_fractal(bis, e + 1, d)
    if ok:
        return {"end": e, "kind": "第二种情况", "gap": True, "confirm_bi": mid}
    return None


def _find_break(bis: list[dict], s: int, gap_mode: str) -> dict | None:
    """71 课的程序化判别主循环：候选终点从 s+2 起逐笔右移，第一个通过考察的就是分界点。

    78 课：线段必是奇数笔且起止同向，所以候选终点 e 与 s 同向、步长 2。
    左侧标准特征序列用「增量合并」维护（顺序原则下与每次从头合并完全等价，
    但把每个候选点的左侧成本从 O(n) 降到 O(1)）。
    """
    d = bis[s]["dir"]
    n = len(bis)
    left = _merge([_elem(bis, s + 1)])      # 候选 e=s+2 时的左侧特征序列（只有第 s+1 笔）
    e = s + 2
    while e + 1 < n:                        # 至少要有 B 这一笔
        if left:
            found = _judge_point(bis, s, e, left[-1], gap_mode)
            if found is not None:
                return found
        # 推进到下一个候选点：第 e+1 笔（方向与段相反）并入左侧标准特征序列
        left = _merge(left + [_elem(bis, e + 1)])
        e += 2
    return None


# =============================================================== 6. 组装
def _make_seg(bis: list[dict], s: int, e: int, kind: str, confirmed: bool, info: dict | None) -> dict:
    """按接口约定打包一段。端点一律取自笔端点，绝不自己插值。"""
    info = info or {}
    return {
        "dir": bis[s]["dir"],
        "start_bi": s,
        "end_bi": e,
        "start_px": bis[s]["start_px"],
        "end_px": bis[e]["end_px"],
        "start_bar": bis[s]["start_bar"],
        "end_bar": bis[e]["end_bar"],
        "start_raw": bis[s]["start_raw"],
        "end_raw": bis[e]["end_raw"],
        "confirmed": confirmed,
        "broken_by": kind,
        # --- 以下为附加诊断字段（接口约定之外，主程序可以不用） ---
        "n_bi": e - s + 1,
        "gap": info.get("gap"),
        "confirm_bi": info.get("confirm_bi"),
    }


def build_segments(bis: list[dict], gap_mode: str = "strict") -> list[dict]:
    """把笔序列划分成线段（严格实现，逐条对应 62/65/67/71/77/78/81 课）。

    bis：笔序列，每笔 dict：dir(+1 向上/-1 向下), start_bar, end_bar, start_px, end_px,
         start_raw, end_raw（start_bar/end_bar 是处理后 K 线下标，*_raw 是原始 K 线下标）。
    gap_mode："strict"（默认，67 课字面口径）/ "loose"（放宽，仅作对照）。

    返回线段列表：dir, start_bi, end_bi, start_px, end_px, start_bar, end_bar,
    start_raw, end_raw, confirmed, broken_by（另附 n_bi/gap/confirm_bi 三个诊断字段）。

    重要：`confirmed=False` 的尾段是**还在走**的线段，按 78 课「线段最终肯定都会被线段破坏」
    它随时会变，主程序不许把它当已完成线段喂给中枢模块。
    """
    if gap_mode not in GAP_MODES:
        raise ValueError(f"gap_mode 只支持 {GAP_MODES}，实际 {gap_mode!r}")
    _validate(bis)
    n = len(bis)
    if n < 3:                                   # 77 课：线段至少三笔
        return []

    segs: list[dict] = []
    s = 0
    while s + 2 < n:
        if not three_bi_overlap(bis, s):
            # 65 课：前三笔没有重叠就不构成线段——但只说明**这个起点**不成段，
            # 起点往后挪一笔继续找。这里早期写成 break，会把后面所有线段一起丢掉
            # （实测：600519 30分钟 4000 根时空窗起点不重叠 → 段数直接变 0）。
            s += 1
            continue
        found = _find_break(bis, s, gap_mode)
        if found is None:
            # 没被破坏：按 78 课，最后这一段是未确认的。
            # 未确认线段的终点取「该方向上的极值笔」——不能简单取最后一根同向笔：
            # 如果价格一路反向走，最后一根同向笔的终点会低于起点，产出「向下却标成向上」的非法段
            # （随机性质测试 seed=24 抓到过：59 笔被并成一段，dir=+1 但 100→81.36）。
            d = bis[s]["dir"]
            # cand 必然包含 s 自己；而 _validate 已保证 bis[s] 的终点在段方向上越过了起点，
            # 所以这里取到的极值一定比起点更极端，78 课「顶高于底」不会被违反。
            cand = [i for i in range(s, n) if bis[i]["dir"] == d]
            best = max(cand, key=lambda i: bis[i]["end_px"] * d)
            if best - s >= 2:
                segs.append(_make_seg(bis, s, best, "未确认", False, None))
            break
        segs.append(_make_seg(bis, s, found["end"], found["kind"], True, found))
        s = found["end"] + 1
    return segs


def confirmed_segments(segs: list[dict]) -> list[dict]:
    """只保留已确认的线段 —— 喂中枢模块前必须先过这一层。

    78 课：「线段最终肯定都会被线段破坏」，所以最后一根（`confirmed=False`）随时会变长、
    变短甚至消失。把它当成「完成的部件」去构成中枢，中枢级别会来回跳。
    """
    return [s for s in segs if s.get("confirmed")]


def check_seg_invariants(bis: list[dict], segs: list[dict]) -> list[str]:
    """线段的硬性质检查（测试与回归共用）。返回违规描述列表，空 = 全部通过。"""
    errs: list[str] = []
    if len(segs) > len(bis):
        errs.append(f"线段数 {len(segs)} 大于笔数 {len(bis)}")
    for k, sg in enumerate(segs):
        if not (0 <= sg["start_bi"] <= sg["end_bi"] < len(bis)):
            errs.append(f"第{k}段笔区间越界 {sg['start_bi']}~{sg['end_bi']}")
            continue
        if sg["n_bi"] < 3 or sg["n_bi"] % 2 == 0:
            errs.append(f"第{k}段笔数 {sg['n_bi']} 不合法（78 课：线段必为奇数笔且至少三笔）")
        if sg["start_px"] != bis[sg["start_bi"]]["start_px"]:
            errs.append(f"第{k}段 start_px 与笔端点不符")
        if sg["end_px"] != bis[sg["end_bi"]]["end_px"]:
            errs.append(f"第{k}段 end_px 与笔端点不符")
        if sg["dir"] != bis[sg["start_bi"]]["dir"]:
            errs.append(f"第{k}段 dir 与首笔方向不符")
        if k:
            prev = segs[k - 1]
            if prev["dir"] == sg["dir"]:
                errs.append(f"第{k}段与上一段同向（线段方向必须交替）")
            if sg["start_bi"] != prev["end_bi"] + 1:
                errs.append(f"第{k}段起点未接上一段终点")
        if sg["dir"] > 0 and not sg["end_px"] > sg["start_px"]:
            errs.append(f"第{k}段向上但终点不高于起点（78 课）")
        if sg["dir"] < 0 and not sg["end_px"] < sg["start_px"]:
            errs.append(f"第{k}段向下但终点不低于起点（78 课）")
        if sg["confirmed"] and sg["broken_by"] not in ("第一种情况", "第二种情况", "特征序列分型"):
            errs.append(f"第{k}段已确认但 broken_by={sg['broken_by']!r}")
        if not sg["confirmed"] and sg["broken_by"] != "未确认":
            errs.append(f"第{k}段未确认但 broken_by={sg['broken_by']!r}")
    return errs


# =============================================================== 7. 自检
def _demo() -> None:
    """用一条构造序列打印划分结果，人手核对用。"""
    prices = [10, 20, 14, 30, 18, 26, 16]
    bis = _mk_bis(prices)
    for gm in GAP_MODES:
        segs = build_segments(bis, gap_mode=gm)
        print(f"[gap_mode={gm}] {len(segs)} 段，违规 {check_seg_invariants(bis, segs) or '无'}")
        for k, sg in enumerate(segs):
            print(
                f"  段{k} dir={sg['dir']:+d} 第{sg['start_bi']}~{sg['end_bi']}笔 "
                f"{sg['start_px']}→{sg['end_px']} confirmed={sg['confirmed']} "
                f"broken_by={sg['broken_by']} gap={sg['gap']}"
            )


def _mk_bis(prices: list[float]) -> list[dict]:
    """把一串折点价格变成笔序列（自检/测试用的最小构造器）。"""
    bis = []
    for i in range(len(prices) - 1):
        a, b = prices[i], prices[i + 1]
        bis.append(
            {
                "dir": 1 if b > a else -1,
                "start_bar": i,
                "end_bar": i + 1,
                "start_px": a,
                "end_px": b,
                "start_raw": i,
                "end_raw": i + 1,
            }
        )
    return bis


if __name__ == "__main__":
    _demo()
