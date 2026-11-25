# HWCJ-02 Eight-Wheel IRI Toolkit

Turn HWCJ-02 eight-wheel profiler captures into International Roughness Index (IRI) estimates for 1, 5, 10, and 20 m road segments. The toolkit simulates the Golden Car once over the full profile and extracts every segment from the same cumulative suspension response. Vehicle state carries across segment boundaries, so the suspension travel of adjacent segments adds up to that of their combined interval.

## Quick start

Use Python 3.10 or newer. Run these commands from the repository root to generate a synthetic capture and process it without an instrument.

```sh
python -m pip install -r requirements.txt
python examples/generate_capture.py demo.bin
python run_pipeline.py demo.bin --output-dir output --segments 1,5,10,20 --no-plot
python -m unittest discover -s tests -v
```

Read `output/demo/05_iri_10m.csv` for the 10 m estimates. Use your own BIN path in place of `demo.bin` for field data. The same command accepts a directory of captures and creates a separate output subdirectory for each BIN. Use a fresh output directory when changing settings to keep runs distinct.

For field capture, connect an ADB-authorized Android tablet to the instrument network and make an executable BusyBox with `nc` available on the tablet. Use `python one_click_pipeline.py --help` for capture and processing options, or `python capture_hwcj.py --help` to save just the raw stream. The shell and batch wrappers run the same Python modules on Ubuntu/WSL and Windows.

## From capture to segment IRI

1. **Decode the byte stream.** The parser reads frames beginning with `0xFA`, uses the length byte to find the frame boundary, and checks the sum of the preceding bytes modulo 256. A 28-byte `0xA3` measurement frame carries ten big-endian unsigned 16-bit ADC values at offsets 7–26, with zero-based indexing. Offset 6 holds a packet counter. The observed counter cycles through `(10 × position) mod 256` for positions 1–100 and is used to infer gaps.
2. **Build the spatial profile.** Assign each logical sample a relative chainage of `distance_offset_m + i × sample_spacing_m`, with 0.1 m spacing by default. Insert placeholders for inferred missing samples so later chainage stays aligned. Strict processing rejects capture integrity failures. To investigate a gap, explicitly opt into diagnostic processing with `--allow-corrupt --gap-policy linear`. Linear interpolation fills internal gaps and the manifest reports the diagnostic status.
3. **Convert and condition the profile.** Multiply ADC counts after zero-offset subtraction by the project scale, 0.05 mm/count by default, and subtract the first displacement to obtain a relative profile. The default second-order, zero-phase Butterworth spatial high-pass uses a 100 m cutoff wavelength. Use `--no-highpass` to disable it. Convert the profile to metres and apply a centered 0.25 m box average when sample spacing is at most 0.125 m.
4. **Simulate the Golden Car.** Use a quarter-car model at 80 km/h with tire stiffness per sprung mass of 653 s⁻², suspension stiffness per sprung mass of 63.3 s⁻², damping per sprung mass of 6 s⁻¹, and an unsprung-to-sprung mass ratio of 0.15. Initialize both masses at the first conditioned elevation. Their initial velocity is the simulation speed times the grade over approximately the first 11 m, using the last available point for shorter profiles. Solve the linear state-space system over the full profile with SciPy's `lsim` and linear interpolation of the input. Sum the absolute difference between sprung and unsprung vertical velocities over time using the right-endpoint sample of each time step. This gives cumulative suspension travel `R(s)` in metres at chainage `s`.
5. **Extract each segment.** Starting at the first sample's chainage, lay out nonoverlapping bins of the requested length. Linearly interpolate cumulative travel at each boundary and calculate `IRI[a,b] = 1000 × (R(b) − R(a)) / (b − a)` in m/km. The final segment is retained when it spans at least 95% of the requested length by default, and its IRI uses its actual length. Full-length IRI uses the first and last observed chainages. A length-weighted mean of segment IRI matches the IRI of the same covered interval. A scale that drops its tail covers a shorter interval than the full-length result.

The 0.1 m spacing and 0.05 mm/count conversion are project settings. The latter is a calibration assumption, so report results as project IRI estimates and record the scale used. Exact hits at ADC counts 0 and 4000 flag possible clipping. Check the manifest before treating a run as quantitative reference data.

## Outputs

| File | What to inspect |
| --- | --- |
| `01_protocol_frames.csv` | Valid frames, byte offsets, and raw frame bytes |
| `02_raw_samples.csv` | Decoded counts, chainage, and missing-sample placeholders |
| `03_profile_for_iri.csv` | Observed and used counts, interpolation flags, and the conditioned profile |
| `04_continuous_golden_car_response.csv` | Vehicle states and cumulative suspension travel |
| `05_iri_<length>m.csv` | Start and end chainage, actual length, suspension travel, IRI, and partial-segment flag |
| `06_overview.png` | Profile and multi-scale IRI overview, when plotting is enabled |
| `run_manifest.json` | Input SHA-256, settings, protocol audit, quality flags, and summary IRI |

## Code layout

| Module | Role |
| --- | --- |
| `hwcj_iri/protocol.py` | Check frame integrity, decode ADC counts, and infer packet gaps |
| `hwcj_iri/profile.py` | Build the relative profile and apply configured preprocessing |
| `hwcj_iri/golden_car.py` | Simulate the full profile and extract segment IRI |
| `hwcj_iri/pipeline.py` | Run the stages and write the results and manifest |
| `hwcj_iri/capture.py` | Stream raw bytes to disk through ADB |
| `hwcj_iri/cli.py`, `hwcj_iri/one_click.py` | Offline and capture-to-IRI command-line workflows |
| `examples/`, `tests/` | Synthetic capture generator and self-contained checks |
