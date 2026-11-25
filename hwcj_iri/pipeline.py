"""从 HWCJ-02 BIN 到连续 Golden-Car 分段 IRI 的完整流水线。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import scipy
import sys
import tempfile

import numpy as np
import pandas as pd

from .golden_car import (
    GOLDEN_CAR_PARAMETERS,
    continuous_golden_car,
    full_length_iri,
    summarize_response,
)
from .profile import build_profile
from .protocol import audit_as_dict, parse_capture_file, sha256_file


@dataclass(frozen=True)
class PipelineConfig:
    """所有会改变数值结果的参数；每次运行都会写入 manifest。"""

    sample_spacing_m: float = 0.1
    distance_offset_m: float = 0.0
    scale_mm_per_count: float = 0.05
    zero_offset_count: float = 0.0
    highpass_wavelength_m: float | None = 100.0
    gap_policy: str = "error"
    segment_lengths_m: tuple[float, ...] = (1.0, 5.0, 10.0, 20.0)
    minimum_segment_fraction: float = 0.95
    strict_protocol: bool = True
    make_plot: bool = True


def _safe_scale_name(scale_m: float) -> str:
    return str(int(scale_m)) if float(scale_m).is_integer() else str(scale_m).replace(".", "p")


def _plot_overview(
    profile: pd.DataFrame,
    segment_tables: dict[float, pd.DataFrame],
    output_path: Path,
) -> None:
    """生成原始 ADC、IRI 输入剖面和多尺度 IRI 的检查图。"""
    os.environ.setdefault(
        "MPLCONFIGDIR",
        str(Path(tempfile.gettempdir()) / "hwcj_iri_matplotlib_cache"),
    )
    try:
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError as exc:  # pragma: no cover - 仅最小安装环境触发
        raise RuntimeError("生成图需要 matplotlib；可使用 --no-plot 跳过") from exc

    bundled_font = Path(__file__).resolve().parents[1] / "assets" / "NotoSansCJKsc-Regular.otf"
    if bundled_font.exists():
        font_manager.fontManager.addfont(str(bundled_font))
        plt.rcParams["font.family"] = font_manager.FontProperties(
            fname=str(bundled_font)
        ).get_name()
    plt.rcParams["axes.unicode_minus"] = False

    fig, axes = plt.subplots(3, 1, figsize=(14, 9), sharex=False)
    axes[0].plot(
        profile.distance_m,
        profile.raw_adc_count_observed,
        lw=0.7,
        color="#2455A4",
    )
    axes[0].set_title("HWCJ-02 正确解码的原始 ADC 空间序列")
    axes[0].set_ylabel("ADC count")
    axes[0].grid(alpha=0.25)

    axes[1].plot(
        profile.distance_m,
        profile.relative_profile_mm,
        lw=0.55,
        alpha=0.55,
        label="相对数采位移",
    )
    axes[1].plot(
        profile.distance_m,
        profile.profile_for_iri_mm,
        lw=0.8,
        label="送入 Golden-Car 的剖面",
    )
    axes[1].set_title("剖面预处理（单位换算、去常数零点及可选空间高通）")
    axes[1].set_ylabel("mm")
    axes[1].legend()
    axes[1].grid(alpha=0.25)

    for scale_m, table in segment_tables.items():
        if table.empty:
            continue
        axes[2].step(
            table.seg_start_m,
            table.iri_m_per_km,
            where="post",
            lw=0.9,
            label=f"{scale_m:g} m",
        )
    axes[2].set_title("同一连续 Golden-Car 响应的多尺度空间汇总")
    axes[2].set_xlabel("相对距离 (m)")
    axes[2].set_ylabel("IRI (m/km)")
    axes[2].legend(ncol=4)
    axes[2].grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def process_bin_file(
    bin_path: str | Path,
    output_root: str | Path,
    config: PipelineConfig = PipelineConfig(),
) -> dict[str, object]:
    """执行并落盘完整流程，返回与 ``run_manifest.json`` 相同的字典。"""
    source = Path(bin_path).expanduser().resolve()
    output_dir = Path(output_root).expanduser().resolve() / source.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. 按长度与校验和解析所有帧，再从 A3 测量帧解出大端 ADC。
    frames, samples, protocol_audit = parse_capture_file(
        source,
        sample_spacing_m=config.sample_spacing_m,
        distance_offset_m=config.distance_offset_m,
        strict=config.strict_protocol,
    )
    frames.to_csv(output_dir / "01_protocol_frames.csv", index=False)
    samples.to_csv(output_dir / "02_raw_samples.csv", index=False)

    # 2. ADC count → mm；去常数零点，并按配置执行项目级空间高通。
    profile = build_profile(
        samples,
        scale_mm_per_count=config.scale_mm_per_count,
        zero_offset_count=config.zero_offset_count,
        highpass_wavelength_m=config.highpass_wavelength_m,
        gap_policy=config.gap_policy,
    )
    profile.to_csv(output_dir / "03_profile_for_iri.csv", index=False)

    # 3. 完整剖面只运行一次 Golden-Car；保存所有状态便于复核。
    response, golden_metadata = continuous_golden_car(
        profile.distance_m.to_numpy(float),
        profile.profile_for_iri_mm.to_numpy(float) / 1000.0,
        parameters=GOLDEN_CAR_PARAMETERS,
        apply_tire_enveloping=True,
    )
    response.to_csv(output_dir / "04_continuous_golden_car_response.csv", index=False)

    # 4. 从同一个累计悬架运动曲线按不同空间边界做加法汇总。
    segment_tables: dict[float, pd.DataFrame] = {}
    scale_summaries = []
    for scale_m in config.segment_lengths_m:
        table = summarize_response(
            response,
            segment_length_m=scale_m,
            minimum_fraction=config.minimum_segment_fraction,
        )
        segment_tables[scale_m] = table
        scale_name = _safe_scale_name(scale_m)
        table.to_csv(output_dir / f"05_iri_{scale_name}m.csv", index=False)
        scale_summaries.append(
            {
                "segment_length_m": scale_m,
                "n_segments": int(len(table)),
                "iri_length_weighted_mean_m_per_km": (
                    float(np.average(table.iri_m_per_km, weights=table.length_m))
                    if not table.empty
                    else None
                ),
                "iri_min_m_per_km": float(table.iri_m_per_km.min()) if not table.empty else None,
                "iri_max_m_per_km": float(table.iri_m_per_km.max()) if not table.empty else None,
            }
        )

    if config.make_plot:
        _plot_overview(profile, segment_tables, output_dir / "06_overview.png")

    interpolated_count = int(profile.is_interpolated.sum())
    has_capture_warning = any(
        [
            protocol_audit.invalid_checksum_candidate_count,
            protocol_audit.truncated_candidate_count,
            protocol_audit.unframed_byte_count,
            protocol_audit.inferred_missing_packet_count,
            protocol_audit.counter_transition_unexpected_count,
        ]
    )
    has_profile_rail_warning = bool(
        protocol_audit.adc_equal_zero_count
        or protocol_audit.adc_equal_4000_count
    )
    manifest: dict[str, object] = {
        "status": (
            "diagnostic_gap_repaired_project_iri_estimate"
            if has_capture_warning or interpolated_count
            else (
                "protocol_corrected_profile_rail_warning_project_iri_estimate"
                if has_profile_rail_warning
                else "protocol_and_golden_car_corrected_project_iri_estimate"
            )
        ),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_bin": str(source),
        "output_directory": str(output_dir),
        "pipeline_config": asdict(config),
        "protocol_audit": audit_as_dict(protocol_audit),
        "profile_summary": {
            "n_samples": int(len(profile)),
            "n_observed_samples": int((~profile.is_interpolated).sum()),
            "n_interpolated_samples": interpolated_count,
            "relative_distance_start_m": float(profile.distance_m.iloc[0]),
            "relative_distance_end_m": float(profile.distance_m.iloc[-1]),
            "raw_adc_observed_min": int(profile.raw_adc_count_observed.min()),
            "raw_adc_observed_max": int(profile.raw_adc_count_observed.max()),
            "profile_for_iri_std_mm": float(profile.profile_for_iri_mm.std(ddof=0)),
        },
        "golden_car": golden_metadata,
        "full_length_iri_m_per_km": full_length_iri(response),
        "segment_summaries": scale_summaries,
        "software": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scipy": scipy.__version__,
        },
        "implementation_sha256": {
            path.name: sha256_file(path)
            for path in sorted(Path(__file__).resolve().parent.glob("*.py"))
        },
        "claim_boundary": [
            "帧结构、字节序和校验和已经由现有 BIN 文件内部一致性验证",
            "0.05 mm/count 仍需厂家可追溯标定资料确认",
            "未知 meta 字节未参与距离或 IRI 计算",
            "counter 只用于缺包审计和保持相对位置，未当作绝对里程",
            "若发生线性补缺，结果只用于敏感性诊断，不是正式参考标签",
            "精确命中 0/4000 的样本提示可能限幅；带 rail_warning 的剖面不宜作定量真值",
            "尚未与 ProVAL/认证实现及标准标定路段完成绝对数值交叉验证",
            "因此输出是纠正后的项目 IRI 估计，不等同于认证绝对 IRI",
        ],
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest
