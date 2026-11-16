"""Product -> reference-food mappings, and the per-100 g table they resolve to.

The food log names products in Polish, often with a brand ("Kefir naturalny OSM
SKAŁA 500g. 1,5%tł"). Turning that into a food-composition record is a fuzzy
language task, which is why the MATCH is made by a human or agent rather than by
code, and then written down here where it can be reviewed and revised.

The split matters. Matching a name is judgment and is recorded with its
reasoning; the nutrient numbers are then looked up, never invented. That keeps
every enriched figure traceable to an FDC food id.

Lives at ``<data_dir>/food_mappings.json``, outside ``raw/``, so re-importing
exports never touches it.
"""

from __future__ import annotations

from dataclasses import dataclass

from trinity.config import Config, get_config
from trinity.connectors.fooddata import FoodDataClient, per_100g
from trinity.storage.io import load_json, save_json

# A match the agent was not confident about still gets used, but is surfaced so a
# reviewer can see what rests on a guess.
CONFIDENCE_LEVELS = ("high", "medium", "low")


@dataclass(frozen=True)
class Mapping:
    """Where a product's missing nutrients should be read from.

    Two sources, in order of preference:

    * ``like_product`` — another product in the athlete's OWN log that does carry
      the values. The gaps are per database entry, not per food, so plain milk
      often has calcium while kefir does not. Same database and same conventions,
      so this beats a foreign table for local products, and it needs no network.
    * ``fdc_id`` — a USDA FoodData Central record, for foods the log never
      populates anywhere.
    """

    product: str
    confidence: str
    fdc_id: int | None = None
    fdc_description: str = ""
    like_product: str | None = None
    note: str | None = None

    @property
    def trusted(self) -> bool:
        return self.confidence in ("high", "medium")


def mappings_path(config: Config | None = None):
    return (config or get_config()).data_dir / "food_mappings.json"


def load_mappings(config: Config | None = None) -> dict[str, Mapping]:
    doc = load_json(mappings_path(config), default={}) or {}
    out: dict[str, Mapping] = {}
    for product, m in (doc.get("mappings") or {}).items():
        if m.get("fdc_id") is None and not m.get("like_product"):
            continue
        out[product.strip()] = Mapping(
            product=product.strip(),
            fdc_id=None if m.get("fdc_id") is None else int(m["fdc_id"]),
            fdc_description=str(m.get("fdc_description", "")),
            like_product=(m.get("like_product") or None),
            confidence=str(m.get("confidence", "low")),
            note=m.get("note"),
        )
    return out


def save_mappings(mappings: dict[str, Mapping], *, unmatched: list[str] | None = None,
                  config: Config | None = None):
    doc = {
        "_comment": (
            "Product -> USDA FoodData Central mappings. The MATCH is judgment "
            "(recorded here, reviewable); the NUMBERS come from the FDC record "
            "and are never authored by hand. Delete an entry to withdraw it. "
            "`unmatched` lists products deliberately left alone -- composite "
            "dishes and products whose micronutrients really are ~zero."
        ),
        "mappings": {
            p: {
                **({"like_product": m.like_product} if m.like_product else {}),
                **({"fdc_id": m.fdc_id, "fdc_description": m.fdc_description}
                   if m.fdc_id is not None else {}),
                "confidence": m.confidence,
                **({"note": m.note} if m.note else {}),
            }
            for p, m in sorted(mappings.items())
        },
        "unmatched": sorted(unmatched or []),
    }
    return save_json(mappings_path(config), doc)


def build_reference(
    mappings: dict[str, Mapping] | None = None,
    client: FoodDataClient | None = None,
    log_composition: dict[str, dict[str, float]] | None = None,
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Resolve mappings into ``{product: {field: per-100 g value}}``.

    Returns the table plus the products that could not be resolved — a
    low-confidence match, a food not in the cache while offline, or a
    ``like_product`` that is itself missing data. Those are reported rather than
    filled with anything, so a gap in the sources degrades to "no enrichment"
    instead of to wrong numbers.
    """
    mappings = load_mappings() if mappings is None else mappings
    client = client or FoodDataClient()
    log_composition = log_composition or {}
    table: dict[str, dict[str, float]] = {}
    unresolved: list[str] = []
    for product, m in mappings.items():
        if not m.trusted:
            unresolved.append(product)
            continue
        if m.like_product:
            values = log_composition.get(m.like_product.strip(), {})
        else:
            food = client.food(m.fdc_id)
            values = per_100g(food) if food else {}
        if values:
            table[product] = values
        else:
            unresolved.append(product)
    return table, unresolved
