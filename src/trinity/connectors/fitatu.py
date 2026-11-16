"""Fitatu meal-plan ingestion.

Fitatu (a meal-planning / nutrition app) exports one CSV row *per logged product
per meal*. This connector flattens those rows into one row *per day* of totals,
normalizes the column names, and writes them to the partitioned raw store at
``data/raw/fitatu/nutrition/{year}/data.csv`` — the same year-partitioned shape
as activities/wellness, so ``loaders.load_nutrition`` can read it uniformly.

Only macro/micro *sums* are derived here (deterministic math). Judgment about
whether the diet is adequate lives in ``analysis/nutrition.py`` + the
nutritionist skill.
"""

from __future__ import annotations

import datetime as dt
import glob as globlib
import re
from pathlib import Path

import pandas as pd

from trinity.storage.io import read_csv, write_csv
from trinity.storage.paths import raw_partition_path

# Output field -> source column header in the Fitatu export. Source headers are
# matched case-insensitively after stripping, so minor export changes still map.
FIELD_MAP: dict[str, str] = {
    "calories_kcal": "calories (kcal)",
    "protein_g": "Protein (g)",
    "fat_g": "Fats (g)",
    "saturated_g": "Saturated (g)",
    "carbs_g": "Carbohydrates (g)",
    "sugars_g": "Sugars (g)",
    "fibre_g": "Fibre (g)",
    "sodium_mg": "Sodium (mg)",
    "caffeine_mg": "Caffeine (mg)",
    "iron_mg": "Iron (mg)",
    "calcium_mg": "Calcium (mg)",
    "magnesium_mg": "Magnesium (mg)",
    "potassium_mg": "Potassium (mg)",
    "zinc_mg": "Zinc (mg)",
    "vitamin_c_mg": "Vitamin C (mg)",
    "vitamin_d_ug": "Vitamin D (ug)",
    "omega3_g": "Omega 3 fatty acid (g)",
    "omega6_g": "Omega 6 fatty acid (g)",
    "cholesterol_mg": "Cholesterol (mg)",
    "salt_g": "Salt (g)",
}

NUTRIENT_FIELDS: list[str] = list(FIELD_MAP.keys())

# Fitatu's food database leaves most micronutrient cells blank per product, and a
# blank is NOT a zero. Summing blanks-as-zeros silently understates a day's total,
# which previously read downstream as a deficiency. For every nutrient we also
# carry `<field>_n`: how many of that day's logged items actually supplied a value
# for it. `<field>_n / item_count` is the only honest coverage measure — a day-level
# "is the total non-zero" test always passes, because one item out of ~17 is enough.
COVERAGE_FIELDS: list[str] = [f"{f}_n" for f in NUTRIENT_FIELDS]

# Items whose value came from a reference food table instead of the log (see
# `enrich_rows`). Kept strictly disjoint from `<field>_n` so the digest can say
# how much of a total the athlete actually logged versus how much we supplied.
IMPUTED_FIELDS: list[str] = [f"{f}_i" for f in NUTRIENT_FIELDS]


def coverage_field(nutrient: str) -> str:
    """Name of the item-count column that backs ``nutrient``."""
    return f"{nutrient}_n"


def imputed_field(nutrient: str) -> str:
    """Name of the item-count column for reference-supplied values."""
    return f"{nutrient}_i"


OUTPUT_COLUMNS: list[str] = [
    "date", *NUTRIENT_FIELDS, *COVERAGE_FIELDS, *IMPUTED_FIELDS,
    "meal_count", "item_count", "alcoholic_drinks",
]

# Rough alcohol proxy: the Fitatu export has no alcohol-grams column, so we count
# beverage rows whose name looks alcoholic UNLESS it is explicitly alcohol-free.
_ALCOHOL_RE = re.compile(r"\b(beer|piwo|wine|wino|vodka|w[oó]dka|whisky|whiskey|rum|gin|cydr|cider|prosecco|champagne|szampan|likier|liqueur)\b", re.IGNORECASE)
_NON_ALCOHOLIC_RE = re.compile(r"non[- ]?alcoholic|bezalkohol|alcohol[- ]?free|0[.,]0\s*%|0%", re.IGNORECASE)


def _is_alcoholic(name: str) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    if _NON_ALCOHOLIC_RE.search(name):
        return False
    return bool(_ALCOHOL_RE.search(name))


def _resolve_columns(df: pd.DataFrame) -> dict[str, str]:
    """Map normalized header (lower/stripped) -> actual column name in df."""
    return {str(c).strip().lower(): c for c in df.columns}


def _presence_field(nutrient: str) -> str:
    return f"_has_{nutrient}"


def _imputed_flag(nutrient: str) -> str:
    return f"_imp_{nutrient}"


def parse_export(path: Path) -> pd.DataFrame:
    """Read one Fitatu export CSV into a normalized per-row frame.

    Returns columns: ``date``, ``meal``, ``product``, every field in
    :data:`NUTRIENT_FIELDS` (numeric, blanks coerced to 0) and a boolean
    ``_has_<field>`` per nutrient recording whether *this item* actually supplied
    a value. A logged 0 counts as present; only a blank/unparseable cell is
    absent. :func:`daily_totals` turns those into ``<field>_n`` item counts.
    """
    raw = pd.read_csv(path)
    if raw.empty:
        cols = ["date", "meal", "product", "grams", *NUTRIENT_FIELDS]
        return pd.DataFrame(columns=[
            *cols,
            *(_presence_field(f) for f in NUTRIENT_FIELDS),
            *(_imputed_flag(f) for f in NUTRIENT_FIELDS),
        ])

    cols = _resolve_columns(raw)
    out = pd.DataFrame()

    date_col = cols.get("date")
    if date_col is None:
        raise ValueError(f"{path}: no 'Date' column found in Fitatu export")
    out["date"] = pd.to_datetime(raw[date_col]).dt.date
    out["meal"] = raw[cols["meal"]].astype(str) if "meal" in cols else ""
    out["product"] = raw[cols["products and dishes"]].astype(str) if "products and dishes" in cols else ""
    # Portion mass, needed to scale a per-100 g reference onto this item.
    grams_col = cols.get("quantity (g)")
    out["grams"] = (
        pd.to_numeric(raw[grams_col], errors="coerce") if grams_col is not None else pd.NA
    )

    for field, header in FIELD_MAP.items():
        src = cols.get(header.strip().lower())
        if src is None:
            # Column absent from the export entirely -> no item can supply it.
            out[field] = 0.0
            out[_presence_field(field)] = False
        else:
            vals = pd.to_numeric(raw[src], errors="coerce")
            out[field] = vals.fillna(0.0)
            out[_presence_field(field)] = vals.notna()
        out[_imputed_flag(field)] = False
    return out


def enrich_rows(
    rows: pd.DataFrame,
    reference: dict[str, dict[str, float]],
    *,
    zero_override_floor: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Fill blank nutrient cells from a per-100 g reference table.

    ``reference`` maps a product name to its composition per 100 g, resolved from
    a food-composition database via a recorded product -> food mapping. Values are
    scaled by the item's portion mass.

    Two cases are filled, and only these two:

    * The log left the cell BLANK. The value was never counted; we supply it.
    * The log recorded a ZERO that the reference contradicts by at least
      ``zero_override_floor[nutrient]`` (in the nutrient's own unit, for this
      portion). A wrong zero is worse than a blank: the blank is visible to the
      coverage machinery, whereas a zero is indistinguishable from a real
      measurement, so it silently drags a mean down. Grated hard cheese logged at
      0 mg calcium is the archetype.

    A logged non-zero value is never touched — the athlete's own record wins over
    a generic table. Rows with no portion mass are left alone, since a per-100 g
    figure cannot be scaled without one.
    """
    if rows.empty or not reference:
        return rows
    floors = zero_override_floor or {}
    rows = rows.copy()
    grams = pd.to_numeric(rows.get("grams"), errors="coerce")
    per_100g = rows["product"].astype(str).str.strip().map(reference)

    for field in NUTRIENT_FIELDS:
        # An unmapped product maps to NaN, which is truthy — hence the explicit
        # dict check rather than `d or {}`.
        ref = per_100g.map(
            lambda d, f=field: d.get(f) if isinstance(d, dict) else None
        ).astype(float)
        amount = ref * grams / 100.0
        usable = amount.notna() & grams.notna()

        blank = usable & ~rows[_presence_field(field)].astype(bool)
        floor = floors.get(field)
        wrong_zero = (
            usable & rows[_presence_field(field)].astype(bool)
            & (rows[field] == 0) & (amount >= floor)
            if floor is not None
            else pd.Series(False, index=rows.index)
        )

        fill = blank | wrong_zero
        if not fill.any():
            continue
        rows.loc[fill, field] = amount[fill]
        # Disjoint by construction: an imputed item is no longer a logged one, so
        # `<field>_n` keeps meaning "the athlete's own data".
        rows.loc[fill, _presence_field(field)] = False
        rows.loc[fill, _imputed_flag(field)] = True
    return rows


def log_composition(rows: pd.DataFrame, *, min_grams: float = 50.0) -> dict[str, dict[str, float]]:
    """Per-100 g composition of every product the log itself has data for.

    The food app's gaps are per PRODUCT ENTRY, not per food: plain milk carries
    calcium while kefir does not, though they are nutritionally near-identical.
    That makes the athlete's own log a reference source in its own right, and a
    better one than a foreign food table for local products — same database, same
    conventions, just a populated row.

    Only nutrients present on EVERY logged row of a product are derived, so a
    product logged sometimes with and sometimes without a value cannot produce a
    figure diluted by the blanks. Products with negligible total mass are skipped,
    since dividing by a few grams turns rounding into a wild per-100 g number.
    """
    if rows.empty:
        return {}
    grams = pd.to_numeric(rows.get("grams"), errors="coerce")
    work = rows.assign(_g=grams, _p=rows["product"].astype(str).str.strip())
    work = work[work["_g"] > 0]
    out: dict[str, dict[str, float]] = {}
    for product, grp in work.groupby("_p"):
        total_g = float(grp["_g"].sum())
        if total_g < min_grams:
            continue
        values: dict[str, float] = {}
        for field in NUTRIENT_FIELDS:
            presence = _presence_field(field)
            if presence in grp.columns and not grp[presence].astype(bool).all():
                continue
            values[field] = float(pd.to_numeric(grp[field], errors="coerce").sum()) / total_g * 100
        if values:
            out[product] = values
    return out


def blank_products(rows: pd.DataFrame, nutrient: str) -> pd.DataFrame:
    """Products that never supply ``nutrient``, ranked by logged mass.

    Mass, not frequency, is the right ranking: closing the biggest hole in a
    nutrient total means finding the products the athlete eats a lot OF, not the
    ones that appear on the most lines. A spice logged 60 times is irrelevant next
    to a litre of kefir a day.
    """
    if rows.empty or nutrient not in rows.columns:
        return pd.DataFrame(columns=["product", "items", "grams"])
    present = rows.get(_presence_field(nutrient))
    if present is None:
        present = rows[nutrient].notna()
    blank = rows[~present.astype(bool)].copy()
    blank["product"] = blank["product"].astype(str).str.strip()
    blank["grams"] = pd.to_numeric(blank.get("grams"), errors="coerce").fillna(0.0)
    out = (blank.groupby("product")
           .agg(items=("product", "size"), grams=("grams", "sum"))
           .reset_index()
           .sort_values("grams", ascending=False)
           .reset_index(drop=True))
    return out


def daily_totals(rows: pd.DataFrame) -> pd.DataFrame:
    """Aggregate normalized per-row data into one row per day."""
    if rows.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    rows = rows.copy()
    rows["_alcoholic"] = rows["product"].map(_is_alcoholic)

    agg: dict[str, object] = {f: (f, "sum") for f in NUTRIENT_FIELDS}
    for f in NUTRIENT_FIELDS:
        presence = _presence_field(f)
        if presence not in rows.columns:  # frame built by hand / older caller
            rows[presence] = rows[f].notna()
        imputed = _imputed_flag(f)
        if imputed not in rows.columns:
            rows[imputed] = False
        agg[coverage_field(f)] = (presence, "sum")
        agg[imputed_field(f)] = (imputed, "sum")
    agg["meal_count"] = ("meal", lambda s: s[s.astype(str).str.strip() != ""].nunique())
    agg["item_count"] = ("product", "count")
    agg["alcoholic_drinks"] = ("_alcoholic", "sum")

    daily = rows.groupby("date").agg(**agg).reset_index()
    for f in NUTRIENT_FIELDS:
        daily[f] = daily[f].round(2)
        daily[coverage_field(f)] = daily[coverage_field(f)].astype(int)
        daily[imputed_field(f)] = daily[imputed_field(f)].astype(int)
    daily["alcoholic_drinks"] = daily["alcoholic_drinks"].astype(int)
    daily["meal_count"] = daily["meal_count"].astype(int)
    daily["item_count"] = daily["item_count"].astype(int)
    return daily[OUTPUT_COLUMNS].sort_values("date").reset_index(drop=True)


def import_exports(
    paths: list[Path],
    reference: dict[str, dict[str, float]] | None = None,
    *,
    zero_override_floor: dict[str, float] | None = None,
) -> dict:
    """Parse + aggregate Fitatu exports and MERGE them into the year-partitioned
    daily store.

    Re-import is genuinely incremental: each file is aggregated to daily totals
    first (so a day's products within a file are summed once), then overlapping
    dates *across* files or against the existing on-disk partition are resolved
    by keep-last (incoming supersedes existing) — never summed. Importing a new
    month therefore extends the store without dropping or double-counting prior
    months.
    """
    # Aggregate each file independently so overlapping calendar days across
    # exports are deduped (keep-last), not double-counted (B2). Enrichment runs
    # per ITEM, before aggregation — a per-100 g reference can only be scaled
    # while the portion mass is still attached to its row.
    per_file = [
        daily_totals(
            enrich_rows(
                parse_export(p), reference or {}, zero_override_floor=zero_override_floor
            )
        )
        for p in paths
    ]
    per_file = [d for d in per_file if not d.empty]
    if not per_file:
        return {"files": len(paths), "days": 0, "years": [], "window": None}

    incoming = pd.concat(per_file, ignore_index=True)
    incoming = incoming.drop_duplicates(subset="date", keep="last")  # later file wins

    years_written: list[int] = []
    for year, part in incoming.groupby(incoming["date"].map(lambda d: d.year)):
        out = raw_partition_path("fitatu", "nutrition", int(year))
        existing = read_csv(out)
        if not existing.empty:
            existing["date"] = pd.to_datetime(existing["date"]).dt.date
            # reindex, not hard-index: partitions written before per-nutrient
            # coverage existed have no `<field>_n` columns. They become NaN, which
            # the analysis reads as "coverage unknown" rather than "zero coverage".
            existing = existing.reindex(columns=OUTPUT_COLUMNS)
            merged = pd.concat([existing, part[OUTPUT_COLUMNS]], ignore_index=True)
            merged = merged.drop_duplicates(subset="date", keep="last")  # incoming supersedes disk
        else:
            merged = part
        merged = merged.sort_values("date").reset_index(drop=True)
        write_csv(out, merged)
        years_written.append(int(year))

    return {
        "files": len(paths),
        "days": int(len(incoming)),
        "years": sorted(years_written),
        "window": (str(incoming["date"].min()), str(incoming["date"].max())),
    }


def resolve_inputs(spec: str | None, inbox: Path) -> list[Path]:
    """Resolve the ``--from`` argument into a sorted list of CSV paths.

    Accepts a single file, a directory (all ``*.csv`` inside), or a glob. When
    ``spec`` is ``None`` the conventional drop folder ``inbox`` is scanned.
    """
    if spec is None:
        return sorted(inbox.glob("*.csv")) if inbox.exists() else []
    p = Path(spec).expanduser()
    if p.is_dir():
        return sorted(p.glob("*.csv"))
    if p.exists():
        return [p]
    # Treat as a glob (relative or absolute); stdlib glob handles both.
    return sorted(Path(m) for m in globlib.glob(str(p)))


def latest_export_date(daily: pd.DataFrame) -> dt.date | None:
    if daily.empty:
        return None
    return max(daily["date"])
