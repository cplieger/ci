# Monitoring and alerts

## Alerting

```yaml
# An example inside a fence is not the alert table.
| Alert | Fires when | Severity |
| `FixtureInFence` | never | info |
```

| Alert | Fires when | Severity |
| --- | --- | --- |
| `FixtureDown` | the fixture app is not scraped | critical |
| `FixtureErrorLogged` | the fixture app reports an error | warning |

| Route | Method |
| --- | --- |
| `/healthz` | `GET` |
