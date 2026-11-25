from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from hwcj_iri.golden_car import (  # noqa: E402
    continuous_golden_car,
    full_length_iri,
    summarize_response,
    tire_enveloping_box_filter,
)
from hwcj_iri.profile import build_profile  # noqa: E402


class ProfileAndIriTests(unittest.TestCase):
    def test_fast_tire_filter_matches_direct_definition(self) -> None:
        distance = np.arange(0.0, 3.01, 0.1)
        elevation = np.sin(distance) + 0.1 * distance
        direct = np.array(
            [
                elevation[np.abs(distance - position) <= 0.125 + 1e-12].mean()
                for position in distance
            ]
        )
        actual = tire_enveloping_box_filter(distance, elevation)
        np.testing.assert_allclose(actual, direct, rtol=0, atol=1e-14)

    def test_flat_profile_has_zero_iri(self) -> None:
        distance = np.arange(0.0, 100.01, 0.1)
        response, _ = continuous_golden_car(distance, np.zeros_like(distance))
        self.assertAlmostEqual(full_length_iri(response), 0.0, places=12)

    def test_multiscale_summaries_are_additive(self) -> None:
        distance = np.arange(0.0, 100.01, 0.1)
        elevation = 0.004 * np.sin(2 * np.pi * distance / 4.3)
        elevation += 0.0012 * np.sin(2 * np.pi * distance / 0.9)
        response, _ = continuous_golden_car(distance, elevation)
        full = full_length_iri(response)
        for scale in (1.0, 5.0, 10.0, 20.0):
            table = summarize_response(response, scale, minimum_fraction=1.0)
            weighted = np.average(table.iri_m_per_km, weights=table.length_m)
            self.assertAlmostEqual(weighted, full, places=10)

    def test_gap_policy_must_be_explicit(self) -> None:
        samples = pd.DataFrame(
            {
                "sample_index": np.arange(5),
                "distance_m": np.arange(5) * 0.1,
                "raw_adc_count": [1000.0, 1010.0, np.nan, 1030.0, 1040.0],
            }
        )
        with self.assertRaises(ValueError):
            build_profile(samples, highpass_wavelength_m=None, gap_policy="error")
        profile = build_profile(
            samples,
            highpass_wavelength_m=None,
            gap_policy="linear",
        )
        self.assertEqual(int(profile.is_interpolated.sum()), 1)
        self.assertEqual(profile.raw_adc_count_used.iloc[2], 1020.0)


if __name__ == "__main__":
    unittest.main()
