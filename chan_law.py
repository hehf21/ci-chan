"""
缠论逻辑 → 程序语言（参考实现 chan_law）
=========================================

本文件是《缠论逻辑-程序语言.md》的可执行部分：文档里每一个「第 N 节」，这里就是同名函数。
写法上守三条纪律，目的是「逻辑对」可以被检验，而不是看起来像：

[原文]  判据直接来自《教你炒股票》108 课，函数 docstring 里给课次 + 原话要点。
[工程]  原文没给可判定式的地方（分型怎么非极大值、区间套递归到哪停……），一律显式写成约定，
        并在 `APPROX` 常量表里登记，决不当成「原文如此」。
[开关]  原文自相矛盾的地方（18 课 vs 20 课中枢区间、24 课 vs 37 课背驰级别、20 课 vs 33 课延伸
        上限），两套都实现，用参数选，输出里标注用了哪套 —— 不做无声取舍。

依赖：只用 Python 标准库。输入是 Bar 序列（t 为递增整数序号）。

--------------------------------------------------------------------------
总链条（每层只依赖上一层的输出，绝不跨层偷看未来）：
    Bar ──①包含处理──> MF ──②分型──> FX ──③笔──> Bi ──④线段──> Seg
        ──⑤中枢──> ZS ──⑥走势类型──> Trend ──⑦背驰──> BC ──⑧买卖点──> Point
        ──⑨区间套 / ⑩级别递归
--------------------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

EPS = 1e-9

# 工程近似登记表：键 = 编号，值 = 说明。文档第 11 节会原样列出这张表。
APPROX: dict[str, str] = {
    "A1": "序列开头第一、二根就构成包含关系时，65 课的方向判据缺「前一根非包含 K 线」这个前提 → 用后一根兜底，标记 bootstrap=True",
    "A2": "78 课「未确认尾段」的端点取法：原文只说未确认，未给端点算法 → 取该方向最后一笔的终点",
    "A3": "67 课线段「特征序列」只有定义没有伪代码 → 用「包含合并 + 分型」的逐元素增量判据（见 ④ 节说明）",
    "A4": "67 课第二种情况要求「第二特征序列出分型」才确认 → 按字面实现，代价是反向至少 6 笔才确认（滞后）",
    "A5": "71 课禁止在转折点两侧做包含处理 → 特征序列合并只在同侧进行，跨转折点的元素不合并",
    "A6": "18 课的「次级别走势类型」用线段代理（原文未给线段升格为走势类型的算法）",
    "A7": "中枢级别用「含段数 n_seg」代理（原文未给中枢级别的判定算法）",
    "A8": "20 课 G/D（中枢的 GG/DD 之外那对）未实现：需先切出 Z 走势段，原文只有定性描述",
    "A9": "走势类型「完成」的判据：原文只说「终要完成」→ 用「出现反方向同级别中枢」工程化判定",
    "A10": "背驰的「力度」用 MACD 面积比较（24/27 课口径），阈值 ratio 是工程参数，原文只说「面积变小」",
    "A11": "37 课要求背驰比较的两段「同级」，本实现的级别就是结构层数，无法再细分 → 开关式处理",
    "A12": "区间套递归到哪一级停、什么算「确认」，原文无判定式 → 用「次级别出现同向背驰/买卖点」作确认",
    "A13": "级别递归用「把 L 级线段当 L+1 级的 K 线」的工程做法，与 84 课「级别与时间无关」有张力",
    "A14": "笔端点是否必须是区间极值（chan.py 的 end_is_peak）：77 课没写 → 默认关，可开",
    "A15": "笔的「至少几根 K 线」原文有两版：62 课「至少 1 根独立 K 线」/ 77 课「至少 3 根原始 K 线」→ 开关",
    "A16": "二买/二卖的「次级别回调」用一根笔代理（原文要求次级别走势类型；A6 已说明线段是次级别代理，这里为了时效退到笔）",
    "A17": "个别线段的终点不是该段绝对极值（特征序列合并口径的副作用，参考实现同样存在）→ 只做诊断计量，不进硬不变量",
    "A18": "24 课「B 中枢级别更大」无法严格判定（本实现没有级别量）→ 用「中枢含段数 ≥5（延伸过）」作代理；旧写法 `>=3` 是恒真式，已废弃",
    "A19": "中枢扩张（相邻中枢区间重叠 → 升级为更大级别中枢）未实现：重叠中枢各自输出，一段盘整会被拆成两个盘整",
}


# ==========================================================================
# 第 0 节  数据结构：每层结构只带「坐标 + 价格区间 + 极值所在位置」
# ==========================================================================

@dataclass(frozen=True)
class Bar:
    """原始 K 线。t 为绝对序号（回测里必须用绝对 bar 位置，不能用窗口内下标）。"""
    t: int
    high: float
    low: float
    open: Optional[float] = None
    close: Optional[float] = None


@dataclass(frozen=True)
class MF:
    """合并 K 线（① 的输出）。

    65 课顺序原则：永远拿「已合并的最后一根」去比下一根原始 K 线；
    包含关系不满足传递律，所以不能一次批量合并。
    hi/lo 是合并后的区间；t_hi/t_lo 是极值真正落在哪根原始 K 线上（端点时间戳靠它，不是靠区间边界）。
    """
    hi: float
    lo: float
    t_hi: int
    t_lo: int
    i0: int
    i1: int
    n: int = 1
    d: int = 0              # 合并时用的方向（+1 向上 / -1 向下 / 0 未知）
    bootstrap: bool = False  # 是否用了 A1 的头部兜底


@dataclass(frozen=True)
class FX:
    """分型（② 的输出）。kind=+1 顶分型，-1 底分型。"""
    kind: int
    idx: int          # 中心那根合并 K 线的下标
    hi: float
    lo: float
    t: int            # 极值所在原始 K 线
    i0: int           # 三根合并 K 线覆盖的原始 K 线起点
    i1: int           # 覆盖终点


@dataclass(frozen=True)
class Bi:
    """笔（③ 的输出）。d=+1 向上笔（底→顶），d=-1 向下笔（顶→底）。"""
    i: int            # 起点分型在 kept 列表里的下标
    j: int
    d: int
    start: FX
    end: FX
    hi: float
    lo: float
    t0: int
    t1: int

    @property
    def n_bars(self) -> int:
        return self.t1 - self.t0


@dataclass(frozen=True)
class Seg:
    """线段（④ 的输出）。s_bi / e_bi 是首末笔在笔序列里的下标。"""
    s_bi: int
    e_bi: int
    d: int
    hi: float
    lo: float
    t0: int
    t1: int
    confirmed: bool = True
    start_px: float = 0.0      # 起点价（线段起点那个笔端点）
    end_px: float = 0.0        # 终点价：向上段是段顶、向下段是段底
    confirm_t: int = -1        # 该段「被确认」的时间（原始 bar 序号）；策略必须按它取用，不能按 t1

    @property
    def n_bi(self) -> int:
        return self.e_bi - self.s_bi + 1

    @property
    def end_price(self) -> float:
        """段终点价。_mk_seg 会填好 end_px；手造线段没填时退回「方向极值」，不静默给 0。"""
        if self.end_px:
            return self.end_px
        return self.hi if self.d > 0 else self.lo


@dataclass(frozen=True)
class ZS:
    """中枢（⑤ 的输出）。

    17 课：中枢是「被至少三个连续次级别走势类型所重叠的部分」。
    zd/zg = 中枢区间（ZG = min(各段高点)，ZD = max(各段低点)，18 课字面）；
    dd/gg = 中枢区间之外的实际极值（DD = min(各段低点)，GG = max(各段高点)）。
    """
    a: int            # 构成中枢的第一段在 segs 里的下标
    b: int            # 最后一段
    zd: float
    zg: float
    dd: float
    gg: float
    d: int            # 中枢的「进入方向」：+1 向上进入 / -1 向下进入 / 0 无进入段
    formula: str = "20"
    confirm_t: int = -1  # 中枢被确认的时间 = 构成它的第三段被确认的时间


@dataclass(frozen=True)
class Trend:
    """走势类型（⑥ 的输出）。kind: 上涨趋势 / 下跌趋势 / 盘整。"""
    kind: str
    a: int            # 中枢下标（闭区间）
    b: int
    direction: int    # +1 上 / -1 下 / 0 盘整


@dataclass(frozen=True)
class BC:
    """背驰（⑦ 的输出）。"""
    kind: str         # 趋势背驰 / 盘整背驰
    side: int         # +1 顶背驰（卖向）/ -1 底背驰（买向）
    t: int            # 背驰点所在原始 K 线
    price: float
    area_in: float    # 进入段 / 第一段 MACD 面积
    area_out: float   # 离开段 / 第三段 MACD 面积
    zs_ix: int        # 相关中枢下标


@dataclass(frozen=True)
class Point:
    """买卖点（⑧ 的输出）。kind ∈ {B1,B2,B3,S1,S2,S3}。

    两个时间必须分清（这是策略能不能用的分水岭）：
        t       价格上出现这个点的 bar（事后视角）
        known_t 这个点**能被知道**的 bar —— 构成它的那段结构被确认的时刻。
                实盘最早只能在 known_t 之后一根 bar 开盘成交，不能按 t 成交。
                两者之差就是「结构信号滞后」，本库把它写成数据，不留给策略去猜。
    """
    kind: str
    t: int
    price: float
    zs_ix: int = -1
    note: str = ""
    known_t: int = -1


# ==========================================================================
# 第 1 节  K 线包含处理（62 / 65 课）
# ==========================================================================

def _contained(h1: float, l1: float, h2: float, l2: float) -> bool:
    """两根 K 线是否存在包含关系（62 课）。

    判据：h1<=h2 且 l1>=l2（后者被前者包含），或 h1>=h2 且 l1<=l2（前者被后者包含）。
    等号算包含（区间退化时并不产生新信息）。
    """
    return (h1 <= h2 + EPS and l1 >= l2 - EPS) or (h1 >= h2 - EPS and l1 <= l2 + EPS)


def merge_bars(bars: Sequence[Bar], head_rule: str = "close") -> tuple[list[MF], dict]:
    """① 包含处理。返回 (合并 K 线序列, 诊断信息)。

    65 课：向上时高点取高、低点取高；向下时高点取低、低点取低。
    65 课方向判据：gn >= gn-1 → 向上；dn <= dn-1 → 向下（gn/dn 为相邻两根的高/低点）。
    这里 gn-1 / dn-1 取的是「已合并序列的最后两根」——这是顺序原则的直接后果。

    原文没定义的地方（A1）：序列最开头前两根就包含，此时没有「前一根非包含 K 线」，
    本实现用收盘价方向兜底（head_rule="close"，与既有实现同口径），
    也可选 head_rule="lookahead"（看后一根），两种都在 MF.bootstrap 上打标，不假装这是原文。
    """
    out: list[MF] = []
    d = 0
    boot = False
    n = len(bars)
    for k, b in enumerate(bars):
        if not out:
            out.append(MF(b.high, b.low, b.t, b.t, b.t, b.t, 1, 0, False))
            continue
        p = out[-1]
        if not _contained(p.hi, p.lo, b.high, b.low):
            # 非包含：方向由这两根确定（65 课判据）
            d = 1 if b.high > p.hi + EPS else -1
            out.append(MF(b.high, b.low, b.t, b.t, b.t, b.t, 1, d, False))
            continue

        # 包含关系 + 顺序原则：用已合并的最后一根去比
        if d != 0:
            dd = d
        else:
            boot = True                                   # A1
            if head_rule == "lookahead" and k + 1 < n:
                dd = 1 if bars[k + 1].high > p.hi + EPS else -1
            elif b.close is not None and bars[k - 1].close is not None:
                dd = 1 if b.close >= bars[k - 1].close - EPS else -1
            else:
                dd = 1 if b.high >= p.hi - EPS else -1

        if dd > 0:                                        # 向上：高取高、低取高
            nhi = p.hi if p.hi >= b.high - EPS else b.high
            thi = p.t_hi if p.hi >= b.high - EPS else b.t
            nlo = p.lo if p.lo >= b.low - EPS else b.low
            tlo = p.t_lo if p.lo >= b.low - EPS else b.t
        else:                                             # 向下：高取低、低取低
            nhi = p.hi if p.hi <= b.high + EPS else b.high
            thi = p.t_hi if p.hi <= b.high + EPS else b.t
            nlo = p.lo if p.lo <= b.low + EPS else b.low
            tlo = p.t_lo if p.lo <= b.low + EPS else b.t

        out[-1] = MF(nhi, nlo, thi, tlo, p.i0, b.t, p.n + 1, dd, p.bootstrap or boot)

    return out, {"bootstrap": any(m.bootstrap for m in out), "n_mf": len(out), "n_bar": n}


# ==========================================================================
# 第 2 节  分型（62 课定义 + 77 课结合律）
# ==========================================================================

def find_fractals(mfs: Sequence[MF]) -> list[FX]:
    """② 分型。

    62 课：顶分型 = 第二根 K 线的高点是相邻三根中最高的，**且低点也是最高的**；底分型反之。
    合并后相邻 K 线必无包含关系，因此高低点同序 —— 两个条件其实等价，
    但代码里两个都写，是为了「判据与原文一一对应」，也防住极端数据（等号退化）。

    77 课结合律：任何相邻分型之间不能有 K 线分属两个分型 → 分型中心至少间隔 2 根合并 K 线。
    """
    fxs: list[FX] = []
    last_idx = -10
    for i in range(1, len(mfs) - 1):
        a, m, c = mfs[i - 1], mfs[i], mfs[i + 1]
        top = (m.hi > a.hi + EPS and m.hi > c.hi + EPS
               and m.lo > a.lo + EPS and m.lo > c.lo + EPS)
        bot = (m.lo < a.lo - EPS and m.lo < c.lo - EPS
               and m.hi < a.hi - EPS and m.hi < c.hi - EPS)
        if not (top or bot):
            continue
        if i - last_idx < 2:                              # 77 课结合律
            continue
        if top:
            fxs.append(FX(+1, i, m.hi, m.lo, m.t_hi, a.i0, c.i1))
        else:
            fxs.append(FX(-1, i, m.hi, m.lo, m.t_lo, a.i0, c.i1))
        last_idx = i
    return fxs


# ==========================================================================
# 第 3 节  笔（62 / 65 / 77 课，77 课三步法 + 单遍栈）
# ==========================================================================

def _more_extreme(a: FX, b: FX) -> bool:
    return (b.hi > a.hi + EPS) if b.kind == 1 else (b.lo < a.lo - EPS)


def _pair_ok(a: FX, b: FX, min_gap: int = 4, rule: str = "62") -> bool:
    """一对异性质分型能否构成一笔。

    [原文] 77 课三条件：
        ① 一顶一底；
        ② 顶分型与底分型不共用 K 线（三根合并 K 线的覆盖区间不相交）；
        ③ 顶分型最高那根 K 线的区间至少有一部分高于底分型最低那根 K 线的区间
           → 形式化：顶的 hi > 底的 lo（向下笔则对称，恒等式）。
    [开关] 「之间至少几根 K 线」原文两版（A15）：
        62 课版：顶底之间至少 1 根不属于两个分型的合并 K 线 → 分型中心间隔 ≥ 4；
        77 课版：顶分型最高 K 线与底分型最低 K 线之间（不含两端）至少 3 根原始 K 线。
    """
    if a.kind == b.kind:
        return False
    top, bot = (a, b) if a.kind == 1 else (b, a)
    if top.hi <= bot.lo + EPS:
        return False
    if rule == "77":
        if not (top.i1 < bot.i0 or bot.i1 < top.i0):      # ② 不共用 K 线
            return False
        if abs(top.t - bot.t) - 1 < 3:                    # ③ 至少 3 根原始 K 线
            return False
    else:
        if abs(b.idx - a.idx) < min_gap:
            return False
    return True


def _end_is_extreme(mfs: Sequence[MF], a: FX, b: FX) -> bool:
    """[工程 A14] 笔端点必须是区间极值（chan.py 的 end_is_peak），77 课没写，默认关。"""
    if a.kind == -1:                                      # 向上笔，终点是顶
        rng = range(a.idx + 1, b.idx)
        return all(mfs[k].hi <= b.hi + EPS for k in rng)
    rng = range(b.idx + 1, a.idx)
    return all(mfs[k].lo >= b.lo - EPS for k in rng)


def prepare_fractals(fxs: Sequence[FX], min_gap: int = 4, rule: str = "62",
                     mfs: Optional[Sequence[MF]] = None,
                     require_end_extreme: bool = False) -> list[FX]:
    """77 课三步法（第 2 步去重 + 第 3 步成笔），单遍栈 O(n)，无迭代上限。

    不变式（返回时必然成立）：
        ① 相邻分型性质必然不同；
        ② 相邻分型必然可成笔（否则已被回退删掉）。
    历史教训：用「迭代到稳定」逼近（for _ in range(200)）会在真实数据上触顶，
    一旦触顶又静默 continue，笔就会在某一天之后彻底断流 —— 必须单遍栈 + 契约抛错。
    """
    kept: list[FX] = []
    for f in fxs:
        kept.append(f)
        while len(kept) >= 2:
            a, b = kept[-2], kept[-1]
            if a.kind == b.kind:                          # 三步法第 2 步：同性质去重
                if _more_extreme(a, b):
                    kept.pop(-2)                          # 后一个更极端 → 前者作废
                    continue
                kept.pop()                                # 否则后者作废（相等时保留先出现的）
                break
            if _pair_ok(a, b, min_gap, rule) and (
                    not require_end_extreme or mfs is None or _end_is_extreme(mfs, a, b)):
                break
            kept.pop()                                    # 65 课：转折级别太小 → 删掉后者再回退
    return kept


def build_bis(mfs: Sequence[MF], fxs: Optional[Sequence[FX]] = None,
              min_gap: int = 4, rule: str = "62",
              require_end_extreme: bool = False) -> list[Bi]:
    """③ 笔。契约：返回时相邻笔必然首尾相接且方向交替，否则抛错（不静默丢段）。"""
    fxs = list(fxs if fxs is not None else find_fractals(mfs))
    kept = prepare_fractals(fxs, min_gap, rule, mfs, require_end_extreme)

    bis: list[Bi] = []
    for k in range(len(kept) - 1):
        a, b = kept[k], kept[k + 1]
        if a.kind == b.kind or not _pair_ok(a, b, min_gap, rule):
            raise RuntimeError(f"分型预处理破坏契约：第 {k} 对不可成笔 bar={a.idx},{b.idx}")
        up = a.kind == -1
        bis.append(Bi(
            i=k, j=k + 1, d=1 if up else -1, start=a, end=b,
            hi=max(a.hi, b.hi), lo=min(a.lo, b.lo),
            t0=a.t, t1=b.t,
        ))
    return bis


def tail_gap(mfs: Sequence[MF], bis: Sequence[Bi]) -> int:
    """尾段覆盖检查：最后一笔终点距数据末尾多少根合并 K 线。

    正常应是几十根以内（未确认尾段）。几百根以上就是「笔断流」，结构分析全部失效。
    """
    if not bis:
        return len(mfs)
    return len(mfs) - 1 - bis[-1].end.idx


# ==========================================================================
# 第 4 节  线段（62 / 65 / 67 / 71 / 78 课：特征序列 + 两种情况）
# ==========================================================================
#
# 这一节是全篇最容易写错的地方，先把判据摆清楚：
#
# 67 课：线段的破坏由「特征序列的分型」判定。向上线段的特征序列 = 段内所有向下笔；
#        向下线段的特征序列 = 段内所有向上笔（特征序列的每一个元素就是一笔的区间）。
# 71 课：判别方法是「假设某转折点」——假设第 e 笔是段最后一笔，然后用两种情况考察它：
#        转折点前的最后一个特征元素叫 A，转折点后的第一笔叫 B，
#        **A 与 B 之间不做包含处理**（原文明确豁免，这是本节的第①号红线）；
#        B 及其右侧元素可以做包含处理，但要求分型中间元素仍然落在 B 这一笔上，
#        且比右侧下一个元素更极端 —— 否则说明线段还在延伸，这个转折点不成立。
# 67 课：A、B 之间有缺口（无重叠区间）= 第二种情况，需要「第二特征序列」出分型才确认；
#        没有缺口 = 第一种情况，直接确认。
# 78 课：线段两端一顶一底，顶必须高于底（方向上必须真的创出极值）。
#
# 历史 bug（我自己第一版就犯了）：把转折点后的 B 并进左侧特征序列一起做包含合并，
# 于是「B 包含左侧元素」时按左侧方向合并，把极值挪到了后面一笔，转折点被整体错过，
# 线段数与既有实现差 6 段（51 vs 45）。修法是照 71 课把 A、B 分开判。

@dataclass
class _El:
    """特征序列的一个元素（一笔的区间），带「极值落在哪一笔」的溯源。"""
    hi: float
    lo: float
    hi_bi: int
    lo_bi: int
    d: int = 0


def _elem(bis: Sequence[Bi], i: int) -> _El:
    return _El(bis[i].hi, bis[i].lo, i, i)


def _ext(el: _El, d: int) -> float:
    """元素在「线段方向」上的极值，统一取成「越大越极端」，向上/向下段共用一套比较。

    向上段看高点；向下段看低点，取负之后同样「越大越极端」。
    （第一版这里直接返回 lo，比较方向就反了 —— 线段永远不肯结束，8 段 vs 45 段。）
    """
    return el.hi if d > 0 else -el.lo


def _ext_bi(el: _El, d: int) -> int:
    return el.hi_bi if d > 0 else el.lo_bi


def _merge_elems(items: Sequence[_El], seed_up: Optional[bool] = None) -> list[_El]:
    """特征序列的包含处理（67 课：与 K 线包含处理同一套规则，顺序原则）。

    seed_up：第一个元素要合并时没有「前两个元素」可判方向，用这个种子（71 课的 B 用它）。
    """
    out: list[_El] = []
    for it in items:
        if not out:
            out.append(_El(it.hi, it.lo, it.hi_bi, it.lo_bi, 0))
            continue
        p = out[-1]
        if _contained(p.hi, p.lo, it.hi, it.lo):
            # 65 课：方向 = 「已合并的最后一根」与「再前一根」比高点（合并后高点会变，必须重算，
            # 不能用元素创建时记下的旧方向 —— 那是一个只在真实数据上才暴露的偏差）
            if len(out) >= 2:
                dd = 1 if p.hi > out[-2].hi + EPS else -1
            elif seed_up is not None:
                dd = 1 if seed_up else -1
            else:
                dd = 1 if it.hi > p.hi + EPS else -1
            old_hi, old_lo = p.hi, p.lo
            if dd > 0:                                    # 向上：高取高、低取高
                p.hi = max(p.hi, it.hi)
                p.lo = max(p.lo, it.lo)
            else:                                         # 向下：高取低、低取低
                p.hi = min(p.hi, it.hi)
                p.lo = min(p.lo, it.lo)
            # 极值归属按「真实极值」登记（高点认最高那一笔、低点认最低那一笔），不随方向规则挪动：
            # 向上合并会把低点抬高，若顺势把 lo_bi 也挪过去，向下线段的真正底部就丢了，
            # 线段端点会落到错的一笔上（这一条是跟既有实现对账时暴露出来的）。
            if old_hi < it.hi - EPS:
                p.hi_bi = it.hi_bi
            if it.lo < old_lo - EPS:
                p.lo_bi = it.lo_bi
            continue
        out.append(_El(it.hi, it.lo, it.hi_bi, it.lo_bi, 1 if it.hi > p.hi + EPS else -1))
    return out


def _three_overlap(bis: Sequence[Bi], s: int) -> bool:
    """65 课：线段的前三笔必须有重叠。区间取三笔 [低, 高] 的交集。"""
    if s + 2 >= len(bis):
        return False
    lo = max(b.lo for b in bis[s:s + 3])
    hi = min(b.hi for b in bis[s:s + 3])
    return lo < hi - EPS


def _second_feature_ok(bis: Sequence[Bi], e: int, d: int, mode: str = "original") -> bool:
    """67 课第二种情况：出现缺口时，必须「第二特征序列」也出分型才确认。

    原文在这里自己就没统一（三套说法，见 _素材/原文摘录-形态学.md 形态-62~66）：
      mode="original"（默认，78 课口径）：沿用**本段**的特征序列（方向 -d 的笔），
          从 e+2 起做包含合并，找与段方向相反的分型（d>0 找底分型，d<0 找顶分型）。
          78 课明确「第二种情况」允许把转折点两侧一起做包含处理。
      mode="reverse"（67 课口径）：改用**反向段**自己的特征序列（方向 d 的笔）。
    两套都实现、都要报数量，不做无声取舍 —— 这是原文没有伪代码的地方。
    [工程 A4] 按字面实现 → 反向要走出多笔才确认，滞后明显；可用 second_case=False 整体关掉。
    """
    want = -d if mode == "original" else d
    items = [_elem(bis, i) for i in range(e + 2, len(bis)) if bis[i].d == want]
    merged = _merge_elems(items)
    for k in range(1, len(merged) - 1):
        a, m, c = merged[k - 1], merged[k], merged[k + 1]
        if d > 0:
            if m.lo < a.lo - EPS and m.lo < c.lo - EPS:
                return True
        else:
            if m.hi > a.hi + EPS and m.hi > c.hi + EPS:
                return True
    return False


def _judge_point(bis: Sequence[Bi], s: int, e: int, A: _El,
                 second_case: bool, second_mode: str = "original") -> Optional[tuple[int, str, bool]]:
    """71 课「假设第 e 笔是段最后一笔」，返回 (终点笔, 类型, 是否有缺口, 确认时间)。

    四个必要条件缺一不可：
      ① B（转折点后第一笔）的极值比 A 更极端 —— 否则根本谈不上转折；
      ② B 与右侧元素合并后，极值仍落在 B 这一笔上 —— 否则线段还在延伸（后面创了新极值）；
      ③ 分型成立：B 的极值比右侧下一个元素更极端；
      ④ 78 课：向上段终点必须高于起点（顶要高于底），向下段反之。
    """
    d = bis[s].d
    n = len(bis)
    B = _elem(bis, e + 1)
    if _ext(B, d) <= _ext(A, d) + EPS:                    # ①
        return None
    right = _merge_elems([B] + [_elem(bis, i) for i in range(e + 3, n) if bis[i].d == -d],
                         seed_up=(B.hi > A.hi))
    M1 = right[0]
    if _ext_bi(M1, d) != e + 1:                           # ②
        return None
    if len(right) < 2:                                    # ③ 分型缺第三个元素
        return None
    if _ext(M1, d) <= _ext(right[1], d) + EPS:
        return None
    start_px = bis[s].start.lo if d > 0 else bis[s].start.hi   # ④
    end_px = bis[e].end.hi if d > 0 else bis[e].end.lo
    if (end_px - start_px) * d <= 0:
        return None
    gap = (B.hi < A.lo - EPS) or (B.lo > A.hi + EPS)      # 67 课缺口（只比 A、B 两个原始元素）
    # 确认时间：分型的第三个元素走完，这个转折点才算数（right[1] 的终点时间）
    confirm_t = bis[right[1].hi_bi if d > 0 else right[1].lo_bi].t1
    if not gap:
        return e, "第一种情况", False, confirm_t
    if not second_case:
        return e, "第二种情况(开关关闭)", True, confirm_t
    if _second_feature_ok(bis, e, d, second_mode):
        return e, "第二种情况", True, confirm_t
    return None


def build_segs(bis: Sequence[Bi], second_case: bool = True,
               require_first3_overlap: bool = True,
               second_mode: str = "original") -> list[Seg]:
    """④ 线段：候选终点从 s+2 起逐笔右移（78 课：线段奇数笔、起止同向），第一个通过考察的就是分界点。

    [工程 A2] 始终没被破坏 → 按 78 课「未确认线段」处理，端点取该方向上的极值笔，confirmed=False。
    [工程 A5] 71 课禁止在转折点两侧做包含处理 → A、B 分开，只在 B 及右侧做合并。
    """
    segs: list[Seg] = []
    n = len(bis)
    s = 0
    while s + 2 < n:
        d = bis[s].d
        if require_first3_overlap and not _three_overlap(bis, s):
            s += 1                                    # 这个起点不成段，往后挪一笔继续找（不许 break）
            continue
        left = _merge_elems([_elem(bis, s + 1)])
        e = s + 2
        found: Optional[tuple[int, str, bool, int]] = None
        while e + 1 < n:
            if left:
                r = _judge_point(bis, s, e, left[-1], second_case, second_mode)
                if r is not None:
                    found = r
                    break
            left = _merge_elems(list(left) + [_elem(bis, e + 1)])
            e += 2
        if found is None:                             # 未确认尾段
            cand = [i for i in range(s, n) if bis[i].d == d]
            best = max(cand, key=lambda i: bis[i].end.hi if d > 0 else -bis[i].end.lo)
            if best - s >= 2:
                segs.append(_mk_seg(bis, s, best, d, False))
            break
        segs.append(_mk_seg(bis, s, found[0], d, True, confirm_t=found[3]))
        s = found[0] + 1
    return segs


def _mk_seg(bis: Sequence[Bi], s: int, e: int, d: int, confirmed: bool,
            confirm_t: int = -1) -> Seg:
    part = bis[s:e + 1]
    return Seg(
        s_bi=s, e_bi=e, d=d,
        hi=max(b.hi for b in part), lo=min(b.lo for b in part),
        t0=bis[s].t0, t1=bis[e].t1, confirmed=confirmed,
        start_px=(bis[s].start.lo if d > 0 else bis[s].start.hi),
        end_px=(bis[e].end.hi if d > 0 else bis[e].end.lo),
        confirm_t=(confirm_t if confirm_t >= 0 else bis[e].t1),
    )


# ==========================================================================
# 第 5 节  中枢（17 / 18 / 20 / 21 / 33 课）
# ==========================================================================

def _overlap(a: ZS | tuple[float, float], zd: float, zg: float) -> float:
    """一个区间与中枢区间的重叠长度（<=0 表示不重叠）。"""
    lo, hi = (a.lo, a.hi) if isinstance(a, Seg) else (a[0], a[1])
    return min(hi, zg) - max(lo, zd)


def build_zhongshus(segs: Sequence[Seg], formula: str = "20",
                    max_seg: Optional[int] = 8,
                    use_unconfirmed: bool = False,
                    extend_rule: str = "endpoint",
                    seg_range: str = "extreme") -> list[ZS]:
    """⑤ 中枢：连续三段的重叠部分。

    17 课定义：被至少三个连续次级别走势类型所重叠的部分。
    [开关 1] 区间公式（原文两套）：
        18 课字面：ZG = min(三段全部高点)，ZD = max(三段全部低点)；
        20 课：只取同向段（第 1、3 段）的高低点，避免把反向段的极端值算进来。
        线段连续时两式恒等，差异只出现在有缺口时。
    [开关 2] 延伸上限（原文两套）：20 课「无上限」vs 33 课「5 段」→ 默认 max_seg=8（3 本体 + 5 延伸）。
    实现纪律（这一条被人工检查抓出过：旧 docstring 写反了）：
        · 中枢因「离开且不回来」而结束时，离开段是 `a+A+b+B+c` 里的 b/c 连接段，
          不能当下一中枢的第一段 → `i = j + 1`；
        · 因触及 max_seg 上限而中断（升级）时，当前段仍在震荡里 → `i = j`。
        另一种写法 `i = j`（永远不跳过离开段）会把连接段吞进下一中枢，二买/三买会系统性偏移。
    """
    zss: list[ZS] = []
    n = len(segs)
    # 未确认段只可能是尾部那一段（78 课）；如果不是，说明上游调用方式有问题，
    # 静默按前缀截断会算出一堆假中枢 —— 这里直接抛错，不猜。
    n_conf = sum(1 for s in segs if s.confirmed)
    if n_conf != n and any(s.confirmed for s in segs[n_conf:]):
        raise ValueError("线段序列里未确认段不在尾部，无法按前缀截断")
    usable = n if use_unconfirmed else n_conf
    i = 0
    while i <= usable - 3:
        a, b, c = segs[i], segs[i + 1], segs[i + 2]
        # 段的区间有两种取法（都合法，但结果不同，必须显式选）：
        #   extreme  = 段内所有笔的最高/最低点（本实现默认，忠于「走势类型的区间」）
        #   endpoint = 只用段两端点的价格（既有实现的口径；只有当「终点恰是段内极值」时两者才相同）
        if seg_range == "endpoint":
            def rng(sg):
                return (min(sg.start_px, sg.end_px), max(sg.start_px, sg.end_px))
            ra, rb, rc = rng(a), rng(b), rng(c)
        else:
            ra, rb, rc = (a.lo, a.hi), (b.lo, b.hi), (c.lo, c.hi)
        if formula == "20" and a.d == c.d:
            zg = min(ra[1], rc[1])
            zd = max(ra[0], rc[0])
        else:
            zg = min(ra[1], rb[1], rc[1])
            zd = max(ra[0], rb[0], rc[0])
        if zg <= zd + EPS:                    # 无重叠，不成中枢
            i += 1
            continue
        dd = min(ra[0], rb[0], rc[0])
        gg = max(ra[1], rb[1], rc[1])
        j = i + 3
        left = False
        while j < usable:
            if max_seg is not None and (j - i) >= max_seg:
                left = False          # 触顶升级，不是离开段
                break
            s_j = segs[j]
            lo_j, hi_j = ((min(s_j.start_px, s_j.end_px), max(s_j.start_px, s_j.end_px))
                          if seg_range == "endpoint" else (s_j.lo, s_j.hi))
            if extend_rule == "endpoint":
                # 20 课：离开中枢后不回到中枢区间，中枢才结束；「收在区间内」用的是段的终点价
                inside = zd - EPS <= s_j.end_price <= zg + EPS
                if inside:
                    dd = min(dd, lo_j)
                    gg = max(gg, hi_j)
                    j += 1
                    continue
                # 离开但下一段又回到中枢 → 仍算延伸（20 课「回拉」）
                if j + 1 < usable:
                    nxt = segs[j + 1]
                    if zd - EPS <= nxt.end_price <= zg + EPS:
                        dd = min(dd, lo_j)
                        gg = max(gg, hi_j)
                        j += 1
                        continue
                left = True
                break
            if _overlap(s_j, zd, zg) > EPS:      # 另一套口径：整段区间与中枢重叠即延伸
                dd = min(dd, s_j.lo)
                gg = max(gg, s_j.hi)
                j += 1
                continue
            left = True
            break
        d = segs[i - 1].d if i > 0 else 0
        conf_t = max(segs[k].confirm_t for k in (i, i + 1, i + 2))
        zss.append(ZS(a=i, b=j - 1, zd=zd, zg=zg, dd=dd, gg=gg, d=d, formula=formula,
                      confirm_t=conf_t))
        # 中枢结束后的推进：离开段（left=True）是连接段 b/c，不能当下一中枢的第一段 →
        # i = j+1；因触顶升级中断的，当前段还在震荡里 → i = j。
        i = (j + 1) if left else j
    return zss


# ==========================================================================
# 第 6 节  走势类型与走势必完美（17 / 18 / 20 / 45 / 102 课）
# ==========================================================================

def diagnose_zs_extensions(zss: Sequence[ZS]) -> list[tuple[int, float]]:
    """诊断（未实现的能力）：相邻中枢区间重叠 = 20 课说的「中枢扩张」，应升级成更大级别中枢。

    本实现**没有做**扩张合并：重叠的相邻中枢仍然各自输出，于是「一段盘整」会被拆成两个盘整。
    这在走势类型层是有代价的（趋势判定不受影响，因为趋势要求中枢不重叠），
    所以只登记为已知缺口（A19），并在验收里报出条数，不假装做到了。
    """
    out = []
    for k in range(len(zss) - 1):
        ov = min(zss[k].zg, zss[k + 1].zg) - max(zss[k].zd, zss[k + 1].zd)
        if ov > EPS:
            out.append((k, ov))
    return out


def build_trends(zss: Sequence[ZS], overlap_rule: str = "ZG_ZD") -> list[Trend]:
    """⑥ 走势类型。

    17/18 课：走势类型 = 趋势（至少两个同向中枢，中枢之间不重叠）+ 连接段；盘整 = 只有一个中枢。
    [开关] 中枢重叠用哪组边界（原文两套）：
        ZG_ZD：18 课字面（中枢区间之间不重叠）；
        GG_DD：最严（连瞬间波动都不重叠）—— 该口径下实测走势类型恒为盘整，一类买点不可达。
    [工程 A9] 「走势必完美」原文只说终要完成，没给判据 → 以「出现反方向同向级别中枢」判定结束。
    [缺口 A19] 中枢扩张未合并：两个重叠的同级中枢本应升级为一个更大级别中枢，
              这里会输出两段独立「盘整」。趋势判定不受影响（趋势要求中枢不重叠）。
    """
    def ov(x: ZS, y: ZS) -> float:
        if overlap_rule == "GG_DD":
            return min(x.gg, y.gg) - max(x.dd, y.dd)
        return min(x.zg, y.zg) - max(x.zd, y.zd)

    out: list[Trend] = []
    i = 0
    while i < len(zss):
        if i + 1 < len(zss) and ov(zss[i], zss[i + 1]) <= EPS:
            d = 1 if zss[i + 1].zd > zss[i].zg + EPS else -1
            j = i + 1
            while j + 1 < len(zss) and ov(zss[j], zss[j + 1]) <= EPS:
                nd = 1 if zss[j + 1].zd > zss[j].zg + EPS else -1
                if nd != d:
                    break
                j += 1
            out.append(Trend(kind="上涨趋势" if d > 0 else "下跌趋势", a=i, b=j, direction=d))
            i = j + 1
        else:
            out.append(Trend(kind="盘整", a=i, b=i, direction=0))
            i += 1
    return out


# ==========================================================================
# 第 7 节  MACD 与背驰（24 / 25 / 27 / 29 / 37 / 43 课）
# ==========================================================================

def ema(xs: Sequence[float], n: int) -> list[float]:
    k = 2.0 / (n + 1.0)
    out: list[float] = []
    prev = None
    for x in xs:
        prev = x if prev is None else prev + k * (x - prev)
        out.append(prev)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, sig: int = 9):
    """标准 MACD。返回 (DIF, DEA, HIST)，HIST = 2*(DIF-DEA)（国内口径）。

    24 课背驰用的是「黄白线（DIF/DEA）与柱子面积」，所以面积口径必须写清楚：
        面积 = Σ|HIST| / 2 = Σ|DIF - DEA|，即黄白线围成的面积，不含那个 2 倍系数。
    """
    ef, es = ema(closes, fast), ema(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, sig)
    hist = [2.0 * (a - b) for a, b in zip(dif, dea)]
    return dif, dea, hist


def _area(hist: Sequence[float], t0: int, t1: int) -> float:
    if t1 < t0:
        t0, t1 = t1, t0
    t0 = max(0, t0)
    t1 = min(len(hist) - 1, t1)
    return sum(abs(hist[k]) for k in range(t0, t1 + 1)) / 2.0


def judge_beichi(segs: Sequence[Seg], zss: Sequence[ZS], hist: Sequence[float],
                 ratio: float = 1.0, level_rule: str = "37") -> list[BC]:
    """⑦ 背驰。

    24 课：趋势背驰 = 进入中枢的那一段（A）与离开中枢的那一段（C）比较，中枢是 B；
           要求 B 中枢的级别大于 A、C 的级别；面积要变小、黄白线要回拉。
    27/29 课：盘整背驰 = 中枢内部第一段与第三段比较。
    方向口径：side = 进入段方向（向下段背驰 → 底背驰 side=-1 → 一买；向上段 → 顶背驰 → 一卖）。
    [开关] 24 课「B 更大」vs 37 课「A、B 同级」→ level_rule 参数，两套都实现。
    [工程 A10] 「面积变小」写成 area_out < area_in * ratio，ratio 默认 1.0（严格小于），
              原文没有阈值，ratio 是留给敏感性分析的参数，不是「原文如此」。
    """
    out: list[BC] = []
    for zi, z in enumerate(zss):
        # —— 趋势背驰：进入段 A = segs[z.a-1]，离开段 C = segs[z.b+1]
        if z.a - 1 >= 0 and z.b + 1 < len(segs):
            a_seg, c_seg = segs[z.a - 1], segs[z.b + 1]
            if a_seg.d == c_seg.d and c_seg.d != 0:
                area_a = _area(hist, a_seg.t0, a_seg.t1)
                area_c = _area(hist, c_seg.t0, c_seg.t1)
                # 24 课：要求「B 中枢的级别大于 A、C 的级别」。
                # 人工检查发现：旧写法 `z.b - z.a + 1 >= 3` 是**恒真式**（中枢定义就 ≥3 段），
                # 等于一个永远成立的假开关 —— 两个 level_rule 跑出来的背驰完全一样。
                # 现在改成有实际含义的代理：中枢发生过延伸（含段数 ≥5，对应 33 课「5 段后升级」）
                # 才认为它「可能比段更大级别」。这是工程代理，不是原文的级别定义（A7/A18）。
                lv_ok = True
                if level_rule == "24":
                    lv_ok = (z.b - z.a + 1) >= 5
                # 创新低/新高：离开段必须越过进入段同向极值（否则不叫「离开中枢的背驰段」）
                if a_seg.d < 0:
                    ext_ok = c_seg.lo < a_seg.lo - EPS
                else:
                    ext_ok = c_seg.hi > a_seg.hi + EPS
                if lv_ok and ext_ok and area_c < area_a * ratio:
                    out.append(BC("趋势背驰", a_seg.d, c_seg.t1,
                                  c_seg.lo if a_seg.d < 0 else c_seg.hi, area_a, area_c, zi))
        # —— 盘整背驰：中枢内部第一段 vs 最后一段（同向）
        if z.b - z.a + 1 >= 3:
            first, last = segs[z.a], segs[z.b]
            if first.d == last.d and first.d != 0:
                area_a = _area(hist, first.t0, first.t1)
                area_c = _area(hist, last.t0, last.t1)
                if area_c < area_a * ratio:
                    out.append(BC("盘整背驰", first.d, last.t1,
                                  last.lo if first.d < 0 else last.hi, area_a, area_c, zi))
    return out


# ==========================================================================
# 第 8 节  三类买卖点（17 / 20 / 21 / 38 / 53 / 61 课）
# ==========================================================================

def find_points(segs: Sequence[Seg], bis: Sequence[Bi], zss: Sequence[ZS],
                bcs: Sequence[BC]) -> list[Point]:
    """⑧ 三类买卖点。

    一买（20/27 课）：趋势背驰产生的买点，且必须落在最后一个中枢的下方。
    二买（20 课）：一买之后第一次次级别回调的低点，不破一买低点。
    [工程 A16]「次级别回调」这里用一根**笔**代理。A6 已说明线段才是次级别的代理，
              二买为了时效退到笔 —— 这是取舍，不是原文规定，写在这里备查。
    三买（20/38 课）：中枢结束、向上离开后，第一次回抽不回中枢（回抽低点 > ZG）。
    [开关] 三买要不要「必须是第一次」：20 课字面 vs 38 课「第一次回抽不到中枢」→ 取 38 课口径
            （只认中枢结束后的第 1 段离开 + 第 2 段回抽），两套判定都在 note 里写明。
    """
    def _seg_known_t(seg) -> int:
        return seg.confirm_t if getattr(seg, "confirm_t", -1) >= 0 else seg.t1

    # 每个买点的可执行时间：由「构成它的那段结构什么时候被确认」决定
    pts: list[Point] = []
    for bc in bcs:
        if bc.kind != "趋势背驰":
            continue
        z = zss[bc.zs_ix]
        kt = -1
        if bc.zs_ix < len(zss) and z.b + 1 < len(segs):
            kt = _seg_known_t(segs[z.b + 1])              # 离开段（背驰段）确认时刻
        if bc.side < 0:                                   # 底背驰 → 一买，须在中枢下方
            if bc.price < z.zd - EPS:
                pts.append(Point("B1", bc.t, bc.price, bc.zs_ix, "趋势背驰且在中枢下方", kt))
        else:                                             # 顶背驰 → 一卖，须在中枢上方
            if bc.price > z.zg + EPS:
                pts.append(Point("S1", bc.t, bc.price, bc.zs_ix, "趋势背驰且在中枢上方", kt))

    for zi, z in enumerate(zss):          # 必须用 enumerate：zss.index(z) 在结构重复时会取错下标
        # 三买 / 三卖：中枢结束后的离开段 + 回抽段
        if z.b + 2 >= len(segs):
            continue
        out_seg, back_seg = segs[z.b + 1], segs[z.b + 2]
        # 三买/三卖的可执行时间 = 回抽段被确认的时刻（回抽段走完还不能算，要等后续特征序列定形）
        if out_seg.d > 0 and back_seg.d < 0 and back_seg.lo > z.zg + EPS:
            pts.append(Point("B3", back_seg.t1, back_seg.lo, zi,
                             "离开段向上 + 回抽不回中枢（38 课口径）", _seg_known_t(back_seg)))
        if out_seg.d < 0 and back_seg.d > 0 and back_seg.hi < z.zd - EPS:
            pts.append(Point("S3", back_seg.t1, back_seg.hi, zi,
                             "离开段向下 + 反抽不回中枢（38 课口径）", _seg_known_t(back_seg)))

    # 二买 / 二卖：一买（一卖）之后的第一笔回调不破前低（不破前高）
    for p in list(pts):
        if p.kind not in ("B1", "S1"):
            continue
        later = [b for b in bis if b.t0 > p.t]
        if not later:
            continue
        if p.kind == "B1":
            for b in later:
                if b.d < 0 and b.t1 > p.t:                 # 第一次向下笔回调
                    if b.lo > p.price + EPS:               # 不破一买低点
                        # 笔的确认要等下一笔出现（笔端点被后来的极值改写时才定形）
                        kt = bis[b.j + 1].t1 if b.j + 1 < len(bis) else b.t1
                        pts.append(Point("B2", b.t1, b.lo, p.zs_ix, "一买后首笔回调不破前低", kt))
                    break
        else:
            for b in later:
                if b.d > 0 and b.t1 > p.t:
                    if b.hi < p.price - EPS:
                        kt = bis[b.j + 1].t1 if b.j + 1 < len(bis) else b.t1
                        pts.append(Point("S2", b.t1, b.hi, p.zs_ix, "一卖后首笔反弹不破前高", kt))
                    break

    pts.sort(key=lambda x: (x.t, x.kind))
    return pts


# ==========================================================================
# 第 9 节  区间套（27 / 61 / 102 课）与第 10 节 级别递归（17 / 63 / 84 / 102 课）
# ==========================================================================

def nest_confirm(parent: Point, child: "Chan", window: tuple[int, int]) -> tuple[bool, str]:
    """⑨ 区间套确认：父级别出现买卖点后，次级别在同一时间窗内是否出现同向背驰/买卖点。

    [工程 A12] 原文没有「递归到哪一级停」和「确认」的判定式 → 本实现用
              「次级别出现同向背驰或同类买卖点」当确认，并如实返回理由。
    """
    t0, t1 = window
    same = [p for p in child.points if t0 <= p.t <= t1 and p.kind[0] == parent.kind[0]]
    if same:
        return True, f"次级别同向买卖点 {len(same)} 个"
    bc = [b for b in child.beichis if t0 <= b.t <= t1]
    if bc:
        return True, f"次级别同向背驰 {len(bc)} 个"
    return False, "次级别窗口内既无同向买卖点也无同向背驰"


def to_level_bars(segs: Sequence[Seg]) -> list[Bar]:
    """把 L 级线段变成 L+1 级的「K 线」：[lo, hi] 当成一根 K 线的低/高。

    [工程 A13] 这是 84 课「级别与时间无关」的工程折中：原文的级别是结构递归出来的，
    本实现用「段 → 高级别 K 线 → 再跑同样五步」来近似，深度一般只能到 2 层（L2 中枢基本凑不出）。
    """
    return [Bar(t=i, high=s.hi, low=s.lo) for i, s in enumerate(segs)]


# ==========================================================================
# 第 11 节  全链路
# ==========================================================================

@dataclass
class Chan:
    bars: Sequence[Bar]
    mfs: list[MF] = field(default_factory=list)
    fxs: list[FX] = field(default_factory=list)
    bis: list[Bi] = field(default_factory=list)
    segs: list[Seg] = field(default_factory=list)
    zss: list[ZS] = field(default_factory=list)
    trends: list[Trend] = field(default_factory=list)
    beichis: list[BC] = field(default_factory=list)
    points: list[Point] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def summarize(self) -> str:
        return (f"K线{len(self.bars)} → 合并{len(self.mfs)} → 分型{len(self.fxs)} → 笔{len(self.bis)}"
                f" → 线段{len(self.segs)}(确认{sum(s.confirmed for s in self.segs)})"
                f" → 中枢{len(self.zss)} → 走势{len(self.trends)} → 背驰{len(self.beichis)}"
                f" → 买卖点{len(self.points)}")


def run(bars: Sequence[Bar], *, bi_rule: str = "62", min_gap: int = 4,
        require_end_extreme: bool = False, second_case: bool = True,
        first3_overlap: bool = True, head_rule: str = "close",
        second_mode: str = "original",
        formula: str = "20", max_seg: Optional[int] = 8,
        overlap_rule: str = "ZG_ZD", zs_extend_rule: str = "endpoint",
        zs_seg_range: str = "extreme", bei_ratio: float = 1.0,
        level_rule: str = "37", use_unconfirmed: bool = False) -> Chan:
    """⑪ 全链路：Bar → 结构 → 背驰 → 买卖点。所有开关集中在这里，默认值都写明依据。"""
    bars = list(bars)
    c = Chan(bars=bars)
    c.mfs, c.meta = merge_bars(bars, head_rule=head_rule)
    c.fxs = find_fractals(c.mfs)
    c.bis = build_bis(c.mfs, c.fxs, min_gap=min_gap, rule=bi_rule,
                      require_end_extreme=require_end_extreme)
    c.segs = build_segs(c.bis, second_case=second_case,
                        require_first3_overlap=first3_overlap, second_mode=second_mode)
    c.zss = build_zhongshus(c.segs, formula=formula, max_seg=max_seg,
                            use_unconfirmed=use_unconfirmed, extend_rule=zs_extend_rule,
                            seg_range=zs_seg_range)
    c.trends = build_trends(c.zss, overlap_rule=overlap_rule)
    closes = [(b.close if b.close is not None else (b.high + b.low) / 2.0) for b in bars]
    _, _, hist = macd(closes)
    c.beichis = judge_beichi(c.segs, c.zss, hist, ratio=bei_ratio, level_rule=level_rule)
    c.points = find_points(c.segs, c.bis, c.zss, c.beichis)
    c.meta.update({"bi_rule": bi_rule, "formula": formula, "max_seg": max_seg,
                   "overlap_rule": overlap_rule, "zs_extend_rule": zs_extend_rule,
                   "zs_seg_range": zs_seg_range,
                   "level_rule": level_rule,
                   "second_case": second_case, "first3_overlap": first3_overlap,
                   "second_mode": second_mode,
                   "tail_gap": tail_gap(c.mfs, c.bis)})
    return c


# ==========================================================================
# 第 11.5 节  因果时间轴（策略层唯一被允许的取用方式）
# ==========================================================================

def timeline(c: Chan, use_unconfirmed_seg: bool = False) -> list[dict]:
    """逐 bar 的结构状态，严格因果：第 i 根只能看见「确认时间 <= t_i」的结构。

    为什么必须有这一层：线段/中枢的 `t1`（终点时间）在事后看是**当时就已经过去**的时间，
    但它要等特征序列走出第三个元素才被确认（`confirm_t`）。策略若用 t1 进场，
    等于用了未来信息 —— 旧信号层在这一点上是含糊的，回测数字因此不可信。

    输出每根 bar 一行：
        t, close,
        seg_dir     当前已确认线段的笔方向（+1 上 / -1 下 / 0 还没有）
        seg_age     该段确认后过了多少根
        zd, zg      当前已确认中枢的区间（没有则 None）
        zs_age      该中枢确认后过了多少根
        broken      收盘价是否跌破最近中枢下沿 ZD（多头视角的「结构走坏」）
        last_point  最近一个已出现的买卖点类型（没有则空串）
        point_age   距那个买卖点多少根
        trend       最近走势类型（上涨趋势 / 下跌趋势 / 盘整 / 空）
    """
    out: list[dict] = []
    segs = [s for s in c.segs if s.confirmed or use_unconfirmed_seg]
    seg_i = -1
    zs_i = -1
    pt_i = -1
    trend_kind = ""
    for b in c.bars:
        t = b.t
        while seg_i + 1 < len(segs) and segs[seg_i + 1].confirm_t <= t:
            seg_i += 1
        while zs_i + 1 < len(c.zss) and c.zss[zs_i + 1].confirm_t <= t:
            zs_i += 1
        while pt_i + 1 < len(c.points) and (c.points[pt_i + 1].known_t
                                            if c.points[pt_i + 1].known_t >= 0
                                            else c.points[pt_i + 1].t) <= t:
            pt_i += 1
        seg = segs[seg_i] if seg_i >= 0 else None
        zs = c.zss[zs_i] if zs_i >= 0 else None
        pt = c.points[pt_i] if pt_i >= 0 else None
        # 走势类型：用「中枢确认时间」把走势类型挂到时间轴上
        trend_kind = ""
        for tr in c.trends:
            if tr.b < len(c.zss) and c.zss[tr.b].confirm_t <= t:
                trend_kind = tr.kind
        out.append({
            "t": t,
            "close": (b.close if b.close is not None else (b.high + b.low) / 2.0),
            "seg_dir": seg.d if seg else 0,
            "seg_age": (t - seg.confirm_t) if seg else -1,
            "zd": zs.zd if zs else None,
            "zg": zs.zg if zs else None,
            "zs_age": (t - zs.confirm_t) if zs else -1,
            "broken": bool(zs is not None and b.low < zs.zd - EPS),
            "last_point": pt.kind if pt else "",
            # 用「知道它的时间」算年龄，不是用价格出现的时间
            "point_age": (t - (pt.known_t if pt.known_t >= 0 else pt.t)) if pt else -1,
            "trend": trend_kind,
        })
    return out


# ==========================================================================
# 第 12 节  不变量检查（「逻辑对」的可执行验收）
# ==========================================================================

def diagnose_seg_extremes(c: Chan) -> list[int]:
    """诊断（不是违规）：哪些线段的终点不是该段的绝对极值。

    严格意义的线段，向上段的终点就是段最高点、向下段的终点就是段最低点；
    但特征序列的合并口径（65 课方向规则会抬高/压低合并后的另一侧极值）会让个别段的终点
    落在段内次极值上。参考实现同样存在这个现象（600036 12 千根：它 1 处 / 本实现 6 处）。
    因此这条不进硬不变量，只作为口径差异的**计量指标**输出 —— 见文档近似表 A17。
    """
    out = []
    for k, sg in enumerate(c.segs):
        if sg.d > 0 and sg.hi - sg.end_px > EPS:
            out.append(k)
        elif sg.d < 0 and sg.end_px - sg.lo > EPS:
            out.append(k)
    return out


def check_invariants(c: Chan) -> list[str]:
    """结构不变量。返回违规描述；空列表 = 全过。这些是「逻辑对不对」的硬证据。"""
    errs: list[str] = []

    # ① 合并 K 线：相邻两根必须无包含关系（否则包含处理漏了）
    for k in range(1, len(c.mfs)):
        a, b = c.mfs[k - 1], c.mfs[k]
        if _contained(a.hi, a.lo, b.hi, b.lo):
            errs.append(f"合并K线 {k-1}/{k} 仍存在包含关系")

    # ② 分型：相邻分型不得共用 K 线
    for k in range(1, len(c.fxs)):
        if c.fxs[k].idx - c.fxs[k - 1].idx < 2:
            errs.append(f"分型 {k-1}/{k} 共用K线（违反 77 课结合律）")

    # ③ 笔：方向交替、端点衔接、价格不倒挂
    for k, b in enumerate(c.bis):
        if b.d > 0 and b.end.hi <= b.start.lo + EPS:
            errs.append(f"第{k}笔方向与价格矛盾")
        if b.d < 0 and b.end.lo >= b.start.hi - EPS:
            errs.append(f"第{k}笔方向与价格矛盾")
        if k:
            p = c.bis[k - 1]
            if p.d == b.d:
                errs.append(f"第{k}笔与上一笔同向")
            if p.end is not b.start:
                errs.append(f"第{k}笔起点未接上一笔终点")
        if b.t1 <= b.t0:
            errs.append(f"第{k}笔时间倒挂")

    # ④ 线段：笔数 >=3、方向交替、首尾相接、端点落在笔端点上
    for k, s in enumerate(c.segs):
        if s.n_bi < 3:
            errs.append(f"第{k}线段不足3笔")
        if s.e_bi < s.s_bi:
            errs.append(f"第{k}线段下标倒挂")
        if k:
            p = c.segs[k - 1]
            if p.d == s.d:
                errs.append(f"第{k}线段与上一段同向")
            if p.e_bi != s.s_bi - 1:
                errs.append(f"第{k}线段与上一段不衔接")
        if s.d > 0 and c.bis[s.e_bi].d < 0:
            errs.append(f"第{k}向上线段以向下笔收尾")

    # ⑤ 中枢：zg > zd、三段以上、下标不越界
    for k, z in enumerate(c.zss):
        if z.zg <= z.zd + EPS:
            errs.append(f"第{k}中枢区间倒挂 zg<=zd")
        if z.b - z.a + 1 < 3:
            errs.append(f"第{k}中枢不足3段")
        if z.b >= len(c.segs):
            errs.append(f"第{k}中枢越界")

    # ⑥ 买卖点：价格必须为正，方向必须与类型一致
    for k, p in enumerate(c.points):
        if p.price <= 0:
            errs.append(f"第{k}个买卖点价格非正 {p.kind}")
        if p.kind.startswith("B") and p.kind not in ("B1", "B2", "B3"):
            errs.append(f"第{k}个买卖点类型未知 {p.kind}")

    # ⑦ 中枢之间不得交叉（后一中枢必须在上一中枢结束之后）
    for k in range(1, len(c.zss)):
        if c.zss[k].a <= c.zss[k - 1].b:
            errs.append(f"第{k}中枢与上一中枢交叉（a={c.zss[k].a} <= 上一中枢 b={c.zss[k - 1].b}）")

    # ⑨ 买卖点必须落在数据范围内
    lo_t = c.bars[0].t if c.bars else 0
    hi_t = c.bars[-1].t if c.bars else 0
    for k, p in enumerate(c.points):
        if not (lo_t <= p.t <= hi_t):
            errs.append(f"第{k}个买卖点时间越界 t={p.t}")
        if p.known_t >= 0 and p.known_t < p.t:
            errs.append(f"第{k}个买卖点 known_t 早于 t（可执行时间不可能早于价格时间）")

    # ⑩ 尾段覆盖：不允许「笔断流」
    tg = tail_gap(c.mfs, c.bis)
    if tg > 200:
        errs.append(f"尾段覆盖异常：最后一笔之后还有 {tg} 根合并K线（疑似断流）")
    return errs
