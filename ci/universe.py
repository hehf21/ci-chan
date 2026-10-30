"""生成股票池：默认取沪深300 成分（按代码排序）前 N 只。

⚠️ 口径说明：沪深300 用的是**当前**成分，回看 2020-2026 会有幸存者偏差
（今天的成分股天然偏向活下来且做大的）。这里只用它检验"信号密度/样本外一致性"，
不作绝对收益结论；要严格无偏，应改用 2020-01-01 时点的成分或全 A 等距抽样。
"""
from __future__ import annotations

import argparse


def hs300_codes(n: int) -> list[str]:
    import baostock as bs

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
    codes = sorted({r[1].split(".")[1] for r in rows})
    return codes[:n]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    a = ap.parse_args()
    print(",".join(hs300_codes(a.n)))
