# Harness real-model control

Known synthetic regression: ten cases per domain, three repetitions per version. Not human business golden data.

| Domain | Baseline passed | Upgraded passed | False completion: baseline / upgraded |
|---|---:|---:|---:|
| code | 18/30 | 19/30 | 0 / 0 |
| document | 20/30 | 20/30 | 0 / 0 |
| research | 9/30 | 10/30 | 2 / 2 |

Non-regression gate: True

Original assertions and raw failures retained; no post-hoc rescoring. No automatic Skill publication. Latency includes local load and is not a speed guarantee.
