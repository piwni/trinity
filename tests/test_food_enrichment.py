"""Filling blank food-log cells from a reference composition table.

The guarantee under test throughout: an enriched total must never be
indistinguishable from a logged one. Every fill is counted separately, so the
digest can discount a number we supplied ourselves.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from trinity.analysis import nutrition as nut
from trinity.connectors import fitatu
from trinity.connectors.food_mappings import Mapping, build_reference
from trinity.connectors.fooddata import FoodDataClient, per_100g
from trinity.models.nutrition_digest import MicronutrientStatus

# Kefir is the real case this was built for: ~430 g/day logged with the calcium
# cell blank, which read downstream as a calcium deficiency.
KEFIR_PER_100G = {"calcium_mg": 120.0, "protein_g": 3.3}
FLOORS = {"calcium_mg": 20.0}


def _rows(*items: tuple[str, float, float | None]) -> pd.DataFrame:
    """One row per (product, grams, logged calcium); None means a blank cell."""
    out = []
    for product, grams, calcium in items:
        row = {c: 0.0 for c in fitatu.NUTRIENT_FIELDS}
        row.update({f"_has_{c}": False for c in fitatu.NUTRIENT_FIELDS})
        row.update({f"_imp_{c}": False for c in fitatu.NUTRIENT_FIELDS})
        row.update({"date": dt.date(2026, 8, 1), "meal": "Breakfast",
                    "product": product, "grams": grams})
        row["calcium_mg"] = 0.0 if calcium is None else calcium
        row["_has_calcium_mg"] = calcium is not None
        out.append(row)
    return pd.DataFrame(out)


def _enriched(*items, reference=None):
    return fitatu.enrich_rows(
        _rows(*items), reference or {"Kefir": KEFIR_PER_100G}, zero_override_floor=FLOORS,
    )


# --- filling blanks ---------------------------------------------------------
def test_blank_cell_is_filled_and_scaled_by_portion():
    out = _enriched(("Kefir", 400.0, None)).iloc[0]
    assert out["calcium_mg"] == pytest.approx(480.0)  # 120 mg/100 g x 4
    assert out["_imp_calcium_mg"]


def test_logged_value_always_beats_the_reference_table():
    """The athlete's own record is the better evidence, even when a generic table
    disagrees. Enrichment closes gaps; it must never quietly restate the log."""
    out = _enriched(("Kefir", 400.0, 999.0)).iloc[0]
    assert out["calcium_mg"] == 999.0
    assert not out["_imp_calcium_mg"]
    assert out["_has_calcium_mg"]


def test_unmapped_product_is_left_blank_not_guessed():
    out = _enriched(("Pierogi z serem", 300.0, None)).iloc[0]
    assert out["calcium_mg"] == 0.0
    assert not out["_imp_calcium_mg"]
    assert not out["_has_calcium_mg"], "still an honest blank, not a value"


def test_missing_portion_mass_blocks_enrichment():
    """A per-100 g figure is unusable without a portion. Better to stay blank than
    to invent a serving size."""
    out = _enriched(("Kefir", float("nan"), None)).iloc[0]
    assert out["calcium_mg"] == 0.0
    assert not out["_imp_calcium_mg"]


# --- wrong zeros ------------------------------------------------------------
def test_zero_contradicted_by_the_reference_is_replaced():
    """A logged 0 is more dangerous than a blank: coverage tracking can see a
    blank, but a zero looks like a measurement and drags the mean down."""
    out = _enriched(("Kefir", 400.0, 0.0)).iloc[0]
    assert out["calcium_mg"] == pytest.approx(480.0)
    assert out["_imp_calcium_mg"]
    assert not out["_has_calcium_mg"], "an overridden zero is no longer the athlete's datum"


def test_zero_below_the_floor_is_left_alone():
    """10 g of kefir implies 12 mg of calcium, under the 20 mg floor. Rounding
    noise in a reference table must not be able to rewrite the log."""
    out = _enriched(("Kefir", 10.0, 0.0)).iloc[0]
    assert out["calcium_mg"] == 0.0
    assert not out["_imp_calcium_mg"]


def test_nutrient_without_a_floor_keeps_its_zeros():
    ref = {"Kefir": {"protein_g": 3.3}}
    rows = _rows(("Kefir", 400.0, None))
    rows["protein_g"], rows["_has_protein_g"] = 0.0, True
    out = fitatu.enrich_rows(rows, ref, zero_override_floor=FLOORS).iloc[0]
    assert out["protein_g"] == 0.0, "no floor configured -> zeros are treated as real"


# --- aggregation ------------------------------------------------------------
def test_daily_totals_keep_logged_and_imputed_counts_disjoint():
    rows = _enriched(("Kefir", 400.0, None), ("Kefir", 200.0, 50.0))
    day = fitatu.daily_totals(rows).iloc[0]
    assert day["calcium_mg"] == pytest.approx(530.0)  # 480 imputed + 50 logged
    assert day["calcium_mg_n"] == 1
    assert day["calcium_mg_i"] == 1
    assert day["calcium_mg_n"] + day["calcium_mg_i"] == day["item_count"]


def test_coverage_counts_imputed_items_but_reports_their_share():
    """An imputed value makes the total trustworthy enough to judge, so it counts
    toward coverage — but the digest has to be able to say how much of the number
    it supplied itself."""
    n = pd.DataFrame([{"item_count": 10, "calcium_mg_n": 3, "calcium_mg_i": 5}])
    coverage, days, imputed_share = nut._item_coverage(n, "calcium_mg")
    assert coverage == pytest.approx(0.8)
    assert days == 1
    assert imputed_share == pytest.approx(5 / 8)


def test_store_without_imputed_columns_reports_nothing_imputed():
    """Partitions written before enrichment existed must read as fully logged, not
    crash and not be silently discounted."""
    n = pd.DataFrame([{"item_count": 10, "calcium_mg_n": 8}])
    coverage, _, imputed_share = nut._item_coverage(n, "calcium_mg")
    assert coverage == pytest.approx(0.8)
    assert imputed_share == 0.0


# --- what a heavily imputed number is allowed to claim ----------------------
def _flag(nutrient: str, mean: float, *, logged: int, imputed: int, items: int = 10):
    days = [dt.date(2026, 7, 1) + dt.timedelta(days=i) for i in range(40)]
    rows = []
    for d in days:
        row = {c: 0.0 for c in fitatu.NUTRIENT_FIELDS}
        row.update({fitatu.coverage_field(c): items for c in fitatu.NUTRIENT_FIELDS})
        row.update({fitatu.imputed_field(c): 0 for c in fitatu.NUTRIENT_FIELDS})
        row.update({"date": d, "meal_count": 4, "item_count": items, "alcoholic_drinks": 0,
                    "calories_kcal": 3000.0, nutrient: mean,
                    fitatu.coverage_field(nutrient): logged,
                    fitatu.imputed_field(nutrient): imputed})
        rows.append(row)
    n = pd.DataFrame(rows)[fitatu.OUTPUT_COLUMNS]
    digest = nut.build_digest(n, pd.DataFrame(), pd.DataFrame())
    return next(f for f in digest.micronutrient_flags if f.nutrient == nutrient)


def test_mostly_imputed_shortfall_cannot_claim_a_deficiency():
    """Recommending a supplement on the strength of a number we largely looked up
    is the expensive error. The shortfall could just as easily be the imputation."""
    flag = _flag("calcium_mg", 400.0, logged=1, imputed=9)
    assert flag.flag is MicronutrientStatus.UNKNOWN
    assert not flag.reliable


def test_mostly_imputed_adequacy_still_counts():
    """The asymmetry is the point: clearing the target by this margin would need
    the reference tables to be wrong by tens of percent, all one way."""
    flag = _flag("calcium_mg", 1600.0, logged=1, imputed=9)
    assert flag.flag is MicronutrientStatus.GREEN
    assert flag.imputed_pct == pytest.approx(90.0)


def test_a_logged_shortfall_is_still_reported():
    """The gate must not swallow real evidence — a well-logged deficit still flags."""
    flag = _flag("calcium_mg", 400.0, logged=9, imputed=1)
    assert flag.flag is MicronutrientStatus.RED


def test_intake_over_the_upper_limit_is_flagged_not_called_adequate():
    """Sodium has a ceiling, not just a floor. Checking only "is it enough" read
    3700 mg/day — well past the 2300 mg upper limit — as green."""
    assert _flag("sodium_mg", 3700.0, logged=9, imputed=1).flag is MicronutrientStatus.RED
    assert _flag("sodium_mg", 2500.0, logged=9, imputed=1).flag is MicronutrientStatus.AMBER
    assert _flag("sodium_mg", 1600.0, logged=9, imputed=1).flag is MicronutrientStatus.GREEN


# --- gap reporting ----------------------------------------------------------
def test_gaps_rank_by_mass_not_by_how_often_a_product_appears():
    rows = _rows(*([("Spice", 2.0, None)] * 20), ("Kefir", 400.0, None))
    gaps = fitatu.blank_products(rows, "calcium_mg")
    assert list(gaps["product"]) == ["Kefir", "Spice"]
    assert gaps.iloc[0]["grams"] == 400.0
    assert gaps.iloc[1]["items"] == 20


# --- reference lookup -------------------------------------------------------
def test_nutrient_in_an_unexpected_unit_is_skipped_not_converted():
    """A mg/ug mix-up is a 1000x error. Dropping the value costs coverage; guessing
    the unit corrupts the total."""
    food = {"foodNutrients": [
        {"nutrient": {"id": 1087, "name": "Calcium, Ca", "unitName": "UG"}, "amount": 120.0},
        {"nutrient": {"id": 1089, "name": "Iron, Fe", "unitName": "MG"}, "amount": 1.5},
    ]}
    assert per_100g(food) == {"iron_mg": 1.5}


def test_search_results_are_read_despite_their_flatter_shape():
    """/foods/search flattens the nutrient that /food/{id} nests, and nothing in
    the payload says which you have. Reading only the nested form makes every
    search hit look nutrient-free, so candidate ranking silently degrades to
    "whatever came back first"."""
    search_shape = {"foodNutrients": [
        {"nutrientId": 1087, "nutrientName": "Calcium, Ca", "unitName": "MG", "value": 120.0},
        {"nutrientId": 1089, "nutrientName": "Iron, Fe", "unitName": "MG", "value": 1.5},
    ]}
    detail_shape = {"foodNutrients": [
        {"nutrient": {"id": 1087, "unitName": "MG"}, "amount": 120.0},
        {"nutrient": {"id": 1089, "unitName": "MG"}, "amount": 1.5},
    ]}
    assert per_100g(search_shape) == per_100g(detail_shape) == {
        "calcium_mg": 120.0, "iron_mg": 1.5}


def _fat(**by_id: float) -> dict:
    return {"foodNutrients": [
        {"nutrient": {"id": int(i.lstrip("n")), "unitName": "g"}, "amount": v}
        for i, v in by_id.items()]}


def test_omega3_sums_the_individual_fatty_acids():
    """FDC has no "omega-3" nutrient, only the chains. EPA + DHA + ALA is how the
    total is derived everywhere else, so deriving it is honest arithmetic rather
    than imputation."""
    out = per_100g(_fat(n1404=0.235, n1278=0.532, n1272=0.864, n1280=0.061))
    assert out["omega3_g"] == pytest.approx(1.692)


def test_generic_and_specific_18_3_are_not_double_counted():
    """20 of the records in use report both "PUFA 18:3" and "18:3 n-3 (ALA)".
    Adding every id that looks like omega-3 would count ALA twice."""
    out = per_100g(_fat(n1270=0.235, n1404=0.235, n1278=0.532))
    assert out["omega3_g"] == pytest.approx(0.767)


def test_generic_18_3_has_any_reported_n6_removed():
    """When only the generic 18:3 is available it stands in for ALA, but it also
    bundles the n-6 GLA the record lists separately — which would otherwise be
    counted as omega-3 and again as omega-6."""
    out = per_100g(_fat(n1270=0.300, n1321=0.050, n1278=0.100))
    assert out["omega3_g"] == pytest.approx(0.350)
    assert out["omega6_g"] == pytest.approx(0.050)


def test_food_without_fatty_acid_data_reports_no_omega_at_all():
    """Absent must not become a confident zero — that is the whole failure this
    pipeline exists to avoid."""
    out = per_100g({"foodNutrients": [
        {"nutrient": {"id": 1087, "unitName": "MG"}, "amount": 120.0}]})
    assert "omega3_g" not in out
    assert "omega6_g" not in out


def test_search_drops_candidates_whose_name_does_not_match(monkeypatch):
    """Relevance has to gate the list before ranking does. Ranking on how complete
    a nutrient panel is picks the best-MEASURED food, not the right one — that is
    how a query for blueberries came back with beets."""
    client = FoodDataClient()
    monkeypatch.setattr(type(client), "online", property(lambda self: True))
    monkeypatch.setattr(client, "_search", lambda q, n: {"foods": [
        {"fdcId": 1, "description": "Beets, raw", "dataType": "Foundation",
         "foodNutrients": [{"nutrientId": 1087, "unitName": "MG", "value": 16.0}]},
        {"fdcId": 2, "description": "Blueberries, raw", "dataType": "SR Legacy",
         "foodNutrients": [{"nutrientId": 1087, "unitName": "MG", "value": 6.0}]},
    ]})

    assert [c.fdc_id for c in client.search("blueberries")] == [1, 2]
    assert [c.fdc_id for c in client.search("blueberries", must=("blueberr",))] == [2]


def test_low_confidence_mappings_are_reported_not_applied():
    guess = {"Racuchy": Mapping("Racuchy", 1, "Pancakes, plain", "low")}
    table, unresolved = build_reference(guess, client=FoodDataClient())
    assert table == {}
    assert unresolved == ["Racuchy"]


def test_uncached_food_offline_degrades_to_no_enrichment(tmp_path, monkeypatch):
    """Offline with a cold cache must yield "no reference available", never a
    partially-filled table that looks complete."""
    client = FoodDataClient()
    monkeypatch.setattr(client, "cache_dir", tmp_path)
    monkeypatch.setattr(type(client), "online", property(lambda self: False))
    assert client.food(999999) is None
    table, unresolved = build_reference(
        {"Kefir": Mapping("Kefir", 999999, "Milk, kefir", "high")}, client=client)
    assert table == {}
    assert unresolved == ["Kefir"]
