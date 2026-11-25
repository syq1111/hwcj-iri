#!/usr/bin/env python3
"""兼容旧命令名的用户入口。

``run_pipeline.py run`` 是现场抓包到 IRI 的一键全流程；``process``（或直接
给出 BIN 路径）是已有文件的离线处理。具体实现分别位于 ``one_click`` 和
``cli``，避免维护两套算法。
"""

import sys

from hwcj_iri.cli import main as process_main
from hwcj_iri.one_click import main as one_click_main


def main(argv: list[str] | None = None) -> int:
    """根据第一个参数选择一键现场模式或离线 process 模式。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "run":
        return one_click_main(arguments[1:])
    if arguments and arguments[0] == "process":
        return process_main(arguments[1:])
    if arguments and arguments[0] in {"capture", "extract", "calc"}:
        raise SystemExit(
            "旧 capture/extract/calc 子命令已拆分；请使用 capture_hwcj.py 或 "
            "run_pipeline.py process。"
        )
    # 保留之前的便捷语法：run_pipeline.py <input.bin> --output-dir ...
    return process_main(["process", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
