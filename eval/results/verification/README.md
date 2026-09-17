# Recorded integration verification

- `final-full-tests-125.txt`: the suite at revision `7bc31e7` passed all 125 tests in 74.600 seconds, including five real-data checks and both operational-bypass disclosure regressions, with no skips.
- `final-full-tests.txt`: the earlier integrated suite passed 123 tests in 72.002 seconds, including five real-data checks, with no skips.
- `official-validator.txt`: the official submission shape validator completed two real cases and reported zero warnings.
- Localhost SDK transport checks exercised HTTP-200 error fallback and disabled automatic hidden retries without calling an external model endpoint.

The transcripts are copied from the actual coordinator output. Absolute user/workspace/interpreter and temporary-directory paths are replaced with `<repo>`, `<python>` and `<temporary-output>` for portability. No measurement, status or assertion is changed. The two disclosure-only regressions are included in the 125-test transcript. These checks establish integration/shape behavior, not diagnostic accuracy, model-routing savings, or Docker acceptance.
