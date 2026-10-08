## Staleness decisions (default threshold: 7d)

| Repo | Last successful docker build (UTC) | Age (days) | Threshold (days) | Decision |
|---|---|---|---|---|
| fresh-app | 2026-10-02T04:00:00Z | 2 | 7 | fresh |
| stale-app | 2026-09-21T04:00:00Z | 13 | 7 | **rebuild** |
| edge-app | 2026-09-27T04:00:00Z | 7 | 7 | fresh |
| past-edge-app | 2026-09-27T03:59:59Z | 7 | 7 | **rebuild** |
| nofinal-app | none in 20 newest successes | n/a | 7 | **rebuild** |
| baddate-app | not-a-date (unparsable) | n/a | 7 | **rebuild** |
| tool-catalog | 2026-09-25T04:00:00Z | 9 | 7 | **rebuild** |

Rebuilding 5 repo(s).
