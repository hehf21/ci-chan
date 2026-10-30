# -*- coding: utf-8 -*-
"""
缠论多级别分析 · K 线数据源（chan_strict/datasource.py）
================================================================================
对外接口（= __all__）：
    get_kline(code, freq="d", start="2010-01-01", end=None, adjust="qfq", use_cache=True)
        -> pd.DataFrame  [date, open, high, low, close, volume, amount]
    get_index_kline(code="000001", freq="d", start="2010-01-01", ...)
        -> pd.DataFrame  同上
    clear_cache() -> None
    DataSourceError(RuntimeError)  取不到数时抛它，**绝不返回空表冒充成功**

freq : "d" 日线 / "w" 周线 / "30" 30 分钟（另容忍 mon/1/5/15/60 与 day/week/30m 等别名）
adjust: "qfq" 前复权（fqt=1，默认）/ "hfq" 后复权（fqt=2）/ "none" 不复权（fqt=0）

================================================================================
一、最终采用哪一路？—— 东方财富 push2his 直连，走**明文 HTTP**，不用 HTTPS
================================================================================
本机 2026-10-29 20:37~20:45 逐路真跑实测（全部在"清空 HTTP(S)_PROXY + NO_PROXY=*"
的直连条件下）：

  https://push2his.eastmoney.com/api/qt/stock/kline/get   → 失败
      ConnectionError: ('Connection aborted.', RemoteDisconnected(...))
      TLS 握手后对端直接断连；连测 6 次 0 成功，换 1./18.push2his 域名的 https 同样失败。
  http://1.push2his.eastmoney.com/...  （同参数，仅协议换成 http）→ 成功
      HTTP 200，6031 根（600519 全历史 2001-08-27 ~ 2026-10-29），
      连测 3 次耗时 0.24 / 0.25 / 0.21 s，5/5 稳定。
  http://18.push2his.eastmoney.com/...  → 成功，0.35~0.39 s，同样稳定。
  http://push2his.eastmoney.com/...     → 能通但**不稳定**（一次 0.27 s，另一次 21 s，
                                          再换一次直接 15 s 读超时）→ 排到最后。
  ※ 结论：本机只有 **明文 HTTP + 1./18.push2his 二级域名** 这一条稳定可用。
     所以 _EM_HOSTS 的顺序是 (1.push2his, 18.push2his, push2his)，全部以 http:// 拼装；
     跑通后把成功 host 记在模块状态里，下次优先用它。**这里没有 https 重试路径**，
     因为实测 https 在该域名上是确定的失败，重试只会白白多等 2 秒。

  东财字段口径（已实测核对，不是照抄文档）：
    fields2=f51,f52,f53,f54,f55,f56,f57,f58
    klines 每行 = "日期,开,收,高,低,成交量(手),成交额(元),振幅(%)"
    —— 注意第 3 个字段是**收盘**、第 4、5 个是**最高、最低**（不是常见的高-低-收顺序）。
    校验：600519 2026-10-29 行 = "2026-10-29,1349.99,1354.00,1356.00,1330.01,42992,..."
      开 1349.99 / 收 1354.00 / 高 1356.00 / 低 1330.01，与本地 KHunter 库该日
      (1349.99, 1356.00, 1330.01, 1354.00) 逐字段吻合。
    klt: 101=日 102=周 103=月 1/5/15/30/60=分钟；fqt: 0=不复权 1=前复权 2=后复权
    （fqt=3 实测返回 rc=102 + data=null，即"非法取值"，不存在第三种复权）。

--------------------------------------------------------------------------------
备选腾讯 web.ifzq.gtimg.cn —— 实测可用但**深度不够，只留作 d/w 兜底**
--------------------------------------------------------------------------------
  https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,640,qfq
      200，但**单次最多 640 根**：n=2000 依然只回 641 根（2024-03-07 起），
      n=4000 直接 {"code":0,"msg":"param error"}。
      → 拿不到 "2010-01-01 起 >3800 根" 的验收要求，不能当主源。
  m30：web.ifzq 的 fqkline 不认 m30（{"code":1,"msg":"bad params"}）；
      kline/mkline 接口（须用 https://ifzq.gtimg.cn，web.ifzq 会 301 到解不出的
      web3.ifzq.gtimg.cn）最多 640 根、**且不带复权**。
  → 因此腾讯兜底只在 东财整体不可用时对 freq=d/w 生效；amount 缺失留 NaN（不估算）；
    分钟线不做腾讯兜底（无复权 + 只有 640 根），宁可直接报错。

================================================================================
二、复权口径 —— 东财前复权是"差值法"，会出负价（本模块最重要的一个坑）
================================================================================
实测（600519 2023-06-01，同一天四个价）：
    不复权 fqt=0 :  开 1618.00 / 高 1674.99 / 低 1618.00 / 收 1635.92
    前复权 fqt=1 :  开 1438.64 / 高 1495.63 / 低 1438.64 / 收 1456.56
    → 四个价格**各减同一个常数 179.36**（不是各乘同一个系数）。
    后复权 fqt=2 :  开 8834.53 / 高 9120.89 / 低 8834.53 / 收 8924.57（四价不同系数，
      是另一套递推算法，不复现；但**全程为正**）

因为是"每价减累计分红"，高分红 + 长历史的票，越往前价格越被减成负数：
    600519 2010-01-04：不复权开 172.00 → 东财前复权开 **-187.35**（减了 359.35）。
    这是东财接口的真实返回值，不是本模块的 bug，也不是编的。

而本机 KHunter 库 D:\\大龙虾\\股票工具\\khunter_data\\stock_selection.db 的
stock_kline 是**等比法**前复权（同一天四个价除以同一个系数）：
    2023-06-01 KHunter = 开 1431.4234 / 高 1481.8417 / 低 1431.4234 / 收 1447.2770
    除以不复权四价 → 系数恒为 0.884687（四价一致）→ 确认是等比法。
→ 两套口径**必然对不上**，下面是真跑出来的判定证据与逐日比对结果。

**判定证据（算出来的，不是猜的）**：
  ① 同一天 open/high/low/close 四个价：
     东财 (不复权 - 前复权) 的日内极差 = 0.000000 元 → 四价同减一个常数 = 差值法；
     KHunter (KHunter ÷ 不复权) 的日内极差 = 0.0000000000 → 四价比值恒定 = 等比法。
  ② 该差值/比值随时间**只在除权除息日跳变**：823 个交易日里东财 offset 只有 8 个
     取值（0.00 / 28.02 / 51.98 / 79.58 / 103.46 / 134.34 / 153.45 / 179.36），
     KHunter ratio 也正好 8 个；跳变日实测 = 2023-06-30、2023-12-20、2024-06-19、
     2024-12-20、2025-06-26、2025-12-19、2026-06-26 —— 与分红除权节奏一致。

**逐日比对实测（600519，2023-06-01 ~ 2026-10-29，823 个可对齐交易日）**：
    最大绝对偏差 39.5361 元（2023-07-28：东财前复权 1743.55 vs KHunter 1704.01）
    最大相对偏差 2.3202%（同一天）
    偏差 >0.5% 的天数 = **381 / 823 = 46.29%**（落在 2023-06-01 ~ 2026-02-05）
    平均相对偏差 0.6860%
    起点 2023-06-01：东财 1456.56 vs KHunter 1447.28（东财高 0.6414%）
    终点 2026-10-29：两边都是 1354.00（当日无复权，差 0）—— 差额随日期收敛。

**结论（重要）**：这不是取数错误，是两套复权算法本身不同：
    东财 qfq = 不复权 − 累计分红（差值法）：老数据会被减成 0/负数
              （600519 2010-01-04 ~ 2016-09-30 共 1578 根日线非正价）
    KHunter qfq = 不复权 × 累计系数（等比法）：全程为正，价格比值结构不失真
  本模块**不修正、不桥接**这两套口径（硬对齐就是编数据）。给缠论用的话：
    * 只看近几年、个股分红不大 → adjust="qfq"（默认）没问题；
    * 要跨多年（如 2010 起）看茅台这类高分红股 → 用 adjust="hfq"（实测 4077 根
      全程为正、无一根非正价）；或把 start 挪到最后一根非正价之后
      （600519 实测：非正价止于 2016-09-30，start 取 2016-10-01 及以后即全程为正）；
    * 与 KHunter 库混用 → 选一边用到底，绝不能直接相减。

================================================================================
三、分钟线只有最近约 32 个交易日（东财服务端硬上限，绕不过去）
================================================================================
实测 klt=30 无论 beg 填 2024/2023/2020/2005、lmt 填 100000/10000/5000，
一律只回 **256 根**（2026-09-08 10:00 ~ 2026-10-29 15:00，32 个交易日 × 8 根）；
klt=15 → 512 根、klt=5 → 1536 根，同样只覆盖同样的 32 个交易日；
用 end=过去日期"向前翻页"实测返回 0 根（服务端不认 beg/end 做分钟线分页）。
→ 所以 get_kline(code,"30","2024-01-01") **拿不到 3000+ 根**，本模块会
   如实返回这 256 根真实数据 + WARNING + df.attrs["history_truncated"]=True，
   绝不伪造、也绝不假装覆盖到了 2024 年。

  自洽性实测（600519 最近 32 个交易日）：
    8 根/日；把 8 根 30 分钟线聚合成日线后与日线 K 线比 —— open/high/low/close
    最大差 0.0000（完全一致），volume 合计最大差 4600 股、amount 合计最大差
    623.7 万元（约 0.1%，是东财分钟线自身的四舍五入，不是本模块算错）。
    所以要拿日线成交量就别用分钟线去加总，直接用 freq="d"。

================================================================================
四、单位（与同目录 chan_data.py 保持一致，混用会算错）
================================================================================
  volume : **股**。东财 f56 原生是"手"，本模块 ×100 转股（VOLUME_MULTIPLIER=100）。
           校验：600519 2026-10-29 东财 42992 手 → 4,299,200 股；
                 KHunter 同库同日也是 42992 手 → 两者口径一致，都乘 100 即可。
  amount : **元**（东财 f57 原生就是元，不换算）。腾讯兜底无此列 → NaN，不估算。
  date   : datetime64[ns]，日/周/月线为当日 00:00；30 分钟线为该根 K 线的**结束时间**
           （10:00 / 10:30 / ... / 15:00，一天 8 根，与 baostock 的 time 语义一致）。

================================================================================
五、代理
================================================================================
本机 HTTP_PROXY / HTTPS_PROXY = http://127.0.0.1:7897。_no_proxy_env() 只在取数
期间清空这些变量并置 NO_PROXY=*，退出时**逐字恢复原值**（含大小写变体）；另外
requests.Session 统一 trust_env=False 作第二道保险。

================================================================================
六、缓存  chan_strict/cache/
================================================================================
每个 (代码, 周期, 复权) 一份原始全历史缓存：<key>.pkl（DataFrame）+ <key>.json（元数据）。
  TTL 规则（按验收要求）：
    * 请求窗口含"今天"（end 为 None 或 >= 今天）→ 缓存 4 小时内有效（TTL_TODAY）。
    * 请求窗口完全落在历史（end < 今天）→ **永久有效**，但要求缓存已覆盖该窗口
      （容忍 HISTORY_SLACK_DAYS=10 天，避免窗口跨节假日报废）。
  损坏缓存自动忽略 + 删除 + 重取；写盘先写 .tmp 再 os.replace，写失败不影响本次返回。
  use_cache=False → 既不读也不写（纯网络路径，用于冷启动计时）。
  ※ 注意：前复权基准会随新的除权除息整体平移，历史 qfq 数值理论上会变。
    验收要求"历史永久有效"，本模块照办；真遇到分红后想强制刷新，调 clear_cache()。

================================================================================
七、失败语义（禁止静默成功）
================================================================================
  接口连不上 / 返回 data=null / 返回 0 根 K 线 / 解析后 0 行 / 请求窗口内 0 行
  → 一律 raise DataSourceError（RuntimeError 子类），消息里带齐每个 host 的失败原因。
  绝不 return 空 DataFrame，也绝不拿测试数据/占位数据顶替。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pickle
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import requests

__all__ = ["get_kline", "get_index_kline", "clear_cache", "DataSourceError"]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_HERE, "cache")

COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]
PRICE_COLUMNS = ["open", "high", "low", "close"]
NUMERIC_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]

VOLUME_MULTIPLIER = 100          # 东财/腾讯 f56 单位=手 → 统一 ×100 变"股"
AMOUNT_UNIT = "元"

CACHE_SCHEMA = 1                 # 缓存结构版本，改列/改口径必须 +1
TTL_TODAY = 4 * 3600             # 含当日数据的缓存有效期：4 小时
HISTORY_SLACK_DAYS = 10          # 历史窗口"已覆盖"的容忍天数（春节长假最多 9 天）

HTTP_TIMEOUT = 12
MAX_ATTEMPTS = 4
EM_FULL_BEG = "19900101"         # 一律拉全历史，最大化缓存复用
EM_FULL_END = "20500101"
EM_LMT = 100000

# 实测排序：1./18. 二级域名走 http 稳定（0.2~0.4 s），主域名走 http 不稳（0.3~21 s）。
# https 在本机被对端 RST，故不列入（见文件头说明）。
_EM_HOSTS = ("1.push2his.eastmoney.com",
             "18.push2his.eastmoney.com",
             "push2his.eastmoney.com")
_EM_PATH = "/api/qt/stock/kline/get"
_EM_FIELDS1 = "f1,f2,f3,f4,f5,f6"
_EM_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}

_TX_BASE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_TX_MAX_BARS = 640
_TX_MAX_PAGES = 12

# freq -> (东财 klt, 是否日内, 中文名, 腾讯周期名 or None)
_FREQ_SPECS = {
    "d":   ("101", False, "日线",   "day"),
    "w":   ("102", False, "周线",   "week"),
    "mon": ("103", False, "月线",   "month"),
    "1":   ("1",   True,  "1分钟",  None),
    "5":   ("5",   True,  "5分钟",  None),
    "15":  ("15",  True,  "15分钟", None),
    "30":  ("30",  True,  "30分钟", None),
    "60":  ("60",  True,  "60分钟", None),
}
_FREQ_ALIASES = {
    "day": "d", "daily": "d", "d": "d", "1d": "d", "日": "d", "日线": "d",
    "week": "w", "weekly": "w", "w": "w", "1w": "w", "周": "w", "周线": "w",
    "month": "mon", "monthly": "mon", "mon": "mon", "月": "mon", "月线": "mon",
    "1m": "1", "1min": "1", "min": "1",
    "5m": "5", "5min": "5",
    "15m": "15", "15min": "15",
    "30m": "30", "30min": "30", "m30": "30", "30分钟": "30",
    "60m": "60", "60min": "60",
}
_ADJUST_TO_FQT = {"qfq": 1, "hfq": 2, "none": 0}
_ADJUST_TO_TX = {"qfq": "qfq", "hfq": "hfq", "none": ""}

_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy")
_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")

_LOG = logging.getLogger("chan_strict.datasource")
if not _LOG.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[datasource] %(levelname)s %(message)s"))
    _LOG.addHandler(_h)
    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False

_LOCK = threading.RLock()
_SESSION: Optional[requests.Session] = None
_STATE = {"good_host": None, "last_source": None}

_DIGITS_RE = re.compile(r"^\d{6}$")
_PREFIX_RE = re.compile(r"^(sh|sz|bj)\s*[.\-_]?\s*(\d{6})$", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"^(\d{6})\s*[.\-_]?\s*(sh|sz|bj)$", re.IGNORECASE)


class DataSourceError(RuntimeError):
    """取数失败。继承 RuntimeError，调用方用 except RuntimeError 也能接住。"""


class _Retryable(Exception):
    """内部用：这一类失败值得换 host 再试一次。"""


# --------------------------------------------------------------------------- #
# 代理
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _no_proxy_env():
    """取数期间临时摘掉代理并置 NO_PROXY=*，退出时逐字恢复原值。"""
    saved = {}
    for key in _PROXY_KEYS + _NO_PROXY_KEYS:
        if key in os.environ:
            saved[key] = os.environ[key]
    try:
        for key in _PROXY_KEYS:
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        yield
    finally:
        for key in _PROXY_KEYS + _NO_PROXY_KEYS:
            os.environ.pop(key, None)
        for key, value in saved.items():
            os.environ[key] = value


def _session() -> requests.Session:
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            sess = requests.Session()
            sess.trust_env = False           # 第二道保险：忽略环境里的代理配置
            sess.headers.update(_HEADERS)
            _SESSION = sess
        return _SESSION


# --------------------------------------------------------------------------- #
# 参数归一化
# --------------------------------------------------------------------------- #
def _norm_freq(freq) -> str:
    text = str(freq if freq is not None else "d").strip().lower()
    if text in _FREQ_SPECS:                 # 规范名本身直接放过（'d'/'w'/'30'/...）
        return text
    if text not in _FREQ_ALIASES:
        raise ValueError("freq 只支持 'd'/'w'/'30'（另容忍 mon/1/5/15/60 与 "
                         "day/week/30m 等别名），收到 %r" % (freq,))
    return _FREQ_ALIASES[text]


def _norm_adjust(adjust) -> str:
    text = str(adjust if adjust is not None else "qfq").strip().lower()
    if text in ("", "none", "no", "raw", "bfq", "不复权"):
        return "none"
    if text in ("qfq", "前复权", "forward"):
        return "qfq"
    if text in ("hfq", "后复权", "backward"):
        return "hfq"
    raise ValueError("adjust 只支持 'qfq' / 'hfq' / 'none'，收到 %r" % (adjust,))


def _norm_date_ts(value, field: str = "date") -> pd.Timestamp:
    """'2010-01-01' / '20100101' / date / datetime / Timestamp -> Timestamp（00:00）"""
    if value is None:
        raise ValueError("%s 不能为空" % field)
    if isinstance(value, pd.Timestamp):
        return value.normalize()
    if isinstance(value, datetime):
        return pd.Timestamp(value).normalize()
    if isinstance(value, date):
        return pd.Timestamp(value).normalize()
    text = str(value).strip()
    if not text:
        raise ValueError("%s 不能为空" % field)
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8 and re.fullmatch(r"\d{8}", digits):
        try:
            return pd.Timestamp(datetime.strptime(digits, "%Y%m%d")).normalize()
        except ValueError:
            pass
    try:
        return pd.Timestamp(text).normalize()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("无法解析 %s=%r，请用 'YYYY-MM-DD' 或 'YYYYMMDD'"
                         % (field, value)) from exc


def _today() -> date:
    return date.today()


def _market_of(digits: str) -> int:
    """6 位代码 -> 东财 secid 市场号：1=沪 0=深/北。"""
    head = digits[0]
    if head in "69":        # 沪主板 / 科创板 / 沪 B
        return 1
    if head == "5":         # 沪基金
        return 1
    if head in "0123":      # 深市（含深 B、基金）
        return 0
    if head in "48":        # 北交所
        return 0
    raise ValueError("无法从代码 %r 推断交易所" % (digits,))


def _normalize_stock_code(code) -> "tuple[str, int]":
    """'600519' / 'sh600519' / '000001.SZ' / 'sz.000001' / 600519 -> ('600519', 1)"""
    if code is None:
        raise ValueError("code 不能为空")
    text = str(code).strip().replace(" ", "")
    if not text:
        raise ValueError("code 不能为空")

    exchange = digits = None
    m = _PREFIX_RE.match(text)
    if m:
        exchange, digits = m.group(1).lower(), m.group(2)
    else:
        m = _SUFFIX_RE.match(text)
        if m:
            digits, exchange = m.group(1), m.group(2).lower()
        elif _DIGITS_RE.match(text):
            digits = text
        else:
            raise ValueError("无法识别的股票代码 %r；支持 '600519' / 'sh600519' / "
                             "'000001.SZ' / 'sz.000001'" % (code,))

    market = {"sh": 1, "sz": 0, "bj": 0}[exchange] if exchange else _market_of(digits)
    return digits, market


def _normalize_index_code(code) -> "tuple[str, int, str]":
    """index code -> (digits, market, secid)

    东财指数 secid 实测：
      1.000001 上证指数 / 1.000300 沪深300 / 1.000905 中证500 / 1.000016 上证50
      0.399001 深证成指 / 0.399006 创业板指 / 0.899050 北证50
    """
    if code is None:
        raise ValueError("index code 不能为空")
    text = str(code).strip().replace(" ", "")
    if not text:
        raise ValueError("index code 不能为空")

    exchange = digits = None
    m = _PREFIX_RE.match(text)
    if m:
        exchange, digits = m.group(1).lower(), m.group(2)
    else:
        m = _SUFFIX_RE.match(text)
        if m:
            digits, exchange = m.group(1), m.group(2).lower()
        elif _DIGITS_RE.match(text):
            digits = text
        else:
            raise ValueError("无法识别的指数代码 %r；支持 '000001' / 'sh000001' / "
                             "'399001.SZ'" % (code,))

    if exchange is not None:
        market = 1 if exchange == "sh" else 0
    elif digits.startswith(("399", "899")):
        market = 0                     # 深证系列 / 北证系列
    else:
        market = 1                     # 上证系列（含 000300/000905/000016 等）
    return digits, market, "%d.%s" % (market, digits)


# --------------------------------------------------------------------------- #
# 磁盘缓存
# --------------------------------------------------------------------------- #
def _cache_key(digits: str, freq: str, adjust: str) -> str:
    return "em_%s_%s_%s" % (digits, freq, adjust)


def _cache_paths(key: str) -> "tuple[str, str]":
    os.makedirs(CACHE_DIR, exist_ok=True)
    base = os.path.join(CACHE_DIR, key)
    return base + ".pkl", base + ".json"


def _cache_read(key: str) -> "Optional[tuple[pd.DataFrame, dict]]":
    pkl_path, meta_path = _cache_paths(key)
    if not (os.path.exists(pkl_path) and os.path.exists(meta_path)):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(pkl_path, "rb") as fh:
            df = pickle.load(fh)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("缓存损坏，忽略并重取：%s（%s: %s）", key, type(exc).__name__, exc)
        _cache_drop(key)
        return None

    if not isinstance(meta, dict) or meta.get("schema") != CACHE_SCHEMA:
        _LOG.info("缓存结构版本不符，忽略：%s", key)
        return None
    if not isinstance(df, pd.DataFrame) or list(df.columns) != COLUMNS:
        _LOG.info("缓存列结构已变更，忽略：%s", key)
        return None
    if int(meta.get("rows") or -1) != len(df):
        _LOG.warning("缓存元数据行数(%s)与数据(%d)不符，忽略：%s",
                     meta.get("rows"), len(df), key)
        return None
    return df, meta


def _cache_write(key: str, df: pd.DataFrame, meta: dict) -> None:
    pkl_path, meta_path = _cache_paths(key)
    payload = dict(meta)
    payload["schema"] = CACHE_SCHEMA
    payload["rows"] = int(len(df))
    payload["columns"] = list(COLUMNS)
    payload["volume_unit"] = "股"
    payload["amount_unit"] = AMOUNT_UNIT
    try:
        tmp_pkl = pkl_path + ".tmp"
        with open(tmp_pkl, "wb") as fh:
            pickle.dump(df, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp_pkl, pkl_path)

        tmp_meta = meta_path + ".tmp"
        with open(tmp_meta, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_meta, meta_path)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("缓存写入失败（不影响本次返回）：%s（%s: %s）", key, type(exc).__name__, exc)
        for path in (pkl_path + ".tmp", meta_path + ".tmp"):
            with contextlib.suppress(OSError):
                os.remove(path)


def _cache_drop(key: str) -> None:
    for path in _cache_paths(key):
        with contextlib.suppress(OSError):
            os.remove(path)


def _cache_usable(meta: dict, need_today: bool,
                  start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> bool:
    """TTL 判定：含今天 → 4 小时；纯历史窗口 → 永久（但需已覆盖该窗口）。"""
    fetched_at = float(meta.get("fetched_at") or 0)
    age = time.time() - fetched_at
    if need_today:
        return age <= TTL_TODAY

    first_raw, last_raw = meta.get("first_date"), meta.get("last_date")
    if not first_raw or not last_raw:
        return False
    try:
        first = pd.Timestamp(first_raw).normalize()
        last = pd.Timestamp(last_raw).normalize()
    except Exception:  # noqa: BLE001
        return False

    if last < end_ts - pd.Timedelta(days=HISTORY_SLACK_DAYS):
        return False                       # 缓存没盖住窗口右端
    # 东财一次拉全历史（beg=19900101），first_date 就是该标的上市首日，
    # 起点比请求晚是"本来就没更早的数据"，不该反复重取。
    # 只有兜底源（腾讯分页可能没翻到底）才要求盖住请求起点。
    if not meta.get("full_history", True) and first > start_ts:
        return False
    return True


# --------------------------------------------------------------------------- #
# 解析 / 规整 / 质检
# --------------------------------------------------------------------------- #
def _finalize(df: pd.DataFrame, intraday: bool) -> pd.DataFrame:
    """列固定、类型固定、去重、升序、RangeIndex 0..n-1、date 为 datetime64[ns]。"""
    out = df.loc[:, COLUMNS].copy()

    fmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"
    parsed = pd.to_datetime(out["date"].astype(str), format=fmt, errors="coerce")
    out["date"] = parsed
    out = out.loc[out["date"].notna()]

    for col in NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    if not intraday:
        out["date"] = out["date"].dt.normalize()

    out = out.drop_duplicates(subset=["date"], keep="last")
    out = out.sort_values("date", kind="mergesort").reset_index(drop=True)

    out["date"] = out["date"].astype("datetime64[ns]")
    for col in NUMERIC_COLUMNS:
        out[col] = out[col].astype("float64")
    return out


def _quality_check(df: pd.DataFrame, freq_label: str, adjust: str,
                   requested_start: pd.Timestamp) -> "list[str]":
    """做真数据体检，返回告警文案列表（同时打日志）。绝不改写数据。"""
    warns = []
    nonpos_mask = (df[PRICE_COLUMNS] <= 0).any(axis=1)
    nonpos = int(nonpos_mask.sum())
    if nonpos:
        bad = df.loc[nonpos_mask, "date"]
        warns.append(
            "%s 有 %d 根 K 线出现非正价格（%s ~ %s）。原因：东财/腾讯的 qfq 前复权"
            "都按「不复权价减累计分红」的差值法计算，高分红长历史个股越往前越会被"
            "减成 0/负数。跨多年分析请改用 adjust='hfq'，或把 start 截到前复权价"
            "仍 >0 之后。"
            % (freq_label, nonpos, bad.min().strftime("%Y-%m-%d"),
               bad.max().strftime("%Y-%m-%d")))

    hl = int((df["high"] < df["low"]).sum())
    if hl:
        warns.append("%s 有 %d 根 K 线的 high < low（源数据异常）" % (freq_label, hl))

    body = int(((df["high"] < df[["open", "close"]].max(axis=1)) |
                (df["low"] > df[["open", "close"]].min(axis=1))).sum())
    if body:
        warns.append("%s 有 %d 根 K 线的 open/close 跑出 [low, high] 区间（源数据异常）"
                     % (freq_label, body))

    nan_amt = int(df["amount"].isna().sum())
    if nan_amt:
        warns.append("%s 有 %d 根 K 线缺成交额（保留 NaN，未估算）" % (freq_label, nan_amt))

    real_first = df["date"].min()
    if real_first > requested_start + pd.Timedelta(days=HISTORY_SLACK_DAYS):
        warns.append("%s 实际起点 %s 晚于请求起点 %s（源端更早无数据/未上市）"
                     % (freq_label, real_first.strftime("%Y-%m-%d"),
                        requested_start.strftime("%Y-%m-%d")))

    for msg in warns:
        _LOG.warning(msg)
    return warns


# --------------------------------------------------------------------------- #
# 东方财富
# --------------------------------------------------------------------------- #
def _host_order() -> "list[str]":
    hosts = list(_EM_HOSTS)
    good = _STATE.get("good_host")
    if good in hosts:
        hosts.remove(good)
        hosts.insert(0, good)
    return hosts


def _em_get(secid: str, klt: str, fqt: int) -> "tuple[list, dict]":
    """打东财 kline 接口，返回 (klines, data_dict)。失败抛 DataSourceError。"""
    params = {
        "secid": secid,
        "fields1": _EM_FIELDS1,
        "fields2": _EM_FIELDS2,
        "klt": klt,
        "fqt": fqt,
        "beg": EM_FULL_BEG,
        "end": EM_FULL_END,
        "lmt": EM_LMT,
    }
    errors = []
    sess = _session()
    for attempt in range(MAX_ATTEMPTS):
        for host in _host_order():
            url = "http://%s%s" % (host, _EM_PATH)
            try:
                t0 = time.perf_counter()
                resp = sess.get(url, params=params, timeout=HTTP_TIMEOUT)
                cost = time.perf_counter() - t0
                if resp.status_code != 200:
                    raise _Retryable("HTTP %s" % resp.status_code)
                try:
                    payload = resp.json()
                except ValueError as exc:
                    raise _Retryable("响应不是 JSON（前 100 字节 %r）"
                                     % (resp.content[:100],)) from exc
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise DataSourceError(
                        "东财 data=null（rc=%r）。secid=%s klt=%s fqt=%s —— "
                        "通常是代码不存在 / 该 secid 没有这个周期 / 参数非法。"
                        "接口原文：%s"
                        % (payload.get("rc"), secid, klt, fqt,
                           json.dumps(payload, ensure_ascii=False)[:200]))
                klines = data.get("klines")
                if not isinstance(klines, list) or not klines:
                    raise DataSourceError(
                        "东财返回 0 根 K 线（secid=%s klt=%s fqt=%s，data=%s）"
                        % (secid, klt, fqt,
                           json.dumps({k: v for k, v in data.items() if k != "klines"},
                                      ensure_ascii=False)[:200]))

                _STATE["good_host"] = host
                _LOG.info("东财 %s %s klt=%s fqt=%s -> %d 根，%.2f s",
                          host, data.get("name") or secid, klt, fqt, len(klines), cost)
                return klines, data
            except _Retryable as exc:
                errors.append("%s: %s" % (host, exc))
                _LOG.debug("东财 host %s 第 %d 轮失败：%s", host, attempt + 1, exc)
            except requests.RequestException as exc:
                errors.append("%s: %s: %s" % (host, type(exc).__name__, exc))
                _LOG.debug("东财 host %s 第 %d 轮失败：%s", host, type(exc).__name__, exc)
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(0.4 * (attempt + 1))

    raise DataSourceError(
        "东财 push2his 取数失败（secid=%s klt=%s fqt=%s，共 %d 轮 × %d host）。"
        "各路原因：\n  - %s"
        % (secid, klt, fqt, MAX_ATTEMPTS, len(_EM_HOSTS), "\n  - ".join(errors[-6:])))


def _parse_em_klines(klines: "list[str]", intraday: bool) -> pd.DataFrame:
    """'日期,开,收,高,低,量,额,振幅' -> 标准列（注意第 3 字段是收盘）。"""
    recs = []
    bad = 0
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 7:
            bad += 1
            continue
        # date, open, close, high, low, volume, amount
        recs.append((parts[0], parts[1], parts[3], parts[4], parts[2],
                     parts[5], parts[6]))
    if bad:
        _LOG.warning("东财返回 %d 行字段数不足 7，已跳过", bad)
    if not recs:
        raise DataSourceError("东财返回的 klines 全部解析失败（%d 行）" % len(klines))

    raw = pd.DataFrame(recs, columns=["date", "open", "high", "low", "close",
                                      "volume", "amount"])
    df = _finalize(raw, intraday=intraday)
    df["volume"] = df["volume"] * VOLUME_MULTIPLIER     # 手 -> 股
    return df


# --------------------------------------------------------------------------- #
# 腾讯兜底（仅 d/w；单次 640 根，需按日期窗口向前翻页）
# --------------------------------------------------------------------------- #
def _tx_code(digits: str, market: int) -> str:
    if digits.startswith(("4", "8")):
        return "bj" + digits
    return ("sh" if market == 1 else "sz") + digits


def _fetch_tencent(digits: str, market: int, freq: str, adjust: str,
                   start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    kind = _FREQ_SPECS[freq][3]
    if kind is None:
        raise DataSourceError("腾讯兜底不支持 %s（分钟线无复权且仅 640 根）"
                              % _FREQ_SPECS[freq][2])

    code = _tx_code(digits, market)
    fq = _ADJUST_TO_TX[adjust]
    sess = _session()
    pieces = []
    errors = []
    end_cur = min(end_ts, pd.Timestamp(_today()))
    lowest = None

    for _ in range(_TX_MAX_PAGES):
        # 640 根日线 ≈ 2.5 年 ≈ 930 自然日；窗口必须开得比 640 根更宽，
        # 否则一页装不满 640 会被误判成"到头了"而提前收工（实测踩过：900 天只回 599 根）。
        beg_cur = end_cur - pd.Timedelta(days=1400)
        param = "%s,%s,%s,%s,%d,%s" % (code, kind, beg_cur.strftime("%Y-%m-%d"),
                                       end_cur.strftime("%Y-%m-%d"),
                                       _TX_MAX_BARS, fq)
        try:
            resp = sess.get(_TX_BASE, params={"param": param}, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                raise _Retryable("HTTP %s" % resp.status_code)
            payload = resp.json()
        except (requests.RequestException, ValueError, _Retryable) as exc:
            errors.append("%s: %s" % (code, type(exc).__name__))
            break

        data = (payload or {}).get("data")
        block = data.get(code) if isinstance(data, dict) else None
        arr = None
        if isinstance(block, dict):
            for key in (fq + kind, kind):
                if isinstance(block.get(key), list) and block[key]:
                    arr = block[key]
                    break
        if not arr:
            break

        rows = [r for r in arr if isinstance(r, (list, tuple)) and len(r) >= 6]
        if not rows:
            break
        pieces.append(rows)
        first_dt = pd.Timestamp(str(rows[0][0])).normalize()
        lowest = first_dt
        if first_dt <= start_ts or len(arr) < _TX_MAX_BARS:
            break
        end_cur = first_dt - pd.Timedelta(days=1)

    if not pieces:
        raise DataSourceError(
            "腾讯兜底也失败（%s %s %s）：%s" % (code, kind, adjust,
                                              " | ".join(errors) or "无数据"))
    if lowest is not None and lowest > start_ts + pd.Timedelta(days=HISTORY_SLACK_DAYS):
        _LOG.warning("腾讯兜底只能回溯到 %s（请求 %s），单次上限 %d 根",
                     lowest.strftime("%Y-%m-%d"), start_ts.strftime("%Y-%m-%d"),
                     _TX_MAX_BARS)

    recs = []
    for rows in pieces:
        for r in rows:
            # date, open, close, high, low, volume(手)
            recs.append((r[0], r[1], r[3], r[4], r[2], r[5], None))
    raw = pd.DataFrame(recs, columns=["date", "open", "high", "low", "close",
                                      "volume", "amount"])
    df = _finalize(raw, intraday=False)
    df["volume"] = df["volume"] * VOLUME_MULTIPLIER     # 手 -> 股
    _LOG.warning("已降级到腾讯源：%s %s %s -> %d 行（amount 缺失=NaN，未估算）",
                 code, kind, adjust, len(df))
    return df


# --------------------------------------------------------------------------- #
# 对外 API
# --------------------------------------------------------------------------- #
def get_kline(code: str, freq: str = "d", start: str = "2010-01-01",
              end: Optional[str] = None, adjust: str = "qfq",
              use_cache: bool = True) -> pd.DataFrame:
    """
    A 股 K 线（东财 push2his 直连，主源）。

    code   : '600519' / 'sh600519' / '000001.SZ' / 'sz.000001'（内部归一化）
    freq   : 'd' 日线 / 'w' 周线 / '30' 30 分钟（另容忍 mon/1/5/15/60 与别名）
    start  : '2010-01-01' 或 '20100101'
    end    : 同上；None = 至今
    adjust : 'qfq'(默认) / 'hfq' / 'none'
    use_cache : False = 既不读也不写缓存（纯网络路径）

    返回列固定 ['date','open','high','low','close','volume','amount']，
    date 为 datetime64[ns] 升序，索引 0..n-1；volume 单位股、amount 单位元。

    30 分钟线只能拿到最近约 32 个交易日（东财服务端硬上限，实测 256 根），
    此时 df.attrs["history_truncated"] 为 True 并打 WARNING，但返回的仍是真数据。

    取不到就 raise DataSourceError，绝不返回空表。
    """
    freq_n = _norm_freq(freq)
    adjust_n = _norm_adjust(adjust)
    digits, market = _normalize_stock_code(code)
    secid = "%d.%s" % (market, digits)

    start_ts = _norm_date_ts(start, "start")
    end_ts = _norm_date_ts(end, "end") if end is not None else pd.Timestamp(_today())
    if start_ts > end_ts:
        raise ValueError("start(%s) 晚于 end(%s)"
                         % (start_ts.strftime("%Y-%m-%d"), end_ts.strftime("%Y-%m-%d")))

    klt, intraday, freq_label, _ = _FREQ_SPECS[freq_n]
    need_today = (end is None) or (end_ts >= pd.Timestamp(_today()))
    key = _cache_key(digits, freq_n, adjust_n)

    # ---- 缓存 ----
    source = "eastmoney"
    meta_extra = {}
    if use_cache:
        hit = _cache_read(key)
        if hit is not None:
            cached_df, cached_meta = hit
            if _cache_usable(cached_meta, need_today, start_ts, end_ts):
                _LOG.info("缓存命中 %s freq=%s adjust=%s（%s 抓取，%d 行）",
                          digits, freq_n, adjust_n,
                          cached_meta.get("fetched_readable", "?"), len(cached_df))
                out = _slice(cached_df, start_ts, end_ts)
                if len(out):
                    return _decorate(out, cached_meta, freq_label, adjust_n,
                                     digits, start_ts)
                _LOG.info("缓存虽在但窗口内 0 行，重取：%s", key)

    # ---- 网络 ----
    errors = []
    raw_df = None
    with _no_proxy_env():
        try:
            klines, data = _em_get(secid, klt, _ADJUST_TO_FQT[adjust_n])
            raw_df = _parse_em_klines(klines, intraday)
            meta_extra = {"em_name": data.get("name") or "",
                          "em_dktotal": data.get("dktotal"),
                          "em_host": _STATE.get("good_host")}
            source = "eastmoney"
        except Exception as exc:  # noqa: BLE001
            errors.append("eastmoney: %s" % exc)
            _LOG.warning("东财失败，尝试腾讯兜底 —— %s: %s", type(exc).__name__, exc)
            if freq_n in ("d", "w", "mon"):
                try:
                    raw_df = _fetch_tencent(digits, market, freq_n, adjust_n,
                                            start_ts, end_ts)
                    source = "tencent"
                    meta_extra = {"em_name": "", "em_dktotal": None, "em_host": None,
                                  "full_history": False}
                except Exception as exc2:  # noqa: BLE001
                    errors.append("tencent: %s" % exc2)

    if raw_df is None or len(raw_df) == 0:
        raise DataSourceError(
            "get_kline(%r, freq=%r, start=%r, end=%r, adjust=%r) 所有数据源均失败：\n"
            "  - %s" % (code, freq_n, start, end, adjust_n, "\n  - ".join(errors)))

    # ---- 缓存写（存全历史，最大化复用）----
    if use_cache:
        _cache_write(key, raw_df, {
            "key": key,
            "code": digits,
            "secid": secid,
            "freq": freq_n,
            "klt": klt,
            "adjust": adjust_n,
            "fqt": _ADJUST_TO_FQT[adjust_n],
            "source": source,
            "fetched_at": time.time(),
            "fetched_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "first_date": raw_df["date"].min().strftime("%Y-%m-%d"),
            "last_date": raw_df["date"].max().strftime("%Y-%m-%d"),
            **meta_extra,
        })

    out = _slice(raw_df, start_ts, end_ts)
    if len(out) == 0:
        raise DataSourceError(
            "get_kline(%r, freq=%r) 源端返回了 %d 根（%s ~ %s），但请求窗口 "
            "%s ~ %s 内 0 行 —— 该区间大概率没有交易日/尚未上市。"
            % (code, freq_n, len(raw_df),
               raw_df["date"].min().strftime("%Y-%m-%d"),
               raw_df["date"].max().strftime("%Y-%m-%d"),
               start_ts.strftime("%Y-%m-%d"), end_ts.strftime("%Y-%m-%d")))

    return _decorate(out, {"source": source, **meta_extra}, freq_label, adjust_n,
                     digits, start_ts)


def get_index_kline(code: str = "000001", freq: str = "d",
                    start: str = "2010-01-01", end: Optional[str] = None,
                    adjust: str = "qfq", use_cache: bool = True) -> pd.DataFrame:
    """
    指数 K 线（东财 push2his 直连）。默认 000001 = 上证指数。

    指数没有分红，东财对指数忽略 fqt；为语义清楚，本函数内部一律按 fqt=0 取，
    返回的 df.attrs["adjust"] 记为 'none'。
    """
    freq_n = _norm_freq(freq)
    _ = _norm_adjust(adjust)              # 仅做合法性校验，指数不参与复权
    digits, market, secid = _normalize_index_code(code)

    start_ts = _norm_date_ts(start, "start")
    end_ts = _norm_date_ts(end, "end") if end is not None else pd.Timestamp(_today())
    if start_ts > end_ts:
        raise ValueError("start 晚于 end")

    klt, intraday, freq_label, _ = _FREQ_SPECS[freq_n]
    need_today = (end is None) or (end_ts >= pd.Timestamp(_today()))
    key = _cache_key("idx" + digits, freq_n, "none")

    with _no_proxy_env():
        if use_cache:
            hit = _cache_read(key)
            if hit is not None:
                cached_df, cached_meta = hit
                if _cache_usable(cached_meta, need_today, start_ts, end_ts):
                    _LOG.info("缓存命中 指数%s freq=%s（%s 抓取，%d 行）",
                              digits, freq_n,
                              cached_meta.get("fetched_readable", "?"), len(cached_df))
                    out = _slice(cached_df, start_ts, end_ts)
                    if len(out):
                        return _decorate(out, cached_meta, freq_label, "none",
                                         digits, start_ts)

        klines, data = _em_get(secid, klt, 0)
        raw_df = _parse_em_klines(klines, intraday)
        name = data.get("name") or ""

        if use_cache:
            _cache_write(key, raw_df, {
                "key": key,
                "code": digits,
                "secid": secid,
                "freq": freq_n,
                "klt": klt,
                "adjust": "none",
                "fqt": 0,
                "source": "eastmoney",
                "fetched_at": time.time(),
                "fetched_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "first_date": raw_df["date"].min().strftime("%Y-%m-%d"),
                "last_date": raw_df["date"].max().strftime("%Y-%m-%d"),
                "em_name": name,
                "em_dktotal": data.get("dktotal"),
                "em_host": _STATE.get("good_host"),
            })

    out = _slice(raw_df, start_ts, end_ts)
    if len(out) == 0:
        raise DataSourceError("指数 %s 在 %s ~ %s 内 0 行"
                              % (secid, start_ts.strftime("%Y-%m-%d"),
                                 end_ts.strftime("%Y-%m-%d")))
    return _decorate(out, {"source": "eastmoney", "em_name": name},
                     freq_label, "none", digits, start_ts)


def clear_cache() -> None:
    """清空 chan_strict/cache/ 下的全部缓存文件（.pkl / .json / .tmp）。"""
    if not os.path.isdir(CACHE_DIR):
        _LOG.info("缓存目录不存在，无需清理：%s", CACHE_DIR)
        return
    removed = 0
    failed = 0
    for name in os.listdir(CACHE_DIR):
        if not name.endswith((".pkl", ".json", ".tmp")):
            continue
        path = os.path.join(CACHE_DIR, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except OSError as exc:
            failed += 1
            _LOG.warning("删除缓存文件失败：%s（%s）", path, exc)
    _LOG.info("缓存已清理：删除 %d 个文件，失败 %d 个（目录 %s）",
              removed, failed, CACHE_DIR)


# --------------------------------------------------------------------------- #
# 内部小工具
# --------------------------------------------------------------------------- #
def _slice(df: pd.DataFrame, start_ts: pd.Timestamp,
           end_ts: pd.Timestamp) -> pd.DataFrame:
    """按 [start, end] 切片（end 含当天全部 K 线）。返回独立副本。"""
    end_excl = end_ts + pd.Timedelta(days=1)
    mask = (df["date"] >= start_ts) & (df["date"] < end_excl)
    out = df.loc[mask].reset_index(drop=True).copy()
    out["date"] = out["date"].astype("datetime64[ns]")
    return out


def _decorate(df: pd.DataFrame, meta: dict, freq_label: str, adjust: str,
              digits: str, requested_start: pd.Timestamp) -> pd.DataFrame:
    """挂 attrs + 跑质检（质检只告警，不改数据）。"""
    out = df.copy()
    source = meta.get("source") or _STATE.get("last_source") or "unknown"
    warns = _quality_check(out, freq_label, adjust, requested_start)

    truncated = False
    if freq_label.endswith("分钟") and len(out) <= 256:
        first_seen = out["date"].min()
        if first_seen > requested_start + pd.Timedelta(days=HISTORY_SLACK_DAYS):
            truncated = True
            msg = ("%s 仅取到 %d 根，起点 %s（请求 %s）。东财分钟线服务端硬上限约 "
                   "32 个交易日，拿不到更早的分钟数据 —— 本模块不给假数据。"
                   % (freq_label, len(out), first_seen.strftime("%Y-%m-%d %H:%M"),
                      requested_start.strftime("%Y-%m-%d")))
            warns.append(msg)
            _LOG.warning(msg)

    out.attrs.update({
        "source": source,
        "em_name": meta.get("em_name") or "",
        "code": digits,
        "freq": freq_label,
        "adjust": adjust,
        "volume_unit": "股",
        "amount_unit": AMOUNT_UNIT,
        "fetched_readable": meta.get("fetched_readable", ""),
        "history_truncated": truncated,
        "warnings": warns,
    })
    return out


# --------------------------------------------------------------------------- #
# 自检（python datasource.py 直接跑）
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys

    _LOG.setLevel(logging.INFO)
    print("=" * 78)
    print("chan_strict/datasource.py 自检 —— 只做冒烟，完整验收看 _check_datasource.py")
    print("=" * 78)
    try:
        _df = get_kline("600519", "d", "2024-01-01")
        print("OK  600519 日线 %d 行  %s ~ %s  来源=%s 名称=%s"
              % (len(_df), _df["date"].iloc[0].date(), _df["date"].iloc[-1].date(),
                 _df.attrs.get("source"), _df.attrs.get("em_name")))
        _idx = get_index_kline("000001", "d", "2024-01-01")
        print("OK  上证指数 %d 行  最新收盘 %.2f"
              % (len(_idx), _idx["close"].iloc[-1]))
    except DataSourceError as exc:
        print("FAIL %s" % exc)
        sys.exit(1)
