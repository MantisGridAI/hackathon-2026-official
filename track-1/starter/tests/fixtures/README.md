`synthetic.py` generates deliberately fabricated telemetry for wiring and failure
tests. Its values and expected answers are never real incident evidence.

`telemetry_only.json` contains eight original records from the allowed official
Market-cloudbed-1 bundle: the first CSV data record in each source on March 20.
It preserves source-relative filenames, original record indexes and raw fields,
plus the bundle manifest hash. It contains no queries or development labels.
This small locator sample is not a complete window and must not be reported as
complete coverage. The upstream attribution/license is in root ATTRIBUTION.md.

Set `RCA_TEST_DATA` to the official full bundle to run real Store, metrics, trace
and controller checks. Full telemetry stays outside Git under `track-1/data/`.
