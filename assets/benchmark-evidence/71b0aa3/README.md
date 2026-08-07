# Benchmark evidence for `71b0aa3`

These files are the exact extracted payload from the successful
[CI run 31172234073](https://github.com/profundium/pg_local_cache/actions/runs/31172234073)
for merge revision
[`71b0aa3a27c5c009b7ba08bbaa660147f078bde8`](https://github.com/profundium/pg_local_cache/commit/71b0aa3a27c5c009b7ba08bbaa660147f078bde8).
Every job and every independent benchmark gate passed.

| File | SHA-256 |
|---|---|
| [`whole-row.json`](whole-row.json) | `a36f7da08e916d9956d67ec83687f60fa6b0694bce347ed14ae774bbc5270b27` |
| [`whole-row.txt`](whole-row.txt) | `1eeef1563aeb7e3694f28ed364cbfc40553e7f8ac51d37fa02191099dcfbff54` |

The original Actions artifact was `comparison-smoke` (`8991446702`). Its ZIP
digest was
`d80dc54358bc0d4a723217cb93ec14b09938eb0a78f7c7ef12449cebb68a3db0`.
The extracted JSON retains the source revision, exact workload, container image
identities, CPU and memory limits, every measured lane, cache-counter deltas,
and gates.

This is a one-second shared-runner regression smoke, not a production capacity
claim. It is preserved because it is the first source-pinned result containing
the transparent ordinary `SELECT ... IN (...)` lane.
