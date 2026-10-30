# -*- coding: utf-8 -*-
"""
缠论策略 · 数据层（chan_data.py）
=================================

本次对外 API（= __all__，只有这三个）：
    get_daily(code, start, end=None, adjust="qfq")   -> pd.DataFrame
    get_min30(code, start, end=None, adjust="qfq")   -> pd.DataFrame | None
    list_all_codes()                                 -> list[str]

另保留 get_index() / code_name() 两个**兼容旧调用**的辅助函数（不在 __all__ 内），
因为同目录的 run_daily.py 与 chan_backtest.py 已经在调用它们，删掉会把整条回测/
选股链路弄坏。它们只读本机 KHunter 库，不联网。

依赖仅限 pandas / numpy / sqlite3 / akshare / baostock / requests。
（akshare、baostock 惰性导入：没装也不影响 import 本模块与其它数据源）

===============================================================================
数据源实测结论（2026-10-29 20:2x 在本机 D:\\大龙虾 逐路实测，数字都是真跑出来的）
===============================================================================
【最终采用：baostock 0.9.40 作主源】

① 本机 KHunter SQLite
   D:\\大龙虾\\股票工具\\khunter_data\\stock_selection.db（5,089,067,008 B ≈ 4.74 GiB）
   sqlite_master 探表 44 张；PRAGMA table_info(stock_kline) =
       id, code, date, open, high, low, close, volume, market_cap, K, D, J,
       created_date, updated_date
   实测：
     - stock_kline 4,290,157 行 / 5382 个 code，但**覆盖区间只有 2023-06-01 ~ 2026-10-29**
       （600519、000001 各 823 行）→ 满足不了 "2020-01-01 起 ≥1000 行"
     - **没有 amount 列**，也没有复权因子列
     - date 是 TEXT 'YYYY-MM-DD'；volume 单位是"手"
       （600519 2026-10-29 volume=42992 ≈ baostock 4,299,231 股 / 100）
     - 是否为前复权：**确实是复权值，但复权基准与当日重算的 qfq 不一致**。实测
         600519 2023-06-01  KHunter 1447.2770 vs baostock 前复权 1447.4342（差 0.01%）
         000001 2023-06-01  KHunter    9.4972 vs baostock 前复权    9.2948（差 2.2%）
       说明它锚定在某个历史日期，跨源混用会造成复权口径打架 → 不宜作主源。
     - minute_fqgate 虽有 22,891,349 行且带 amount，但 600519 在该表 0 行，且是分钟
       快照门控表，不是全市场 30 分钟 K 线；stock_minutes 只有当天数据
   结论：**不作主源**。仅保留为 adjust="none" 的最后兜底（amount=NaN，日志明确说明）。
   ※ 上一版文件把 KHunter 当主源并断言"已验证是前复权"，上面两行实测说明该说法
     只对 600519 近似成立、对 000001 差 2.2%，故本次改用 baostock 并在此留痕。

② akshare 1.18.64 —— 本机当前**不通**（两条路都反复实测）：
   - 走环境代理 HTTP(S)_PROXY=http://127.0.0.1:7897（注册表 ProxyEnable=1,
     ProxyServer=127.0.0.1:7897，ProxyOverride 不含 eastmoney）：
       ProxyError: Unable to connect to proxy, RemoteDisconnected
   - 清空代理 + NO_PROXY=*（直连）：
       push2his.eastmoney.com 连测 6 次 0 成功、stock_zh_a_hist 连测 3 次全失败，
       均为 ConnectionError: RemoteDisconnected
   → 保留为第二顺位 fallback（网络/代理恢复后自动可用），失败不致命。
   期间曾出现 1 次 HTTP 200 直连成功（115 KB），属服务端限流型不稳定，不能当主源。

③ baostock 0.9.40 —— **可用且口径最干净（最终采用）**：
   - 走自有 socket（baostock.com:10030），**完全不经过 HTTP(S)_PROXY**，不受本机代理影响
   - 日线 sh.600519 2020-01-01~2026-10-29 = 1652 行，末行 2026-10-29（最新交易日）
   - 自带 amount（元），adjustflag 2=前复权；实测 qfq 生效：
     600519 2020-01-02 不复权 close=1130.00 → 前复权 956.41
   - 30 分钟线：600519 2024-01-01~ = 5456 行；**实测 30 分钟历史仅从 2020-01-01 起**
     （2010/2013/2016/2017/2018/2019 各年均为 0 行），日线可回溯到上市首日
   - query_stock_basic() 9025 行，type: 1=股票 5564 / 2=指数 596 / 4=可转债 1145 /
     5=ETF 1720；type=='1' 的 6 位代码前缀为 300/301/302/002/003/000/001/600/601/
     603/605/688/689，**天然不含 900/200 B 股、不含指数与基金**
   - 代码表在市口径实测：type=1 共 5564，其中 status=='1' 为 5223，
     status=='0' 的 341 个 **outDate 全部非空**（即真退市）；"按 status 过滤"与
     "按 outDate 为空过滤"结果完全一致（5223 vs 5223，差集 0），
     工商银行/宁德时代/比亚迪/金山办公等均在列 → 5223 就是当前在市 A 股普通股
   - 坑1：非法代码/超范围返回 error_code='0' 但 0 行（不报错）→ 必须按"空结果=取不到"处理
   - 坑2：缺交易所前缀（"600519"）直接 error_code=10004006 → 内部必须归一化
   - 坑3：query_stock_basic 单次耗时 21.8 秒 → 必须磁盘缓存
   - 坑4（曾踩）：30 分钟线的 date/time 不能拼成 "%Y-%m-%d %Y%m%d%H%M%S" 一次解析，
     %Y 重复会让 pandas 构造正则时报 "redefinition of group name 'Y'"。
     现在改为 date 取日期部分、time 取时分秒再相加。

===============================================================================
代理处理
===============================================================================
本机 HTTP_PROXY / HTTPS_PROXY = http://127.0.0.1:7897。_no_proxy_env() 上下文管理器
**只在取数调用期间**清空这些变量并置 NO_PROXY=*，退出时逐字恢复原值（含大小写变体）。
baostock 走原始 socket 不受影响；akshare 的 requests 调用一律包在该上下文内。

===============================================================================
单位与口径（重要：与其它脚本混用会算错）
===============================================================================
- volume 统一 **股**（baostock 原生股；akshare/KHunter 原生态"手"，内部 ×100）
- amount 统一 **元**（baostock 原生元；KHunter 无此列 → NaN，不补 0、不估算）
- adjust 默认 qfq；前复权基准是最新价，越靠近今天越接近真实成交价
- get_min30 的 date = 该根 30 分钟 K 线的**结束时间**（baostock time 语义；
  一天 8 根：10:00/10:30/11:00/11:30/13:30/14:00/14:30/15:00）
- 日线 date 为当日 00:00 的 datetime64[ns]；索引恒为 0..n-1

===============================================================================
缓存
===============================================================================
目录 D:\\大龙虾\\股票工具\\缠论\\策略\\cache\\，pickle，key = 源+代码+起止+复权。
新鲜度：区间已结束（end < 今天）→ 永久命中；end 为今天/未给 → 6 小时 TTL；
代码表 7 天 TTL。损坏缓存自动忽略并重取；写失败不影响本次返回。
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import pickle
import re
import sqlite3
import threading
import time
from datetime import date, datetime
from typing import Optional

import pandas as pd

__all__ = ["get_daily", "get_min30", "list_all_codes"]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
CACHE_DIR = r"D:\大龙虾\股票工具\缠论\策略\cache"
KHUNTER_DB = r"D:\大龙虾\股票工具\khunter_data\stock_selection.db"

COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]
NUMERIC_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]

CACHE_SCHEMA_VERSION = 2      # 2: date 统一到 datetime64[ns]（旧版缓存自动失效）
TTL_OPEN_RANGE = 6 * 3600        # 含今天/未给 end：6 小时
TTL_CODE_LIST = 7 * 24 * 3600    # 代码表：7 天
MIN30_EARLIEST = "2020-01-01"    # 实测 baostock 30 分钟最早只到 2020-01-01

_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy")
_NOPROXY_KEYS = ("NO_PROXY", "no_proxy")

_ADJUST_TO_BS = {"qfq": "2", "hfq": "1", "none": "3"}   # baostock adjustflag
_ADJUST_TO_AK = {"qfq": "qfq", "hfq": "hfq", "none": ""}

_EX_PREFIX_RE = re.compile(r"^(sh|sz|bj)\s*[.\-_]?\s*(\d{6})$", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"^(\d{6})\s*[.\-_]?\s*(sh|sz|bj)$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d{6}$")

_LOG = logging.getLogger("chan_data")
if not _LOG.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[chan_data] %(levelname)s %(message)s"))
    _LOG.addHandler(_handler)
    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False          # 不污染调用方的 root logger

_BS_LOCK = threading.RLock()
_BS_STATE = {"logged_in": False}


# --------------------------------------------------------------------------- #
# 代理上下文
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _no_proxy_env():
    """取数期间临时摘掉代理变量并置 NO_PROXY=*，退出时逐字恢复。"""
    saved = {}
    for key in _PROXY_KEYS + _NOPROXY_KEYS:
        if key in os.environ:
            saved[key] = os.environ[key]
    try:
        for key in _PROXY_KEYS:
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        yield
    finally:
        for key in _PROXY_KEYS + _NOPROXY_KEYS:
            os.environ.pop(key, None)
        for key, value in saved.items():
            os.environ[key] = value


# --------------------------------------------------------------------------- #
# 代码 / 日期归一化
# --------------------------------------------------------------------------- #
def _normalize_code(code) -> "tuple[str, str]":
    """'600519' / 'sh600519' / '000001.SZ' / 'sz.000001' -> ('600519', 'sh.600519')"""
    if code is None:
        raise ValueError("code 不能为空")
    text = str(code).strip().replace(" ", "")
    if not text:
        raise ValueError("code 不能为空")

    exchange = digits = None
    m = _EX_PREFIX_RE.match(text)
    if m:
        exchange, digits = m.group(1).lower(), m.group(2)
    else:
        m = _SUFFIX_RE.match(text)
        if m:
            digits, exchange = m.group(1), m.group(2).lower()
        elif _DIGITS_RE.match(text):
            digits = text
        else:
            raise ValueError(
                "无法识别的股票代码 %r；支持 '600519' / 'sh600519' / "
                "'000001.SZ' / 'sz.000001'" % (code,))

    if exchange is None:
        exchange = _guess_exchange(digits)
    return digits, "%s.%s" % (exchange, digits)


def _guess_exchange(digits: str) -> str:
    head = digits[0]
    if head in ("6", "9", "5"):         # 沪主板/科创板、沪 B、沪基金
        return "sh"
    if head in "0123":                  # 深市（含深基金/B 股）
        return "sz"
    if head in "48":                    # 北交所
        return "bj"
    raise ValueError("无法从代码 %r 推断交易所" % (digits,))


def _norm_date(value, field: str = "date") -> str:
    """'2020-01-01' / '20200101' / date / datetime -> 'YYYY-MM-DD'"""
    if value is None:
        raise ValueError("%s 不能为空" % field)
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    text = str(value).strip()
    if not text:
        raise ValueError("%s 不能为空" % field)
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        try:
            return datetime.strptime(digits, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    try:
        return pd.Timestamp(text).strftime("%Y-%m-%d")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("无法解析 %s=%r，请用 'YYYY-MM-DD' 或 'YYYYMMDD'"
                         % (field, value)) from exc


def _norm_adjust(adjust) -> str:
    text = str(adjust if adjust is not None else "qfq").strip().lower()
    if text in ("", "none", "no", "raw", "bfq", "不复权"):
        return "none"
    if text in ("qfq", "前复权", "forward"):
        return "qfq"
    if text in ("hfq", "后复权", "backward"):
        return "hfq"
    raise ValueError("adjust 只支持 'qfq' / 'hfq' / 'none'，收到 %r" % (adjust,))


def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 磁盘缓存
# --------------------------------------------------------------------------- #
def _ensure_cache_dir() -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return CACHE_DIR


def _cache_file(kind: str, *parts) -> str:
    name = "_".join([kind] + [str(p) for p in parts if p not in (None, "")])
    name = re.sub(r"[^0-9A-Za-z_.-]+", "_", name)
    return os.path.join(_ensure_cache_dir(), name + ".pkl")


def _cache_load(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as fh:
            payload = pickle.load(fh)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("缓存损坏，忽略并重取：%s (%s: %s)", path, type(exc).__name__, exc)
        with contextlib.suppress(OSError):
            os.remove(path)
        return None

    if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA_VERSION:
        return None
    if payload.get("columns") != COLUMNS:
        _LOG.info("缓存列结构已变更，忽略：%s", path)
        return None
    if not isinstance(payload.get("df"), pd.DataFrame):
        return None
    return payload


def _cache_save(path: str, df: pd.DataFrame, source: str, extra: Optional[dict] = None) -> None:
    payload = {
        "schema": CACHE_SCHEMA_VERSION,
        "columns": COLUMNS,
        "df": df,
        "source": source,
        "fetched_at": time.time(),
        "fetched_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "extra": extra or {},
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("缓存写入失败（不影响本次返回）：%s (%s)", path, exc)
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _cache_is_fresh(payload: dict, end_str: str, ttl_open: int = TTL_OPEN_RANGE) -> bool:
    age = time.time() - (payload.get("fetched_at") or 0)
    if end_str < _today_str():
        return True                  # 历史区间已定型，永久有效
    return age <= ttl_open           # 含今天的区间按 TTL


# --------------------------------------------------------------------------- #
# 结果规整
# --------------------------------------------------------------------------- #
def _finalize(df: pd.DataFrame, intraday: bool = False) -> pd.DataFrame:
    """列固定、类型固定、去重、按时间升序、RangeIndex 0..n-1。"""
    out = df.loc[:, COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out[out["date"].notna()]

    for col in NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    if not intraday:
        out["date"] = out["date"].dt.normalize()

    out = out.drop_duplicates(subset=["date"], keep="last")
    out = out.sort_values("date", kind="mergesort").reset_index(drop=True)

    # 统一到 datetime64[ns]（pandas 3.0 默认给 datetime64[us]）
    with contextlib.suppress(Exception):
        out["date"] = out["date"].astype("datetime64[ns]")

    # 成交额缺失保持 NaN —— 不补 0、不用 volume*close 估算（那是编数据）
    out["volume"] = out["volume"].astype("float64")
    out["amount"] = out["amount"].astype("float64")
    return out


# --------------------------------------------------------------------------- #
# baostock 数据源
# --------------------------------------------------------------------------- #
def _bs_login(bs) -> None:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        lg = bs.login()
    if getattr(lg, "error_code", "-1") != "0":
        raise RuntimeError("baostock 登录失败：%s %s"
                           % (getattr(lg, "error_code", "?"),
                              getattr(lg, "error_msg", "")))
    _BS_STATE["logged_in"] = True


def _bs_query(bs, fields: str, bs_code: str, start: str, end: str,
              frequency: str, adjustflag: str) -> "tuple[list, str]":
    """返回 (rows, error_msg)；error_msg 为空串表示成功。"""
    with _BS_LOCK:
        if not _BS_STATE["logged_in"]:
            _bs_login(bs)
        rs = bs.query_history_k_data_plus(
            bs_code, fields, start_date=start, end_date=end,
            frequency=frequency, adjustflag=adjustflag)
        if rs.error_code != "0":
            # 长连接可能已失效：重登一次再试
            _BS_STATE["logged_in"] = False
            _bs_login(bs)
            rs = bs.query_history_k_data_plus(
                bs_code, fields, start_date=start, end_date=end,
                frequency=frequency, adjustflag=adjustflag)
        if rs.error_code != "0":
            return [], "%s %s" % (rs.error_code, rs.error_msg)

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        return rows, ""


def _fetch_daily_baostock(bs_code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import baostock as bs

    fields = "date,open,high,low,close,volume,amount"
    rows, err = _bs_query(bs, fields, bs_code, start, end, "d", _ADJUST_TO_BS[adjust])
    if err:
        raise RuntimeError("baostock 日线查询失败 %s: %s" % (bs_code, err))
    if not rows:
        raise RuntimeError("baostock 日线 0 行（%s %s~%s adjust=%s；多为代码不存在/"
                           "未上市/区间内无交易日）" % (bs_code, start, end, adjust))
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                     "volume", "amount"])
    return _finalize(df)


def _fetch_min30_baostock(bs_code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import baostock as bs

    fields = "date,time,open,high,low,close,volume,amount"
    rows, err = _bs_query(bs, fields, bs_code, start, end, "30", _ADJUST_TO_BS[adjust])
    if err:
        raise RuntimeError("baostock 30分钟查询失败 %s: %s" % (bs_code, err))
    if not rows:
        raise RuntimeError("baostock 30分钟 0 行（%s %s~%s）" % (bs_code, start, end))

    df = pd.DataFrame(rows, columns=["date", "time", "open", "high", "low", "close",
                                     "volume", "amount"])
    # time 形如 '20240102100000000'（17 位，bar 结束时间）。
    # 坑：不要把日期与时间拼成一个 format 一次解析 —— "%Y-%m-%d %Y%m%d%H%M%S" 里
    # %Y 出现两次，pandas 据此构造正则会抛 "redefinition of group name 'Y'"。
    day = pd.to_datetime(df["date"].astype(str), errors="coerce").dt.normalize()
    clock = pd.to_datetime(df["time"].astype(str).str.slice(0, 14),
                           format="%Y%m%d%H%M%S", errors="coerce")
    df["date"] = day + (clock - clock.dt.normalize())
    return _finalize(df, intraday=True)


# --------------------------------------------------------------------------- #
# akshare 数据源（第二顺位；本机当前不通，保留自愈能力）
# --------------------------------------------------------------------------- #
_AK_COLUMN_MAP = {
    "日期": "date", "时间": "date",
    "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
    "成交量": "volume", "成交额": "amount",
}


def _fetch_daily_akshare(digits: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import akshare as ak

    with _no_proxy_env():
        df = ak.stock_zh_a_hist(
            symbol=digits, period="daily",
            start_date=start.replace("-", ""), end_date=end.replace("-", ""),
            adjust=_ADJUST_TO_AK[adjust])
    if df is None or len(df) == 0:
        raise RuntimeError("akshare 日线返回空（%s %s~%s）" % (digits, start, end))

    df = df.rename(columns=_AK_COLUMN_MAP)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100.0   # 手 -> 股
    return _finalize(df)


def _fetch_min30_akshare(digits: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import akshare as ak

    with _no_proxy_env():
        df = ak.stock_zh_a_hist_min_em(
            symbol=digits, period="30",
            start_date=start + " 09:30:00", end_date=end + " 15:00:00",
            adjust=_ADJUST_TO_AK[adjust])
    if df is None or len(df) == 0:
        raise RuntimeError("akshare 30分钟返回空（%s %s~%s）" % (digits, start, end))

    df = df.rename(columns=_AK_COLUMN_MAP)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100.0
    return _finalize(df, intraday=True)


# --------------------------------------------------------------------------- #
# KHunter 数据源（仅 adjust="none" 的离线兜底）
# --------------------------------------------------------------------------- #
def _khunter_connect() -> sqlite3.Connection:
    if not os.path.exists(KHUNTER_DB):
        raise RuntimeError("KHunter 库不存在：%s" % KHUNTER_DB)
    uri = "file:%s?mode=ro" % KHUNTER_DB.replace("\\", "/")
    return sqlite3.connect(uri, uri=True, timeout=30)


def _fetch_daily_khunter(digits: str, start: str, end: str) -> pd.DataFrame:
    con = _khunter_connect()
    try:
        # 必须带 WHERE + LIMIT，禁止全表扫（库 4.74 GiB）
        rows = con.execute(
            "SELECT date, open, high, low, close, volume FROM stock_kline "
            "WHERE code = ? AND date >= ? AND date <= ? "
            "ORDER BY date ASC LIMIT 20000", (digits, start, end)).fetchall()
    finally:
        con.close()

    if not rows:
        raise RuntimeError("KHunter 无该区间数据（%s %s~%s；该库实测仅覆盖 2023-06-01 起）"
                           % (digits, start, end))
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100.0   # 手 -> 股
    df["amount"] = float("nan")                       # 该库无成交额，留空不估算
    return _finalize(df)


# --------------------------------------------------------------------------- #
# 对外 API
# --------------------------------------------------------------------------- #
def get_daily(code: str, start: str, end: Optional[str] = None,
              adjust: str = "qfq") -> pd.DataFrame:
    """
    日线行情。

    code   : '600519' / 'sh600519' / '000001.SZ' / 'sz.000001'（内部归一化）
    start  : '2020-01-01' 或 '20200101'
    end    : 同上；None = 至今
    adjust : 'qfq'(默认) / 'hfq' / 'none'

    返回列固定 ['date','open','high','low','close','volume','amount']；
    date 为 datetime64 升序，索引 0..n-1；volume 单位股、amount 单位元。
    取不到抛 RuntimeError（消息含各源失败原因）—— 绝不返回编造数据。
    """
    digits, bs_code = _normalize_code(code)
    start_s = _norm_date(start, "start")
    end_s = _norm_date(end, "end") if end is not None else _today_str()
    adjust_n = _norm_adjust(adjust)
    if start_s > end_s:
        raise ValueError("start(%s) 晚于 end(%s)" % (start_s, end_s))

    path = _cache_file("daily", digits, start_s.replace("-", ""),
                       end_s.replace("-", ""), adjust_n)
    cached = _cache_load(path)
    if cached is not None and _cache_is_fresh(cached, end_s):
        _LOG.info("缓存命中 daily %s %s~%s %s（%s 抓取，%d 行）",
                  digits, start_s, end_s, adjust_n,
                  cached.get("fetched_readable", "?"), len(cached["df"]))
        return cached["df"].copy()

    errors = []
    attempts = [
        ("baostock", lambda: _fetch_daily_baostock(bs_code, start_s, end_s, adjust_n)),
        ("akshare", lambda: _fetch_daily_akshare(digits, start_s, end_s, adjust_n)),
    ]
    if adjust_n == "none":
        # 本地库只在"不复权"诉求下才有资格兜底
        attempts.append(("khunter", lambda: _fetch_daily_khunter(digits, start_s, end_s)))

    for source, fn in attempts:
        try:
            df = fn()
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s: %s" % (source, type(exc).__name__, exc))
            _LOG.warning("daily 源 %s 失败，试下一路 —— %s", source, errors[-1])
            continue
        if df is None or len(df) == 0:
            errors.append("%s: 返回空表" % source)
            continue

        if source == "khunter":
            _LOG.warning("降级到 KHunter 本地库：该源不复权、无成交额(amount=NaN)，"
                         "且只覆盖 2023-06-01 之后")
        _LOG.info("daily %s %s~%s %s -> %d 行，来源=%s",
                  digits, start_s, end_s, adjust_n, len(df), source)
        _cache_save(path, df, source,
                    {"start": start_s, "end": end_s, "adjust": adjust_n})
        return df.copy()

    raise RuntimeError(
        "get_daily(%r, %r, %r, adjust=%r) 所有数据源均失败：\n  - %s\n"
        "提示：本机 akshare 走代理与直连都被重置；baostock 需要能访问 10030 端口。"
        % (code, start, end, adjust_n, "\n  - ".join(errors)))


def get_min30(code: str, start: str, end: Optional[str] = None,
              adjust: str = "qfq") -> Optional[pd.DataFrame]:
    """
    30 分钟线。列同 get_daily；date = 该根 K 线结束时间（一天 8 根）。
    实测 baostock 30 分钟数据仅从 2020-01-01 起 —— 更早的 start 会被截断并记日志。
    取不到返回 None 并在日志写明原因，**不抛未处理异常**（上层策略须能降级到日线）。
    """
    try:
        digits, bs_code = _normalize_code(code)
        start_s = _norm_date(start, "start")
        end_s = _norm_date(end, "end") if end is not None else _today_str()
        adjust_n = _norm_adjust(adjust)
        if start_s > end_s:
            _LOG.error("get_min30 参数错误：start(%s) 晚于 end(%s)", start_s, end_s)
            return None
    except Exception as exc:  # noqa: BLE001
        _LOG.error("get_min30 参数错误：%s: %s", type(exc).__name__, exc)
        return None

    eff_start = start_s
    if eff_start < MIN30_EARLIEST:
        _LOG.warning("30 分钟数据实测仅从 %s 起（baostock 限制），start 由 %s 截断为 %s",
                     MIN30_EARLIEST, eff_start, MIN30_EARLIEST)
        eff_start = MIN30_EARLIEST

    path = _cache_file("min30", digits, eff_start.replace("-", ""),
                       end_s.replace("-", ""), adjust_n)
    cached = _cache_load(path)
    if cached is not None and _cache_is_fresh(cached, end_s):
        _LOG.info("缓存命中 min30 %s %s~%s %s（%d 行）",
                  digits, eff_start, end_s, adjust_n, len(cached["df"]))
        return cached["df"].copy()

    attempts = [
        ("baostock", lambda: _fetch_min30_baostock(bs_code, eff_start, end_s, adjust_n)),
        ("akshare", lambda: _fetch_min30_akshare(digits, eff_start, end_s, adjust_n)),
    ]
    errors = []
    for source, fn in attempts:
        try:
            df = fn()
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s: %s" % (source, type(exc).__name__, exc))
            _LOG.warning("min30 源 %s 失败 —— %s", source, errors[-1])
            continue
        if df is None or len(df) == 0:
            errors.append("%s: 返回空表" % source)
            continue

        _LOG.info("min30 %s %s~%s %s -> %d 行，来源=%s",
                  digits, eff_start, end_s, adjust_n, len(df), source)
        _cache_save(path, df, source,
                    {"start": eff_start, "end": end_s, "adjust": adjust_n})
        return df.copy()

    _LOG.error("get_min30(%r, %r, %r) 取不到数据，返回 None。原因：%s",
               code, start, end, " | ".join(errors) or "未知")
    return None


def list_all_codes() -> "list[str]":
    """
    全部 A 股普通股票代码（6 位数字、去重、升序）。

    主源 baostock query_stock_basic() 取 type=='1'（实测该集合正好是 A 股普通股，
    不含 900/200 B 股、不含指数与基金），并只保留 status=='1'（在市）。
    已退市不返回（实测 status=='0' ⇔ outDate 非空，共 341 个）；ST 股保留（仍在交易）。
    失败依次降级：过期磁盘缓存 -> KHunter 本地 stock_basic；全失败才抛 RuntimeError。
    """
    path = _cache_file("codes_all")
    cached = _cache_load(path)
    if cached is not None:
        age = time.time() - (cached.get("fetched_at") or 0)
        if age <= TTL_CODE_LIST:
            codes = cached["df"]["code"].tolist()
            _LOG.info("代码表缓存命中（%s 抓取，%d 只）",
                      cached.get("fetched_readable", "?"), len(codes))
            return codes

    errors = []
    try:
        import baostock as bs

        with _BS_LOCK:
            if not _BS_STATE["logged_in"]:
                _bs_login(bs)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rs = bs.query_stock_basic()
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
        fields = list(rs.fields or [])
        if not rows:
            raise RuntimeError("query_stock_basic 返回 0 行（%s %s）"
                               % (rs.error_code, rs.error_msg))

        basic = pd.DataFrame(rows, columns=fields)
        stocks = basic[basic["type"] == "1"].copy()
        if "status" in stocks.columns:
            stocks = stocks[stocks["status"] == "1"]
        codes = (stocks["code"].astype(str)
                 .str.replace(r"^(sh|sz|bj)\.", "", regex=True)
                 .str.strip())
        codes = sorted({c for c in codes if _DIGITS_RE.match(c)})
        if not codes:
            raise RuntimeError("baostock 代码表过滤后为空")

        _cache_save(path, pd.DataFrame({"code": codes}), "baostock",
                    {"total_rows": len(basic), "type1_in_market": len(stocks)})
        _LOG.info("代码表取自 baostock：%d 只（原始 %d 行）", len(codes), len(basic))
        return codes
    except Exception as exc:  # noqa: BLE001
        errors.append("baostock: %s: %s" % (type(exc).__name__, exc))
        _LOG.warning("代码表 baostock 失败 —— %s", errors[-1])

    if cached is not None:
        codes = cached["df"]["code"].tolist()
        _LOG.warning("代码表降级用过期缓存（%s 抓取，%d 只）",
                     cached.get("fetched_readable", "?"), len(codes))
        return codes

    try:
        con = _khunter_connect()
        try:
            rows = con.execute(
                "SELECT DISTINCT code FROM stock_basic LIMIT 20000").fetchall()
        finally:
            con.close()
        codes = sorted({str(r[0]).strip() for r in rows
                        if _DIGITS_RE.match(str(r[0]).strip())})
        if codes:
            _LOG.warning("代码表降级取自 KHunter stock_basic：%d 只（可能含已退市）",
                         len(codes))
            return codes
        raise RuntimeError("KHunter stock_basic 无可用代码")
    except Exception as exc:  # noqa: BLE001
        errors.append("khunter: %s: %s" % (type(exc).__name__, exc))

    raise RuntimeError("list_all_codes() 所有来源均失败：\n  - %s" % "\n  - ".join(errors))


# --------------------------------------------------------------------------- #
# 兼容旧调用（run_daily.py / chan_backtest.py 在用，不属于本次对外 API）
# --------------------------------------------------------------------------- #
def get_index(code: str = "000001", start: str = "2006-01-01",
              end: Optional[str] = None) -> pd.DataFrame:
    """指数日线（默认上证指数），读本机 KHunter stock_index_kline（不联网）。"""
    text = str(code).strip().lower()
    if "." in text:
        key = text
    else:
        digits = re.sub(r"^(sh|sz|bj)", "", text)
        key = "%s.%s" % ("sh" if digits.startswith("000") else "sz", digits)

    start_s = _norm_date(start, "start")
    end_s = _norm_date(end, "end") if end is not None else _today_str()

    con = _khunter_connect()
    try:
        rows = con.execute(
            "SELECT date, open, high, low, close, volume, amount FROM stock_index_kline "
            "WHERE code = ? AND date >= ? AND date <= ? "
            "ORDER BY date ASC LIMIT 20000", (key, start_s, end_s)).fetchall()
    finally:
        con.close()

    if not rows:
        raise RuntimeError("指数 %s 在 %s~%s 无数据" % (key, start_s, end_s))
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                     "volume", "amount"])
    return _finalize(df)


def code_name(code: str) -> str:
    """本地代码 -> 名称；查不到返回空串（不抛异常，供展示用）。"""
    try:
        digits, _ = _normalize_code(code)
    except Exception:  # noqa: BLE001
        return ""
    try:
        con = _khunter_connect()
        try:
            row = con.execute("SELECT name FROM stock_basic_info WHERE code = ? LIMIT 1",
                              (digits,)).fetchone()
            if row is None:
                row = con.execute("SELECT name FROM stock_basic WHERE code = ? LIMIT 1",
                                  (digits,)).fetchone()
        finally:
            con.close()
        return row[0] if row else ""
    except Exception:  # noqa: BLE001
        return ""
