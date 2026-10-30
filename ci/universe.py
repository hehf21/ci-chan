"""生成股票池：默认取沪深300 成分（按代码排序）前 N 只。

⚠️ 口径说明：沪深300 用的是**当前**成分，回看 2020-2026 会有幸存者偏差
（今天的成分股天然偏向活下来且做大的）。这里只用它检验"信号密度/样本外一致性"，
不作绝对收益结论；要严格无偏，应改用 2020-01-01 时点的成分或全 A 等距抽样。

注意：baostock 自己会往 stdout 打印 "login success!" / "logout success!"，
必须把它重定向掉，否则输出会被污染成多行（2026-10-30 在 CI 上踩过一次）。
"""
from __future__ import annotations

import argparse
import contextlib
import io


def all_codes(limit: int = 0) -> list[str]:
    """全部 A 股代码（主板/创业板/科创板），剔除 ST、退市、B 股。

    用 baostock 的 query_all_stock 取某一交易日的全部标的，
    再用 query_stock_basic 过滤类型。取数较慢（约 1 分钟），只在 plan 阶段跑一次。
    """
    import baostock as bs
    import contextlib
    import io as _io

    buf = _io.StringIO()
    out: list[str] = []
    with contextlib.redirect_stdout(buf):
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(lg.error_msg)
        try:
            rs = bs.query_all_stock(day="2026-10-29")
            codes = []
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                code = str(row[0]).split(".")[-1]
                name = str(row[2]) if len(row) > 2 else ""
                if not (code.isdigit() and len(code) == 6):
                    continue
                if name.startswith(("ST", "*ST")) or "退" in name:
                    continue
                if code.startswith(("200", "900", "51", "15", "16", "58")):
                    continue          # B 股 / 场内基金
                codes.append(code)
            codes = sorted(set(codes))
        finally:
            bs.logout()
    out = codes[:limit] if limit else codes
    return out


def hs300_codes(n: int) -> list[str]:
    import baostock as bs

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(lg.error_msg)
        try:
            rs = bs.query_hs300_stocks()
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()
    codes = sorted({r[1].split(".")[1] for r in rows if len(r) > 1})
    return [c for c in codes if c.isdigit() and len(c) == 6][:n]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="取前多少只（0=不限）")
    ap.add_argument("--all", action="store_true", help="全 A 股（剔除 ST/退市/B股/场内基金）")
    a = ap.parse_args()
    if a.all:
        codes = all_codes(a.n)
    else:
        codes = hs300_codes(a.n)
    print(",".join(codes))
