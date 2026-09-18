## Module and result

- Module: M1 / M2 / M3 / M4 / M5 / claim-only / docs
- Owner / claim PR:
- Outcome and files owned by this change:
- Interface version: rca-v1

## Integration contract

- Public functions and input/output example:
- Upstream dependency / downstream caller:
- Shared-type changes (or none), affected consumers and migration:
- Missing data / deadline / API failure behavior:

## Checks actually run

- Commands, result and environment:
- Real-window check or synthetic fixture (label which):
- Calls / dollars spent; no paid calls if only offline checks:
- Limitations and unfinished implementation:

## Review checklist

- [ ] Only owned files changed, or cross-module changes coordinated and documented.
- [ ] No keys, raw data, bulk cache or runtime use of development answers.
- [ ] UTC+8, seconds/milliseconds and missing coverage handled where applicable.
- [ ] Evidence IDs/values trace to actual data; no model-invented measurements.
- [ ] Fault count, exact names/labels, requested fields and key order checked where applicable.
- [ ] Time/cost, retry/fallback and usage accounting checked where applicable.
- [ ] Consumer can call the exported interface; independent module tests pass.
- [ ] TEAM.md state reflects actual integration/verification, not just code generation.

For claim-only or documentation changes, mark implementation checks as not applicable in the checks section; do not claim runtime tests were executed.
