"""HWCJ-02 TCP 抓包协议解析。

本模块只解释已经由现有 18 个实测 BIN 文件共同验证的字段。尚不能从厂家
资料确认含义的 3 个元数据字节会原样保留，但不会被擅自命名成速度、高程或
里程。这样既不会丢信息，也不会把推测写成事实。

实测帧的一般结构为：

    FA | length | payload ... | checksum

``length`` 表示其后字节数，因此总帧长为 ``length + 2``。``checksum`` 是
从 ``FA`` 到校验字节前一字节之和对 256 取模。测量帧固定为 28 字节：

    FA 1A A3 | meta[3] | counter[1] | 10 × uint16(big-endian) | checksum

历史提取器从 counter 字节开始按小端 uint16 配对，并通过搜索下一个 FA1A
判断帧尾；两种做法都不符合上述结构，会造成数值错位和丢样。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import struct

import numpy as np
import pandas as pd


SYNC_BYTE = 0xFA
MEASUREMENT_LENGTH_BYTE = 0x1A
MEASUREMENT_COMMAND = 0xA3
MEASUREMENT_FRAME_BYTES = MEASUREMENT_LENGTH_BYTE + 2
SAMPLES_PER_MEASUREMENT_FRAME = 10
SAMPLE_TABLE_COLUMNS = (
    "sample_index",
    "sample_ordinal",
    "valid_sample_index",
    "distance_m",
    "raw_adc_count",
    "sample_status",
    "packet_index",
    "sample_in_packet",
    "packet_byte_offset",
    "packet_counter_u8",
    "meta_byte3_u8",
    "meta_bytes4_5_hex",
    "frame_checksum_u8",
    "missing_packet_ordinal_before_next_valid",
)


class ProtocolError(ValueError):
    """抓包包含校验失败、截断帧或没有测量帧。"""


@dataclass(frozen=True)
class Frame:
    """一个通过长度和校验和双重检查的原始帧。"""

    frame_index: int
    byte_offset: int
    length_byte: int
    command_u8: int
    raw: bytes

    @property
    def checksum_stored_u8(self) -> int:
        return self.raw[-1]

    @property
    def checksum_computed_u8(self) -> int:
        return sum(self.raw[:-1]) & 0xFF


@dataclass(frozen=True)
class MeasurementPacket:
    """一帧 A3 测量数据。

    ``meta_byte3_u8`` 与 ``meta_bytes4_5_hex`` 的物理含义尚未得到厂家协议
    文档确认。``packet_counter_u8`` 在全部现有数据中以每帧 10 的规律变化，
    可用于连续性审计，但不直接当作物理距离。
    """

    packet_index: int
    frame_index: int
    byte_offset: int
    meta_byte3_u8: int
    meta_bytes4_5_hex: str
    packet_counter_u8: int
    adc_counts: tuple[int, ...]
    checksum_u8: int


@dataclass(frozen=True)
class ParseAudit:
    """解析质量台账；所有计数都会写入 JSON。"""

    source_path: str
    source_sha256: str
    file_size_bytes: int
    valid_frame_count: int
    measurement_packet_count: int
    measurement_sample_count: int
    logical_sample_count_with_inferred_gaps: int
    frame_type_counts: dict[str, int]
    invalid_checksum_candidate_count: int
    truncated_candidate_count: int
    unframed_byte_count: int
    adc_outside_12bit_count: int
    adc_equal_zero_count: int
    adc_equal_4000_count: int
    adc_near_observed_rails_count: int
    adc_near_observed_rails_fraction: float
    inferred_missing_packet_count: int
    inferred_missing_sample_count: int
    counter_transition_unexpected_count: int


def sha256_file(path: Path) -> str:
    """流式计算源文件哈希，供结果追溯。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scan_frames(data: bytes) -> tuple[list[Frame], dict[str, int]]:
    """按 ``FA + length + checksum`` 扫描并返回完整有效帧。

    校验失败时只向前移动一个字节重新寻找同步头，避免一次坏帧吞掉后续数据。
    对有效帧则直接跳过整帧，因此载荷内部即使出现 ``FA 1A`` 也不会被错认成
    新帧。
    """
    frames: list[Frame] = []
    offset = 0
    invalid_checksum = 0
    truncated = 0
    unframed = 0

    while offset < len(data):
        if data[offset] != SYNC_BYTE:
            unframed += 1
            offset += 1
            continue
        if offset + 2 > len(data):
            truncated += 1
            unframed += len(data) - offset
            break

        total_length = data[offset + 1] + 2
        if total_length < 4 or offset + total_length > len(data):
            truncated += 1
            unframed += 1
            offset += 1
            continue

        raw = data[offset : offset + total_length]
        if (sum(raw[:-1]) & 0xFF) != raw[-1]:
            invalid_checksum += 1
            unframed += 1
            offset += 1
            continue

        frames.append(
            Frame(
                frame_index=len(frames),
                byte_offset=offset,
                length_byte=raw[1],
                command_u8=raw[2],
                raw=raw,
            )
        )
        offset += total_length

    return frames, {
        "invalid_checksum_candidate_count": invalid_checksum,
        "truncated_candidate_count": truncated,
        "unframed_byte_count": unframed,
    }


def decode_measurement_packets(frames: list[Frame]) -> list[MeasurementPacket]:
    """从有效帧中解出 A3 测量帧的 10 个大端 16 位 ADC 值。"""
    packets: list[MeasurementPacket] = []
    for frame in frames:
        raw = frame.raw
        if not (
            frame.length_byte == MEASUREMENT_LENGTH_BYTE
            and frame.command_u8 == MEASUREMENT_COMMAND
            and len(raw) == MEASUREMENT_FRAME_BYTES
        ):
            continue

        # 字节 7..26 共 20 字节；">10H" 明确指定 10 个大端无符号 16 位整数。
        adc_counts = struct.unpack(">10H", raw[7:27])
        packets.append(
            MeasurementPacket(
                packet_index=len(packets),
                frame_index=frame.frame_index,
                byte_offset=frame.byte_offset,
                meta_byte3_u8=raw[3],
                meta_bytes4_5_hex=raw[4:6].hex().upper(),
                packet_counter_u8=raw[6],
                adc_counts=tuple(int(value) for value in adc_counts),
                checksum_u8=raw[-1],
            )
        )
    return packets


_COUNTER_TO_POSITION = {(10 * position) % 256: position for position in range(1, 101)}


def _counter_step_count(left_u8: int, right_u8: int) -> int | None:
    """由相邻 counter 推断经过了多少个测量帧。

    实测 counter 对应一个 100 帧块内的第 1..100 帧，其值为
    ``(10 × position) mod 256``；块末从 position=100（232）回到下一块
    position=1（10）。1 表示连续，2 表示中间缺 1 帧。该规律已在现有 18 个
    BIN 中验证，但它只用于完整性审计和保持相对链程，不被解释成厂家定义的
    绝对里程字段。
    """
    left_position = _COUNTER_TO_POSITION.get(left_u8)
    right_position = _COUNTER_TO_POSITION.get(right_u8)
    if left_position is None or right_position is None:
        return None
    if right_position > left_position:
        return right_position - left_position
    return (100 - left_position) + right_position


def _missing_packets_before(packets: list[MeasurementPacket]) -> tuple[list[int], int]:
    """返回每个有效包之前推断的缺包数及无法解释的 transition 数。"""
    missing_before = [0] * len(packets)
    unexpected = 0
    for index, (left, right) in enumerate(zip(packets, packets[1:]), start=1):
        steps = _counter_step_count(left.packet_counter_u8, right.packet_counter_u8)
        # 一次跳过完整 100 帧块在单字节 counter 中不可辨识。当前实测缺口均
        # 很短，因此把 >50 的跳跃视为无法可靠解释，而不是自动插入大量数据。
        if steps is None or steps < 1 or steps > 50:
            unexpected += 1
            continue
        missing_before[index] = steps - 1
    return missing_before, unexpected


def frames_to_dataframe(frames: list[Frame]) -> pd.DataFrame:
    """把所有有效帧保存成可审计表，不丢弃非测量帧。"""
    return pd.DataFrame(
        [
            {
                "frame_index": frame.frame_index,
                "byte_offset": frame.byte_offset,
                "length_byte": frame.length_byte,
                "total_length_bytes": len(frame.raw),
                "command_u8": frame.command_u8,
                "command_hex": f"0x{frame.command_u8:02X}",
                "checksum_stored_u8": frame.checksum_stored_u8,
                "checksum_computed_u8": frame.checksum_computed_u8,
                "raw_hex": frame.raw.hex().upper(),
            }
            for frame in frames
        ]
    )


def packets_to_samples_dataframe(
    packets: list[MeasurementPacket],
    sample_spacing_m: float = 0.1,
    distance_offset_m: float = 0.0,
) -> pd.DataFrame:
    """把每帧 10 个 ADC 值展开为逐空间样本表。

    ``distance_m`` 是把首个已抓到样本重新定义为 0 m 后的相对链程：
    ``distance_offset_m + sample_index × sample_spacing_m``。它不是 GPS 距离，也
    不是由未知 meta 字段推断出的仪器绝对里程。
    """
    if sample_spacing_m <= 0:
        raise ValueError("sample_spacing_m 必须大于 0")

    rows = []
    logical_sample_index = 0
    valid_sample_index = 0
    missing_before, _ = _missing_packets_before(packets)
    for packet, missing_packet_count in zip(packets, missing_before):
        # 缺失位置只以 NaN 占位，用来防止后续有效样本被错误地向前压缩。
        # 是否允许线性插值由 profile 层的 gap_policy 明确决定。
        for missing_packet_ordinal in range(missing_packet_count):
            for sample_in_packet in range(SAMPLES_PER_MEASUREMENT_FRAME):
                rows.append(
                    {
                        "sample_index": logical_sample_index,
                        "sample_ordinal": logical_sample_index + 1,
                        "valid_sample_index": pd.NA,
                        "distance_m": distance_offset_m
                        + logical_sample_index * sample_spacing_m,
                        "raw_adc_count": np.nan,
                        "sample_status": "inferred_missing_from_counter",
                        "packet_index": pd.NA,
                        "sample_in_packet": sample_in_packet,
                        "packet_byte_offset": pd.NA,
                        "packet_counter_u8": pd.NA,
                        "meta_byte3_u8": pd.NA,
                        "meta_bytes4_5_hex": pd.NA,
                        "frame_checksum_u8": pd.NA,
                        "missing_packet_ordinal_before_next_valid": missing_packet_ordinal,
                    }
                )
                logical_sample_index += 1
        for sample_in_packet, adc_count in enumerate(packet.adc_counts):
            rows.append(
                {
                    "sample_index": logical_sample_index,
                    "sample_ordinal": logical_sample_index + 1,
                    "valid_sample_index": valid_sample_index,
                    "distance_m": distance_offset_m
                    + logical_sample_index * sample_spacing_m,
                    "raw_adc_count": adc_count,
                    "sample_status": "observed_valid_frame",
                    "packet_index": packet.packet_index,
                    "sample_in_packet": sample_in_packet,
                    "packet_byte_offset": packet.byte_offset,
                    "packet_counter_u8": packet.packet_counter_u8,
                    "meta_byte3_u8": packet.meta_byte3_u8,
                    "meta_bytes4_5_hex": packet.meta_bytes4_5_hex,
                    "frame_checksum_u8": packet.checksum_u8,
                    "missing_packet_ordinal_before_next_valid": pd.NA,
                }
            )
            logical_sample_index += 1
            valid_sample_index += 1
    return pd.DataFrame(rows, columns=SAMPLE_TABLE_COLUMNS)


def parse_capture_file(
    path: str | Path,
    sample_spacing_m: float = 0.1,
    distance_offset_m: float = 0.0,
    strict: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, ParseAudit]:
    """解析一个 BIN，返回 ``(frames, samples, audit)``。

    ``strict=True`` 时，任何校验失败/截断、没有测量帧或 12 位范围外 ADC 都会
    阻止继续计算 IRI，防止坏抓包静默进入标签。
    """
    source = Path(path).expanduser().resolve()
    data = source.read_bytes()
    frames, scan_audit = scan_frames(data)
    packets = decode_measurement_packets(frames)
    frame_table = frames_to_dataframe(frames)
    sample_table = packets_to_samples_dataframe(
        packets,
        sample_spacing_m=sample_spacing_m,
        distance_offset_m=distance_offset_m,
    )

    type_counts = Counter(f"0x{frame.command_u8:02X}" for frame in frames)
    missing_before, unexpected_transitions = _missing_packets_before(packets)
    inferred_missing_packets = int(sum(missing_before))
    outside_12bit = (
        int(
            (
                (sample_table.raw_adc_count.dropna() < 0)
                | (sample_table.raw_adc_count.dropna() > 4095)
            ).sum()
        )
        if not sample_table.empty
        else 0
    )
    observed_adc = sample_table.raw_adc_count.dropna()
    adc_equal_zero = int((observed_adc == 0).sum())
    adc_equal_4000 = int((observed_adc == 4000).sum())
    adc_near_rails = int(((observed_adc <= 10) | (observed_adc >= 3990)).sum())
    audit = ParseAudit(
        source_path=str(source),
        source_sha256=sha256_file(source),
        file_size_bytes=len(data),
        valid_frame_count=len(frames),
        measurement_packet_count=len(packets),
        measurement_sample_count=len(packets) * SAMPLES_PER_MEASUREMENT_FRAME,
        logical_sample_count_with_inferred_gaps=len(sample_table),
        frame_type_counts=dict(sorted(type_counts.items())),
        invalid_checksum_candidate_count=scan_audit["invalid_checksum_candidate_count"],
        truncated_candidate_count=scan_audit["truncated_candidate_count"],
        unframed_byte_count=scan_audit["unframed_byte_count"],
        adc_outside_12bit_count=outside_12bit,
        adc_equal_zero_count=adc_equal_zero,
        adc_equal_4000_count=adc_equal_4000,
        adc_near_observed_rails_count=adc_near_rails,
        adc_near_observed_rails_fraction=(
            float(adc_near_rails / len(observed_adc)) if len(observed_adc) else 0.0
        ),
        inferred_missing_packet_count=inferred_missing_packets,
        inferred_missing_sample_count=(
            inferred_missing_packets * SAMPLES_PER_MEASUREMENT_FRAME
        ),
        counter_transition_unexpected_count=unexpected_transitions,
    )

    problems = []
    if audit.invalid_checksum_candidate_count:
        problems.append(f"{audit.invalid_checksum_candidate_count} 个校验失败候选帧")
    if audit.truncated_candidate_count:
        problems.append(f"{audit.truncated_candidate_count} 个截断候选帧")
    if audit.unframed_byte_count:
        problems.append(f"{audit.unframed_byte_count} 个字节未归入有效帧")
    if audit.measurement_packet_count == 0:
        problems.append("没有 A3 测量帧")
    if audit.adc_outside_12bit_count:
        problems.append(f"{audit.adc_outside_12bit_count} 个 ADC 值超出 0..4095")
    if audit.inferred_missing_packet_count:
        problems.append(f"counter 推断缺失 {audit.inferred_missing_packet_count} 个测量帧")
    if audit.counter_transition_unexpected_count:
        problems.append(
            f"{audit.counter_transition_unexpected_count} 个 counter 跳变无法可靠解释"
        )
    if strict and problems:
        raise ProtocolError("；".join(problems))

    return frame_table, sample_table, audit


def audit_as_dict(audit: ParseAudit) -> dict[str, object]:
    """为 JSON 序列化提供稳定入口。"""
    return asdict(audit)
