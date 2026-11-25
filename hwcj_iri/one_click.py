"""HWCJ-02 一键全流程：现场抓包或导入 BIN，然后严格计算 IRI。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from .capture import capture
from .cli import _parse_segments
from .pipeline import PipelineConfig, process_bin_file
from .protocol import sha256_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "一键完成 HWCJ-02 原始字节获取、协议审计、剖面处理、"
            "连续 Golden-Car 和多尺度 IRI"
        )
    )
    parser.add_argument(
        "--input-bin",
        type=Path,
        help="离线模式：处理已有 BIN；不写此参数即进入现场抓包模式",
    )
    parser.add_argument(
        "--name",
        help="本次测试名；默认使用 input 文件名或 test_年月日_时分秒",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "one_click_runs",
        help="全部会话的根目录；默认是工具目录下的 one_click_runs",
    )

    # 现场抓包参数；离线模式会忽略这些参数。
    parser.add_argument("--adb", help="adb/adb.exe 的完整路径；若已在 PATH 可不写")
    parser.add_argument("--collector-ip", default="10.10.100.254")
    parser.add_argument("--collector-port", type=int, default=8899)
    parser.add_argument("--busybox", default="/data/local/tmp/busybox")
    parser.add_argument(
        "--duration-s",
        type=float,
        help="抓包秒数；不写则由用户按 Ctrl+C 停止",
    )

    # 所有会改变 IRI 数值或空间边界的参数都显式暴露，并写入 summary/manifest。
    parser.add_argument("--step-m", type=float, default=0.1)
    parser.add_argument("--distance-offset-m", type=float, default=0.0)
    parser.add_argument("--scale-mm-per-count", type=float, default=0.05)
    parser.add_argument("--zero-offset-count", type=float, default=0.0)
    parser.add_argument("--highpass-wavelength-m", type=float, default=100.0)
    parser.add_argument("--no-highpass", action="store_true")
    parser.add_argument(
        "--segments",
        type=_parse_segments,
        default=(1.0, 5.0, 10.0, 20.0),
    )
    parser.add_argument("--minimum-fraction", type=float, default=0.95)
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument(
        "--diagnostic-linear-gaps",
        action="store_true",
        help=(
            "仅用于坏抓包诊断：允许协议告警并线性补内部缺样；"
            "输出不会被标为正式无缺陷结果"
        ),
    )
    return parser


def _default_name(input_bin: Path | None) -> str:
    if input_bin is not None:
        return f"{input_bin.stem}_{datetime.now():%Y%m%d_%H%M%S}"
    return f"test_{datetime.now():%Y%m%d_%H%M%S}"


def _validate_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("测试名不能为空")
    if Path(cleaned).name != cleaned or "/" in cleaned or "\\" in cleaned:
        raise ValueError("测试名只能是一个目录名，不能包含 / 或 \\")
    if cleaned in {".", ".."}:
        raise ValueError("测试名不能是 . 或 ..")
    return cleaned


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_bin = args.input_bin.expanduser().resolve() if args.input_bin else None
    if input_bin is not None and not input_bin.is_file():
        raise FileNotFoundError(input_bin)

    name = _validate_name(args.name or _default_name(input_bin))
    output_root = args.output_root.expanduser().resolve()
    session_root = output_root / name
    raw_dir = session_root / "01_raw"
    result_root = session_root / "02_results"
    summary_path = session_root / "pipeline_summary.json"

    # 不覆盖已完成会话或已经包含 BIN 的失败会话。上一次若在真正抓包前因
    # ADB 配置失败，只留下一个 status=failed 且没有 BIN 的空会话，则允许
    # 原命令安全重试，避免用户必须手动清理目录。
    previous_summary: dict[str, object] | None = None
    if session_root.exists():
        if not session_root.is_dir():
            print(f"会话路径不是目录，无法使用：{session_root}", flush=True)
            return 2
        if summary_path.exists():
            try:
                previous_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous_summary = None
        existing_bins = list(raw_dir.glob("*.bin")) if raw_dir.exists() else []
        existing_files = [item for item in session_root.rglob("*") if item.is_file()]
        stale_empty_session = previous_summary is None and not existing_files
        if not (
            previous_summary is not None
            and previous_summary.get("status") in {"failed", "running"}
            and not existing_bins
        ) and not stale_empty_session:
            print(
                "同名会话已存在，为避免覆盖原始数据和结果，请更换 --name："
                f"{session_root}",
                flush=True,
            )
            return 2
        print("发现上一次未抓到BIN的残留会话，正在安全重试。", flush=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        result_root.mkdir(parents=True, exist_ok=True)
    else:
        session_root.mkdir(parents=True, exist_ok=False)
        raw_dir.mkdir()
        result_root.mkdir()

    mode = "offline_existing_bin" if input_bin is not None else "live_capture"
    config = PipelineConfig(
        sample_spacing_m=args.step_m,
        distance_offset_m=args.distance_offset_m,
        scale_mm_per_count=args.scale_mm_per_count,
        zero_offset_count=args.zero_offset_count,
        highpass_wavelength_m=(
            None if args.no_highpass else args.highpass_wavelength_m
        ),
        gap_policy=("linear" if args.diagnostic_linear_gaps else "error"),
        segment_lengths_m=args.segments,
        minimum_segment_fraction=args.minimum_fraction,
        strict_protocol=not args.diagnostic_linear_gaps,
        make_plot=not args.no_plot,
    )
    summary: dict[str, object] = {
        "status": "running",
        "mode": mode,
        "name": name,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "session_root": str(session_root),
        "pipeline_config": asdict(config),
        "steps": [],
    }
    if previous_summary is not None:
        summary["previous_failed_attempt"] = previous_summary
    _write_json(summary_path, summary)

    try:
        if input_bin is None:
            raw_bin = raw_dir / f"{name}.bin"
            print("\n[1/2] 现场抓取原始 TCP 字节", flush=True)
            capture_result = capture(
                output_path=raw_bin,
                adb_path=args.adb,
                collector_ip=args.collector_ip,
                collector_port=args.collector_port,
                remote_busybox=args.busybox,
                duration_s=args.duration_s,
            )
            summary["capture"] = capture_result
            summary["steps"].append("live_capture_completed")
        else:
            raw_bin = raw_dir / input_bin.name
            print("\n[1/2] 复制并校验已有 BIN", flush=True)
            shutil.copy2(input_bin, raw_bin)
            original_hash = sha256_file(input_bin)
            copied_hash = sha256_file(raw_bin)
            if original_hash != copied_hash:
                raise RuntimeError("BIN 副本 SHA-256 与原件不一致")
            source_copy = {
                "original_path": str(input_bin),
                "copied_path": str(raw_bin),
                "sha256": copied_hash,
                "exact_copy": True,
            }
            _write_json(raw_dir / "source_copy_manifest.json", source_copy)
            summary["source_copy"] = source_copy
            summary["steps"].append("existing_bin_copied_and_sha256_verified")

        print("\n[2/2] 协议解析、剖面处理与连续 Golden-Car IRI", flush=True)
        run_manifest = process_bin_file(raw_bin, result_root, config=config)
        summary["steps"].append("iri_pipeline_completed")
        summary["run_manifest"] = str(
            result_root / raw_bin.stem / "run_manifest.json"
        )
        summary["result_status"] = run_manifest["status"]
        summary["full_length_iri_m_per_km"] = run_manifest[
            "full_length_iri_m_per_km"
        ]
        summary["protocol_audit"] = run_manifest["protocol_audit"]
        summary["status"] = "completed"
        summary["completed_utc"] = datetime.now(timezone.utc).isoformat()
        _write_json(summary_path, summary)

        print("\n一键流程完成", flush=True)
        print(f"  会话目录：{session_root}", flush=True)
        print(f"  总清单：  {summary_path}", flush=True)
        print(f"  结果状态：{run_manifest['status']}", flush=True)
        print(
            f"  全长 IRI：{run_manifest['full_length_iri_m_per_km']:.6f} m/km",
            flush=True,
        )
        if "rail_warning" in str(run_manifest["status"]):
            print("  警告：检测到精确 0/4000 值，剖面可能限幅。", flush=True)
        if str(run_manifest["status"]).startswith("diagnostic_"):
            print("  警告：这是缺包/坏帧诊断结果，不能作为正式无缺陷标签。", flush=True)
        return 0
    except Exception as exc:
        summary["status"] = "failed"
        summary["failed_utc"] = datetime.now(timezone.utc).isoformat()
        summary["error_type"] = type(exc).__name__
        summary["error_message"] = str(exc)
        _write_json(summary_path, summary)
        print("\n一键流程失败；原始 BIN（若已抓取）和失败清单均已保留。", flush=True)
        print(f"  原因：{type(exc).__name__}: {exc}", flush=True)
        print(f"  失败清单：{summary_path}", flush=True)
        return 2
