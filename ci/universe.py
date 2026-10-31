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


def all_codes(limit: int = 0, seed: int = 20261030) -> list[str]:
    """全部 A 股代码（剔除 ST、退市、B 股、场内基金）。

    只用 query_all_stock 一次请求：它自带 tradeStatus（1=正常交易）和名称，
    直接按这两个字段过滤即可，**不要**逐票再查 query_stock_basic——
    5000 多个代码逐个查会把 plan 阶段拖成几小时（2026-10-30 实测卡死 1 小时）。
    """
    import baostock as bs
    import contextlib
    import io as _io

    buf = _io.StringIO()
    codes: list[str] = []
    with contextlib.redirect_stdout(buf):
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(lg.error_msg)
        try:
            rs = bs.query_all_stock(day="2026-10-29")
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                if len(row) < 3:
                    continue
                raw_code, trade_status, name = str(row[0]), str(row[1]), str(row[2])
                code = raw_code.split(".")[-1]
                if not (code.isdigit() and len(code) == 6):
                    continue
                if trade_status != "1":            # 停牌/退市
                    continue
                if name.startswith(("ST", "*ST", "PT")) or "退" in name:
                    continue
                if code.startswith(("200", "900", "51", "15", "16", "58")):
                    continue                        # B 股 / 场内基金
                codes.append(code)
        finally:
            bs.logout()
    codes = sorted(set(codes))
    if limit and limit < len(codes):
        # 按代码顺序取前 N 只会拿到一堆深市老票（000001~0000xx），样本有偏。
        # 用固定种子随机抽样，保证可复现且覆盖全市场。
        import random as _random
        return sorted(_random.Random(int(seed)).sample(codes, limit))
    return codes


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
    ap.add_argument("--seed", type=int, default=20261030, help="随机抽样种子（换种子=换股票池）")
    a = ap.parse_args()
    if a.all:
        codes = all_codes(a.n, seed=a.seed)
    else:
        codes = hs300_codes(a.n)
    print(",".join(codes))
