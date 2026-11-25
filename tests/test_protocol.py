from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from hwcj_iri.protocol import (  # noqa: E402
    ProtocolError,
    decode_measurement_packets,
    parse_capture_file,
    scan_frames,
)


class ProtocolTests(unittest.TestCase):
    def test_measurement_frame_is_big_endian_and_skips_counter(self) -> None:
        prefix = bytes.fromhex("fa1aa30000000a")
        payload = bytes.fromhex("0123") + bytes.fromhex("0456") * 9
        body = prefix + payload
        frames, audit = scan_frames(body + bytes([sum(body) & 0xFF]))
        self.assertEqual(audit["invalid_checksum_candidate_count"], 0)
        packet, = decode_measurement_packets(frames)
        self.assertEqual(packet.packet_counter_u8, 10)
        self.assertEqual(packet.adc_counts, (0x0123,) + (0x0456,) * 9)

    def test_payload_fa1a_is_not_mistaken_for_a_new_frame(self) -> None:
        prefix = bytes.fromhex("fa1aa30000000a")
        payload = bytes.fromhex("fa1a") + bytes.fromhex("0001") * 9
        without_checksum = prefix + payload
        raw = without_checksum + bytes([sum(without_checksum) & 0xFF])
        frames, audit = scan_frames(raw)
        self.assertEqual(len(frames), 1)
        self.assertEqual(audit["invalid_checksum_candidate_count"], 0)
        self.assertEqual(decode_measurement_packets(frames)[0].adc_counts[0], 0xFA1A)

    def test_checksum_failure_is_rejected_in_strict_mode(self) -> None:
        frame = bytes.fromhex("fa1aa30000000a") + bytes.fromhex("07d0") * 10
        bad_checksum = (sum(frame) & 0xFF) ^ 1
        with TemporaryDirectory() as directory:
            temporary = Path(directory) / "corrupt.bin"
            temporary.write_bytes(frame + bytes([bad_checksum]))
            with self.assertRaises(ProtocolError):
                parse_capture_file(temporary, strict=True)

    def test_missing_packet_preserves_logical_sample_count(self) -> None:
        frames = []
        for counter in (10, 20, 40, 50):
            frame = bytes((0xFA, 0x1A, 0xA3, 0, 0, 0, counter))
            frame += bytes.fromhex("07d0") * 10
            frames.append(frame + bytes([sum(frame) & 0xFF]))
        with TemporaryDirectory() as directory:
            capture = Path(directory) / "gap.bin"
            capture.write_bytes(b"".join(frames))
            _, _, audit = parse_capture_file(capture, strict=False)
        self.assertEqual(audit.measurement_sample_count, 40)
        self.assertEqual(audit.inferred_missing_packet_count, 1)
        self.assertEqual(audit.logical_sample_count_with_inferred_gaps, 50)


if __name__ == "__main__":
    unittest.main()
