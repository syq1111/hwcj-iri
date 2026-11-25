"""ADC 空间序列到 Golden-Car 输入剖面的预处理。"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import signal


def spatial_highpass(
    values: np.ndarray,
    sample_spacing_m: float,
    cutoff_wavelength_m: float,
    order: int = 2,
) -> np.ndarray:
    """用零相位 Butterworth 空间高通去除长波基线。

    截止波长 ``cutoff_wavelength_m`` 对应空间频率 ``1 / wavelength``，单位
    cycles/m。该步骤是本项目八轮仪剖面预处理假设，不属于 Golden-Car 四个
    车辆参数本身；所有输出 manifest 都会记录是否启用。
    """
    if sample_spacing_m <= 0 or cutoff_wavelength_m <= 0:
        raise ValueError("sample_spacing_m 与 cutoff_wavelength_m 必须大于 0")
    values = np.asarray(values, dtype=float)
    sample_frequency_cpm = 1.0 / sample_spacing_m
    cutoff_cpm = 1.0 / cutoff_wavelength_m
    nyquist_cpm = sample_frequency_cpm / 2.0
    if cutoff_cpm >= nyquist_cpm:
        raise ValueError("高通截止频率必须低于空间 Nyquist 频率")
    sos = signal.butter(
        order,
        cutoff_cpm,
        btype="highpass",
        fs=sample_frequency_cpm,
        output="sos",
    )
    return signal.sosfiltfilt(sos, values)


def build_profile(
    samples: pd.DataFrame,
    scale_mm_per_count: float = 0.05,
    zero_offset_count: float = 0.0,
    highpass_wavelength_m: float | None = 100.0,
    gap_policy: str = "error",
) -> pd.DataFrame:
    """生成带单位且可追溯的剖面表。

    ``sensor_displacement_mm`` 是按给定传感器比例换算的数采位移；当前尚缺
    厂家可追溯标定记录，因此不能把它无条件称为认证纵断面高程。
    ``relative_profile_mm`` 去掉常数零点。Golden-Car 对常数高程平移不敏感。
    ``profile_for_iri_mm`` 是实际送入 Golden-Car 的列。
    """
    required = {"distance_m", "raw_adc_count"}
    missing = required - set(samples.columns)
    if missing:
        raise ValueError(f"samples 缺少列: {sorted(missing)}")
    if scale_mm_per_count <= 0:
        raise ValueError("scale_mm_per_count 必须大于 0")
    if len(samples) < 3:
        raise ValueError("至少需要 3 个剖面样本")

    distance = samples.distance_m.to_numpy(float)
    increments = np.diff(distance)
    if np.any(increments <= 0):
        raise ValueError("distance_m 必须严格递增")
    spacing = float(np.median(increments))
    if not np.allclose(increments, spacing, rtol=1e-6, atol=1e-9):
        raise ValueError("当前 HWCJ 管线要求等空间间隔数据")

    if gap_policy not in {"error", "linear"}:
        raise ValueError("gap_policy 必须是 'error' 或 'linear'")
    raw_adc_observed = samples.raw_adc_count.to_numpy(float)
    missing = np.isnan(raw_adc_observed)
    if missing.any() and gap_policy == "error":
        raise ValueError(
            f"原始序列含 {int(missing.sum())} 个推断缺失样本；正式计算默认拒绝。"
            "仅诊断时可明确选择 gap_policy='linear'。"
        )
    if missing.any():
        raw_adc_used = (
            pd.Series(raw_adc_observed)
            .interpolate(method="linear", limit_area="inside")
            .to_numpy(float)
        )
        if np.isnan(raw_adc_used).any():
            raise ValueError("序列首尾存在缺样，不能安全地进行线性内插")
    else:
        raw_adc_used = raw_adc_observed.copy()

    displacement_mm = (raw_adc_used - zero_offset_count) * scale_mm_per_count
    relative_mm = displacement_mm - displacement_mm[0]
    if highpass_wavelength_m is None:
        profile_for_iri_mm = relative_mm.copy()
    else:
        profile_for_iri_mm = spatial_highpass(
            relative_mm,
            sample_spacing_m=spacing,
            cutoff_wavelength_m=highpass_wavelength_m,
        )

    return pd.DataFrame(
        {
            "sample_index": samples.sample_index.to_numpy(int),
            "distance_m": distance,
            "raw_adc_count_observed": raw_adc_observed,
            "raw_adc_count_used": raw_adc_used,
            "is_interpolated": missing,
            "sensor_displacement_mm": displacement_mm,
            "relative_profile_mm": relative_mm,
            "profile_for_iri_mm": profile_for_iri_mm,
        }
    )
