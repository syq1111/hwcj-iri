#!/usr/bin/env python3
"""用户入口：通过 ADB 抓取 HWCJ-02 原始字节。"""

import sys

from hwcj_iri.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["capture", *sys.argv[1:]]))
