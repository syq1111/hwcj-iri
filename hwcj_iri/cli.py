"""命令行参数与批处理入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .capture import capture
from .pipeline import PipelineConfig, process_bin_file


def _parse_segments(text: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in text.split(",") if item.strip())
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError("分段必须是逗号分隔的正数，例如 1,5,10,20")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HWCJ-02 正确协议解析与连续 Golden-Car IRI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    process_parser = subparsers.add_parser("process", help="处理一个 BIN 或目录中的全部 BIN")
    process_parser.add_argument("input", type=Path, help="输入 .bin 文件或包含 .bin 的目录")
    process_parser.add_argument("--output-dir", type=Path, required=True, help="输出根目录")
    process_parser.add_argument("--glob", default="*.bin", help="目录模式，默认 *.bin")
    process_parser.add_argument("--step-m", type=float, default=0.1, help="空间采样间隔，默认 0.1 m")
    process_parser.add_argument("--distance-offset-m", type=float, default=0.0)
    process_parser.add_argument("--scale-mm-per-count", type=float, default=0.05)
    process_parser.add_argument("--zero-offset-count", type=float, default=0.0)
    process_parser.add_argument(
        "--highpass-wavelength-m",
        type=float,
        default=100.0,
        help="项目级空间高通截止波长，默认 100 m",
    )
    process_parser.add_argument(
        "--no-highpass", action="store_true", help="禁用项目级 100 m 空间高通（用于敏感性分析）"
    )
    process_parser.add_argument("--segments", type=_parse_segments, default=(1.0, 5.0, 10.0, 20.0))
    process_parser.add_argument("--minimum-fraction", type=float, default=0.95)
    process_parser.add_argument("--no-plot", action="store_true")
    process_parser.add_argument(
        "--allow-corrupt", action="store_true", help="允许坏帧继续（仅诊断，不建议生成正式标签）"
    )
    process_parser.add_argument(
        "--gap-policy",
        choices=("error", "linear"),
        default="error",
        help="缺包处理：默认 error；linear 仅供显式诊断性线性补缺",
    )

    capture_parser = subparsers.add_parser("capture", help="通过 ADB 抓取 HWCJ-02 原始 TCP 字节")
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--adb", help="adb/adb.exe 的完整路径")
    capture_parser.add_argument("--collector-ip", default="10.10.100.254")
    capture_parser.add_argument("--collector-port", type=int, default=8899)
    capture_parser.add_argument("--busybox", default="/data/local/tmp/busybox")
    capture_parser.add_argument("--duration-s", type=float)
    return parser


def _input_files(path: Path, pattern: str) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.glob(pattern))
    raise FileNotFoundError(path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "capture":
        result = capture(
            output_path=args.output,
            adb_path=args.adb,
            collector_ip=args.collector_ip,
            collector_port=args.collector_port,
            remote_busybox=args.busybox,
            duration_s=args.duration_s,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    config = PipelineConfig(
        sample_spacing_m=args.step_m,
        distance_offset_m=args.distance_offset_m,
        scale_mm_per_count=args.scale_mm_per_count,
        zero_offset_count=args.zero_offset_count,
        highpass_wavelength_m=None if args.no_highpass else args.highpass_wavelength_m,
        gap_policy=args.gap_policy,
        segment_lengths_m=args.segments,
        minimum_segment_fraction=args.minimum_fraction,
        strict_protocol=not args.allow_corrupt,
        make_plot=not args.no_plot,
    )
    inputs = _input_files(args.input, args.glob)
    if not inputs:
        raise FileNotFoundError(f"没有匹配的 BIN: {args.input} / {args.glob}")

    rows = []
    for input_path in inputs:
        print(f"处理: {input_path}", flush=True)
        manifest = process_bin_file(input_path, args.output_dir, config=config)
        rows.append(
            {
                "source_bin": str(input_path.resolve()),
                "sha256": manifest["protocol_audit"]["source_sha256"],
                "n_samples": manifest["profile_summary"]["n_samples"],
                "distance_end_m": manifest["profile_summary"]["relative_distance_end_m"],
                "full_length_iri_m_per_km": manifest["full_length_iri_m_per_km"],
                "output_directory": manifest["output_directory"],
            }
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(args.output_dir / "batch_manifest.csv", index=False)
    print(f"完成：{len(rows)} 个 BIN；汇总见 {args.output_dir / 'batch_manifest.csv'}")
    return 0
