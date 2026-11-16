"""Load normalized raw data from the partitioned store into DataFrames."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from trinity.config import Config, get_config
from trinity.storage.io import load_json


def _read_partitions(root: Path) -> pd.DataFrame:
    if not root.exists():
        return pd.DataFrame()
    frames = [pd.read_csv(p) for p in sorted(root.rglob("*.csv"))]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_activities(config: Config | None = None) -> pd.DataFrame:
    config = config or get_config()
    df = _read_partitions(config.raw_dir / "intervals" / "activities")
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = _apply_power_corrections(df, load_power_corrections(config))
    df = _apply_durability(df, load_durability(config))
    df = df.sort_values("start_time").reset_index(drop=True)
    return df


def load_durability(config: Config | None = None) -> dict:
    """Stream-computed decoupling / fade, keyed by activity id.

    Written by ``trinity durability``. Lives at ``<data_dir>/durability-<athlete>.json``
    rather than in raw/ so `fetch` never overwrites it, and is overlaid on load so
    it can be recomputed or withdrawn without touching the raw record.
    """
    config = config or get_config()
    doc = load_json(config.data_dir / "durability-athlete.json", default={})
    return dict(doc.get("activities") or {})


def _apply_durability(df: pd.DataFrame, computed: dict) -> pd.DataFrame:
    """Overlay our own Pa:Hr / Pw:Hr numbers, and add ``fade_pct``.

    The provider only reports decoupling for rides, so runs arrive empty; and our
    figure is trustworthiness-checked against the device's own moving pace, which
    theirs is not. Untrustworthy results are simply absent from the file.
    """
    if "fade_pct" not in df.columns:
        df["fade_pct"] = pd.NA
    if not computed or df.empty or "id" not in df.columns:
        return df
    ids = df["id"].astype(str)
    for col in ("decoupling_pct", "fade_pct"):
        vals = ids.map(lambda k, c=col: computed.get(k, {}).get(c))
        df[col] = pd.to_numeric(vals, errors="coerce").combine_first(
            pd.to_numeric(df[col], errors="coerce") if col in df.columns else pd.Series(pd.NA, index=df.index)
        )
    return df


def load_wellness(config: Config | None = None) -> pd.DataFrame:
    config = config or get_config()
    df = _read_partitions(config.raw_dir / "intervals" / "wellness")
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.sort_values("date").reset_index(drop=True)
    return df


def load_nutrition(config: Config | None = None) -> pd.DataFrame:
    """Daily nutrition totals from the Fitatu partitioned store (one row/day)."""
    config = config or get_config()
    df = _read_partitions(config.raw_dir / "fitatu" / "nutrition")
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.drop_duplicates(subset="date", keep="last")
    df = df.sort_values("date").reset_index(drop=True)
    return df


def load_settings(config: Config | None = None) -> dict:
    config = config or get_config()
    settings = load_json(config.raw_dir / "intervals" / "settings.json", default={})
    return _merge_manual_metrics(settings, load_manual_metrics(config))


def load_manual_metrics(config: Config | None = None) -> dict:
    """User-authored metrics that no connector provides (e.g. Garmin VO2max).

    Lives at ``<data_dir>/manual_metrics.json`` — OUTSIDE raw/ so `fetch` never
    overwrites it.
    """
    config = config or get_config()
    return load_json(config.data_dir / "manual_metrics.json", default={})


# Everything that scales linearly with what the meter reported. TSS is handled
# separately because it scales with the square.
_LINEAR_POWER_COLUMNS = ("np_w", "avg_w", "intensity_factor", "efficiency_factor")


def load_power_corrections(config: Config | None = None) -> list[dict]:
    """Calibration corrections for periods when the power meter mis-read.

    Lives at ``<data_dir>/manual_corrections.json`` — OUTSIDE raw/ so `fetch`
    never overwrites it. Applied on every load rather than written back, so the
    raw partitions stay a faithful record of what the device actually reported
    and a correction can be revised or withdrawn later.
    """
    config = config or get_config()
    doc = load_json(config.data_dir / "manual_corrections.json", default={})
    return list(doc.get("power_calibration") or [])


def _apply_power_corrections(df: pd.DataFrame, corrections: list[dict]) -> pd.DataFrame:
    """Rescale power, and everything derived from it, over a mis-calibrated period.

    A meter reading low understates power linearly but training load
    QUADRATICALLY: TSS is proportional to NP x IF and IF is NP/FTP, so a meter
    25% low costs 56% of the session's TSS. Correcting the watts and leaving TSS
    alone would leave the load record wrong in a way nothing downstream notices.

    This holds only while FTP is independent of the faulty meter. An FTP *tested*
    on that same meter absorbs the identical error, leaving IF — and therefore
    TSS — already correct; such a period must be recorded with the power columns
    scaled but the load left alone, which this helper deliberately cannot express.
    """
    if not corrections or df.empty:
        return df
    for c in corrections:
        factor = float(c["factor"])
        rows = pd.Series(
            [dt.date.fromisoformat(c["from"]) <= d <= dt.date.fromisoformat(c["to"])
             for d in df["date"]],
            index=df.index,
        )
        if sports := c.get("sports"):
            rows &= df["sport"].isin(sports)
        if not rows.any():
            continue
        for col in _LINEAR_POWER_COLUMNS:
            if col in df.columns:
                df.loc[rows, col] = pd.to_numeric(df.loc[rows, col], errors="coerce") * factor
        if "tss" in df.columns:
            df.loc[rows, "tss"] = pd.to_numeric(df.loc[rows, "tss"], errors="coerce") * factor**2
    return df


def load_manual_races(config: Config | None = None) -> dict:
    """User-authored race results / names / leg splits not auto-detected.

    Lives at ``<data_dir>/manual_races.json`` — merged over auto-detection by date.
    """
    config = config or get_config()
    return load_json(config.data_dir / "manual_races.json", default={})


def _merge_manual_metrics(settings: dict, manual: dict) -> dict:
    """Overlay manual values onto fetched settings. Manual wins (it's the value
    the athlete explicitly supplied); connector nulls never clobber it."""
    if not manual:
        return settings
    out = dict(settings)
    overridden: list[str] = []
    for key, val in manual.items():
        if key.startswith("_"):
            continue
        if isinstance(val, dict):
            merged = dict(out.get(key) or {})
            given = {k: v for k, v in val.items() if v is not None}
            merged.update(given)
            out[key] = merged
            overridden += [f"{key}.{k}" for k in given]
        elif val is not None:
            out[key] = val
            overridden.append(key)
    # Which values the athlete supplied, so the profile can say so instead of
    # crediting them to the connector they replaced.
    out["_manual_keys"] = sorted(set(overridden))
    return out


def data_as_of(activities: pd.DataFrame, wellness: pd.DataFrame) -> dt.date:
    """The most recent date present in the data; used as 'now' for determinism."""
    candidates: list[dt.date] = []
    if not activities.empty:
        candidates.append(max(activities["date"]))
    if not wellness.empty:
        candidates.append(max(wellness["date"]))
    return max(candidates) if candidates else dt.date.today()
