# Fixtures

These files let the whole pipeline run with **no accounts or API keys**.

## Synthetic athlete

When `TRINITY_OFFLINE=true` (the default), the connectors serve a deterministic
synthetic athlete generated in code by
[`src/trinity/connectors/synthetic.py`](../../src/trinity/connectors/synthetic.py):

- ~16 weeks of multisport history (swim/bike/run) ending on a fixed anchor date,
- daily wellness (HRV rMSSD, resting HR, sleep, weight),
- sport settings (FTP/eFTP, run threshold pace, CSS, LTHR, VO2max estimate).

It is fabricated data for demos and tests, **not** real or medical data.

## Sample calendar

`calendar.sample.json` is an example competition calendar (A/B/C races). To use
it, copy it to the active location:

```bash
cp data/fixtures/calendar.sample.json data/calendar-athlete.json
```

## Sample health records

`health.sample.yaml` shows how to enter lab panels (bloodwork) and other health
entries for the doctor agent. Its values are invented. To use it, copy it to the
active location and replace them with your own:

```bash
mkdir -p data/health
cp data/fixtures/health.sample.yaml data/health/records.yaml
uv run trinity health
```

## Recording real fixtures (optional)

To record real API responses for offline replay, set credentials in `.env`,
set `TRINITY_OFFLINE=false`, and capture with `vcrpy` cassettes under
`data/fixtures/private/` (gitignored — keep your data private).
