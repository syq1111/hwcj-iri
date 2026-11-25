"""连续 Golden-Car 响应与空间分段 IRI。

模型参数与计算步骤：

* 归一化参数 K1=653、K2=63.3、C=6、mu/ms=0.15；
* 80 km/h 模拟速度；
* 以最初约 11 m 的平均坡度初始化两质量块速度；
* 采样间隔不大于 0.125 m 时应用 0.25 m 中心箱式轮胎包络；
* 整条剖面只运行一次状态方程，再由累计悬架运动汇总各空间段。

"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy.signal import lsim


@dataclass(frozen=True)
class GoldenCarParameters:
    """按簧上质量归一化的 Golden-Car 参数。"""

    tire_stiffness_k1_per_s2: float = 653.0
    suspension_stiffness_k2_per_s2: float = 63.3
    damping_c_per_s: float = 6.0
    unsprung_to_sprung_mass_ratio: float = 0.15
    simulation_speed_kmh: float = 80.0
    initialization_distance_m: float = 11.0
    tire_enveloping_base_m: float = 0.25


GOLDEN_CAR_PARAMETERS = GoldenCarParameters()


def tire_enveloping_box_filter(
    distance_m: np.ndarray,
    elevation_m: np.ndarray,
    base_length_m: float = 0.25,
) -> np.ndarray:
    """计算中心 0.25 m 箱式平均，模拟轮胎包络/跨越小尺度尖峰。"""
    distance = np.asarray(distance_m, dtype=float)
    elevation = np.asarray(elevation_m, dtype=float)
    half_base = base_length_m / 2.0
    # 用左右边界和前缀和计算，与逐点求均值完全同义，但由 O(n²) 降为
    # O(n log n)，长距离 BIN 不会因轮胎包络步骤而显著变慢。
    left = np.searchsorted(distance, distance - half_base - 1e-12, side="left")
    right = np.searchsorted(distance, distance + half_base + 1e-12, side="right")
    prefix = np.r_[0.0, np.cumsum(elevation)]
    return (prefix[right] - prefix[left]) / (right - left)


def continuous_golden_car(
    distance_m: np.ndarray,
    elevation_m: np.ndarray,
    parameters: GoldenCarParameters = GOLDEN_CAR_PARAMETERS,
    apply_tire_enveloping: bool = True,
) -> tuple[pd.DataFrame, dict[str, float | bool]]:
    """在完整等距剖面上运行一次连续四分之一车模型。

    返回逐点状态/累计悬架运动，以及本次实际采用的初始化和滤波元数据。
    IRI 的积分对象为簧上与簧下质量的相对垂向速度绝对值。
    """
    distance = np.asarray(distance_m, dtype=float)
    elevation = np.asarray(elevation_m, dtype=float)
    if distance.ndim != 1 or elevation.shape != distance.shape or len(distance) < 3:
        raise ValueError("distance_m 与 elevation_m 必须是等长的一维数组")
    if not np.all(np.isfinite(distance)) or not np.all(np.isfinite(elevation)):
        raise ValueError("剖面不能包含 NaN 或无穷值")
    increments = np.diff(distance)
    spacing = float(np.median(increments))
    if np.any(increments <= 0) or not np.allclose(
        increments, spacing, rtol=1e-5, atol=1e-9
    ):
        raise ValueError("当前实现要求严格递增、等空间间隔剖面")

    filtered = elevation.copy()
    tire_filter_used = bool(apply_tire_enveloping and spacing <= 0.125)
    if tire_filter_used:
        filtered = tire_enveloping_box_filter(
            distance,
            elevation,
            base_length_m=parameters.tire_enveloping_base_m,
        )

    speed_m_per_s = parameters.simulation_speed_kmh / 3.6
    time_s = (distance - distance[0]) / speed_m_per_s
    k1 = parameters.tire_stiffness_k1_per_s2
    k2 = parameters.suspension_stiffness_k2_per_s2
    damping = parameters.damping_c_per_s
    mass_ratio = parameters.unsprung_to_sprung_mass_ratio

    # 状态顺序为 [簧上位移, 簧上速度, 簧下位移, 簧下速度]。
    system_a = np.array(
        [
            [0.0, 1.0, 0.0, 0.0],
            [-k2, -damping, k2, damping],
            [0.0, 0.0, 0.0, 1.0],
            [k2 / mass_ratio, damping / mass_ratio, -(k1 + k2) / mass_ratio, -damping / mass_ratio],
        ]
    )
    system_b = np.array([[0.0], [0.0], [0.0], [k1 / mass_ratio]])
    system_c = np.eye(4)
    system_d = np.zeros((4, 1))

    # 11 m 对应 Golden-Car 在 80 km/h 下约 0.5 s。若剖面不足 11 m，则使用
    # 能取得的最远点；两质量块从同一高程和同一坡度速度开始。
    lead_index = min(
        int(round(parameters.initialization_distance_m / spacing)),
        len(distance) - 1,
    )
    initialization_span_m = float(distance[lead_index] - distance[0])
    initial_slope = float(
        (filtered[lead_index] - filtered[0]) / initialization_span_m
    )
    initial_velocity_m_per_s = speed_m_per_s * initial_slope
    initial_state = np.array(
        [filtered[0], initial_velocity_m_per_s, filtered[0], initial_velocity_m_per_s]
    )

    _, state, _ = lsim(
        (system_a, system_b, system_c, system_d),
        U=filtered,
        T=time_s,
        X0=initial_state,
        interp=True,
    )
    relative_velocity_abs = np.abs(state[:, 1] - state[:, 3])
    incremental_motion_m = np.r_[0.0, relative_velocity_abs[1:] * np.diff(time_s)]
    cumulative_motion_m = np.cumsum(incremental_motion_m)

    response = pd.DataFrame(
        {
            "distance_m": distance,
            "time_at_80kmh_s": time_s,
            "profile_input_m": elevation,
            "profile_tire_enveloped_m": filtered,
            "sprung_displacement_m": state[:, 0],
            "sprung_velocity_m_per_s": state[:, 1],
            "unsprung_displacement_m": state[:, 2],
            "unsprung_velocity_m_per_s": state[:, 3],
            "abs_relative_velocity_m_per_s": relative_velocity_abs,
            "incremental_suspension_motion_m": incremental_motion_m,
            "cumulative_suspension_motion_m": cumulative_motion_m,
        }
    )
    metadata: dict[str, float | bool] = {
        **asdict(parameters),
        "sample_spacing_m": spacing,
        "tire_enveloping_applied": tire_filter_used,
        "initialization_span_m": initialization_span_m,
        "initial_slope_m_per_m": initial_slope,
        "initial_velocity_m_per_s": initial_velocity_m_per_s,
    }
    return response, metadata


def summarize_response(
    response: pd.DataFrame,
    segment_length_m: float,
    minimum_fraction: float = 0.95,
) -> pd.DataFrame:
    """把已连续运行的累计响应汇总为不重叠空间段 IRI。

    每段采用半开区间语义 ``[seg_start, seg_end)``；数值由边界处累计悬架
    运动差除以实际长度得到。末段仅在达到完整段长的 ``minimum_fraction``
    时保留，默认 95%，不会像旧代码那样保留仅 30% 的残段。
    """
    if segment_length_m <= 0:
        raise ValueError("segment_length_m 必须大于 0")
    if not 0 < minimum_fraction <= 1:
        raise ValueError("minimum_fraction 必须位于 (0, 1]")
    required = {"distance_m", "cumulative_suspension_motion_m"}
    missing = required - set(response.columns)
    if missing:
        raise ValueError(f"response 缺少列: {sorted(missing)}")

    distance = response.distance_m.to_numpy(float)
    motion = response.cumulative_suspension_motion_m.to_numpy(float)
    start0 = float(distance[0])
    final = float(distance[-1])
    rows = []
    for start in np.arange(start0, final, segment_length_m):
        end = min(start + segment_length_m, final)
        actual_length = end - start
        if actual_length < minimum_fraction * segment_length_m:
            continue
        start_motion, end_motion = np.interp([start, end], distance, motion)
        suspension_motion_m = float(end_motion - start_motion)
        rows.append(
            {
                "seg_start_m": float(start),
                "seg_end_m": float(end),
                "length_m": float(actual_length),
                "suspension_motion_m": suspension_motion_m,
                "iri_m_per_km": 1000.0 * suspension_motion_m / actual_length,
                "is_partial_segment": bool(actual_length < segment_length_m - 1e-9),
            }
        )
    return pd.DataFrame(rows)


def full_length_iri(response: pd.DataFrame) -> float:
    """计算整个已观测剖面的长度归一化 IRI（m/km）。"""
    distance = response.distance_m.to_numpy(float)
    motion = response.cumulative_suspension_motion_m.to_numpy(float)
    length_m = float(distance[-1] - distance[0])
    if length_m <= 0:
        raise ValueError("剖面长度必须大于 0")
    return float(1000.0 * (motion[-1] - motion[0]) / length_m)
