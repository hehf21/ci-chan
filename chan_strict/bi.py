"""
笔（严格实现：第 77 课三步划分法 + 失败回溯）
==============================================
原文依据（逐字见 _规则依据-原文摘录.md 第三节）：

第 62 课：「两个相邻的顶和底之间构成一笔……顶和底之间必须共用一个K线，这就违反结合律了，
          所以这不算一笔……都必须要求顶和底之间都至少有一K线当成一笔的最基本要求。」
第 65 课：「先顶后底，构成向下一笔；先底后顶，构成向上一笔」
          「在两个顶或底中间没有其他的顶和底，这种情况，意味着第一个顶或底后的转折级别太小，
           不足以构成值得考察的对象」→ **转折太小就删掉它，再继续**
第 77 课：「笔，必须是一顶一底，而且顶和底之间至少有一个K线不属于顶分型与底分型。
           还有一个最显然的，就是……顶分型中最高那K线的区间至少要有一部分高于底分型中
           最低那K线的区间」
          三步法：①取全部标准分型 ②同性质相邻的，顶留高的、底留低的，前一个 X 掉
                  ③余下的相邻顶底即为一笔；连续同性质时取「最先一个」配对

## 两个踩过的坑（都只在真实票上暴露）

① **只做「顺序扫描 + 配对失败就留着游标」会死锁**：600036 上分型 194 个，配出的笔只有 8 个。
   原因是配对不成立时游标不动，后面的同性质极端分型永远无法替换它。
   修法：配对失败就删掉「转折级别太小」的那个分型（后者），再重新做同性质去重。

② **靠「最多迭代 200 次」逼近稳定点，在真实数据上必然失效**（2026-10-30 发现）：
   600036 30 分钟 13216 根、分型 2867 个，迭代 200 次触顶时**还剩 248 个「相邻异性质但不成笔」
   的分型对和 9 个相邻同性质对**；`build_bis` 碰到这种对就 `continue`，于是笔在 2024-09-25
   之后彻底断流 —— 后面 2643 根合并 K 线（约 330 个交易日）再没有任何笔/线段/中枢/买卖点。
   20 只测试票里 **12 只断流**，最长丢了 371 个交易日。
   修法：改成**单遍栈**（本模块 `_prepare_fractals`），每一步都维持「kept 严格顶底交替且相邻可成笔」
   这个不变式，复杂度 O(n)，不存在迭代上限；并把「契约被破坏」从静默 continue 改成抛错。

③ 端点时间戳：旧版 `start_raw` 取合并 K 线的 `i0`、`end_raw` 取 `i1`，而端点价取的是合并 K 线的
   high/low —— 极值可能落在组内任意一根，导致「价格是 A 根的极值、时间却是 B 根」。
   现在改用 `kline.merge_bars` 记下的 `hi_raw` / `lo_raw`（极值真正所在的那根原始 K 线）。
"""

from __future__ import annotations

MIN_FRACTAL_GAP = 4      # 两个分型中心至少隔 4 根合并 K 线 = 含两端共 5 根
                         # 依据：77 课「顶和底之间至少有一个K线不属于顶分型与底分型」
                         # 顶占 i-1,i,i+1，底占 j-1,j,j+1，要留出自由 K 线需 j-i>=4


def _pair_ok(a: dict, b: dict, min_gap: int) -> bool:
    """相邻两个（异性质）分型能否构成一笔。"""
    if a["type"] == b["type"]:
        return False
    if (b["bar"] - a["bar"]) < min_gap:
        return False
    top = a if a["type"] == "top" else b
    bot = b if a["type"] == "top" else a
    # 77 课：顶分型最高那 K 线的区间至少有一部分高于底分型最低那 K 线的区间
    return top["high"] > bot["low"]


def _end_is_peak(bars: list[dict], a: dict, b: dict) -> bool:
    """工程增补（原文未明写）：笔的端点必须是该区间的极值。

    向上笔：a(底) 与 b(顶) 之间不得有合并 K 线的高点超过 b 的高点；
    向下笔反之。这条在 chan.py 里叫 `end_is_peak`（`BiList.py:216-235`），
    是"笔的顶必须真是这一段最高"的显式化；没有它，笔数会比行业口径多 40%
    （600036 30 分钟：我们 818 笔 vs chan.py 549 笔，2026-10-30 对账实测）。
    因原文 77 课只给了"顶的区间有一部分高于底的区间"，这条**不算严格实现**，
    默认关闭，打开后输出需标注 engineered。
    """
    if a["type"] == "bottom":              # 向上笔，终点是顶
        thred = b["high"]
        return all(bars[k]["high"] <= thred for k in range(a["bar"] + 1, b["bar"]))
    thred = b["low"]                       # 向下笔，终点是底
    return all(bars[k]["low"] >= thred for k in range(a["bar"] + 1, b["bar"]))


def _can_pair(bars, a, b, min_gap, require_end_peak: bool) -> bool:
    if not _pair_ok(a, b, min_gap):
        return False
    if require_end_peak and bars is not None and not _end_is_peak(bars, a, b):
        return False
    return True


def _prepare_fractals(fractals: list[dict], min_gap: int, bars: list[dict] | None = None,
                      require_end_peak: bool = False) -> list[dict]:
    """77 课三步法，单遍栈实现。

    不变式（函数返回时必然成立）：
        ① kept 里相邻两个分型类型必然不同（顶底交替）；
        ② kept 里每一对相邻分型都满足 _pair_ok（即成笔）。
    因此 build_bis 可以直接把相邻对当成笔，不会再出现「配对失败→游标卡住→尾段断流」。

    与旧版（迭代 200 次）的区别：
        · 同性质相邻：顶留高的、底留低的；**相等时保留先出现的**（77 课「相等的先保留」，
          最终取最先一个，与旧版 build_bis 的取法一致）。
        · 异性质但不成笔：删掉后一个（65 课「转折级别太小」），然后回退继续检查。
    """
    kept: list[dict] = []
    for f in fractals:
        kept.append(f)
        while len(kept) >= 2:
            a, b = kept[-2], kept[-1]
            if a["type"] == b["type"]:
                better = b["px"] > a["px"] if b["type"] == "top" else b["px"] < a["px"]
                if better:
                    kept.pop(-2)      # 后一个更极端 → 换掉前一个，再与更前一个重新检查
                    continue
                kept.pop()            # 不如前一个极端（或相等）→ 丢掉后一个
                break
            if _can_pair(bars, a, b, min_gap, require_end_peak):
                break
            kept.pop()                # 「转折级别太小」→ 删后者，回退检查前一对
            continue
    return kept


def _extreme_raw(bars: list[dict], f: dict) -> int:
    """分型端点对应的**原始 K 线下标**：顶取组内最高那根、底取组内最低那根。"""
    bar = bars[f["bar"]]
    key = "hi_raw" if f["type"] == "top" else "lo_raw"
    return int(bar.get(key, bar.get("i1", f["bar"])))


def build_bis(bars: list[dict], fractals: list[dict], min_gap: int = MIN_FRACTAL_GAP,
              require_end_peak: bool = False) -> list[dict]:
    """按 77 课三步法划分笔。

    require_end_peak: 工程增补钩子 —— 打开后要求笔端点是区间极值（默认关，见 `_end_is_peak`）。

    返回每笔：dir(+1 向上 / -1 向下), start_bar, end_bar, start_px, end_px,
              start_raw, end_raw, start_type, end_type
    其中 *_bar 是合并 K 线下标，*_raw 是原始 K 线下标（端点极值所在的那根，供 MACD 取区间用）。
    """
    if not fractals:
        return []

    kept = _prepare_fractals(fractals, min_gap, bars=bars, require_end_peak=require_end_peak)

    # 契约检查：_prepare_fractals 保证相邻对必可成笔，这里不再「悄悄 continue」。
    broken = [
        (kept[i]["bar"], kept[i + 1]["bar"])
        for i in range(len(kept) - 1)
        if kept[i]["type"] == kept[i + 1]["type"]
        or not _can_pair(bars, kept[i], kept[i + 1], min_gap, require_end_peak)
    ]
    if broken:
        raise RuntimeError(f"分型预处理未收敛，存在 {len(broken)} 个不可成笔的相邻对：{broken[:5]}")

    bis: list[dict] = []
    for a, b in zip(kept, kept[1:]):
        up = a["type"] == "bottom"        # 先底后顶 = 向上笔
        bis.append(
            {
                "dir": 1 if up else -1,
                "start_bar": a["bar"],
                "end_bar": b["bar"],
                "start_px": a["px"],
                "end_px": b["px"],
                "start_raw": _extreme_raw(bars, a),
                "end_raw": _extreme_raw(bars, b),
                "start_type": a["type"],
                "end_type": b["type"],
            }
        )
    return bis


def tail_gap_klc(bars: list[dict], bis: list[dict]) -> int:
    """最后一笔终点距数据末尾还有多少根合并 K 线（健康检查用）。

    正常值应当很小（几十根以内，未确认尾段）。若几百根以上，说明笔断流了 ——
    2026-10-30 之前就是这个函数能一眼看出来的 bug，当时 20 只票里 12 只断流。
    """
    last = bis[-1]["end_bar"] if bis else 0
    return len(bars) - 1 - last


def check_bi_invariants(bis: list[dict]) -> list[str]:
    """笔的硬性质检查（测试与回归都用它）。返回违规描述列表，空列表 = 全部通过。"""
    errs: list[str] = []
    for i, b in enumerate(bis):
        if b["end_bar"] <= b["start_bar"]:
            errs.append(f"第{i}笔 end_bar<=start_bar")
        if (b["dir"] == 1 and b["end_px"] <= b["start_px"]) or (
            b["dir"] == -1 and b["end_px"] >= b["start_px"]
        ):
            errs.append(f"第{i}笔方向与价格方向矛盾")
        if b["end_bar"] - b["start_bar"] < MIN_FRACTAL_GAP:
            errs.append(f"第{i}笔间隔不足 {MIN_FRACTAL_GAP} 根")
        if b["start_raw"] > b["end_raw"]:
            errs.append(f"第{i}笔原始下标倒挂 start_raw>end_raw")
        if i > 0:
            prev = bis[i - 1]
            if prev["dir"] == b["dir"]:
                errs.append(f"第{i}笔与上一笔同向（笔必须上下交替）")
            if prev["end_bar"] != b["start_bar"]:
                errs.append(f"第{i}笔起点未接上一笔终点（端点不衔接）")
            if abs(prev["end_px"] - b["start_px"]) > 1e-9:
                errs.append(f"第{i}笔起始价与上一笔终点价不等（价不衔接）")
    return errs
