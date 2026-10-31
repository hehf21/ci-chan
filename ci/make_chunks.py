# -*- coding: utf-8 -*-
"""把逗号分隔的代码串切成 JSON 数组（每块 size 只），供 CI 矩阵使用。

用法：
  python ci/universe.py --all --n 0 | python ci/make_chunks.py --size 120
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=120)
    ap.add_argument("--codes", default="", help="逗号分隔；空则从 stdin 读")
    a = ap.parse_args()

    raw = a.codes if a.codes else sys.stdin.read()
    codes = [c.strip() for c in raw.replace("\n", ",").split(",") if c.strip()]
    chunks = [",".join(codes[i:i + a.size]) for i in range(0, len(codes), a.size)]
    print(json.dumps(chunks))
    print(f"{len(codes)} codes -> {len(chunks)} chunks", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
