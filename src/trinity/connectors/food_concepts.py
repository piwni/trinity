"""Concept groups — the step between "232 unmapped products" and a usable mapping.

Matching products to reference foods one by one is the wrong unit of work. Forty
non-alcoholic beers are ONE food question, not forty, and answering it forty
times invites forty different answers. So products are grouped into concepts
first, each concept is matched once, and the decision propagates to every product
in the group.

The division of labour from :mod:`trinity.connectors.food_mappings` holds here
too. Grouping and phrasing the search query is judgment: it is recorded in
``<data_dir>/food_concepts.json`` where it can be reviewed and revised. What this
module does is mechanical — run the searches, gate them on relevance, and check
that the chosen record actually resolves to numbers.

Rerunnable by design: the food log gains new products constantly, and the point
of keeping this in the tool rather than in a throwaway script is that next
quarter's products go through the same path as this quarter's.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from trinity.config import Config, get_config
from trinity.connectors.food_mappings import Mapping
from trinity.connectors.fooddata import FoodDataClient, per_100g
from trinity.storage.io import load_json, save_json

# How far down the ranked candidates to keep looking. Not one winner: Foundation
# records regularly 404 on the detail endpoint, and a concept lost that way would
# surface as a nutrient gap rather than as the retrieval failure it actually is.
SHORTLIST_DEPTH = 6


@dataclass(frozen=True)
class Concept:
    """One food question, and every logged product that asks it.

    ``must`` is the relevance gate — substrings the USDA description has to
    contain. Without it the search happily returns a food that is well measured
    but wrong, which is far more damaging than no match at all.

    ``pin`` short-circuits the search with a reviewed fdc_id, for the cases where
    the ranking cannot be fixed by phrasing alone.
    """

    key: str
    queries: tuple[str, ...]
    must: tuple[str, ...]
    confidence: str
    products: tuple[str, ...]
    note: str | None = None
    pin: int | None = None


def concepts_path(config: Config | None = None):
    return (config or get_config()).data_dir / "food_concepts.json"


def load_concepts(config: Config | None = None) -> dict[str, Concept]:
    doc = load_json(concepts_path(config), default={}) or {}
    out: dict[str, Concept] = {}
    for key, c in (doc.get("concepts") or {}).items():
        query = c.get("query") or []
        out[key] = Concept(
            key=key,
            # A concept may carry several phrasings; the first that returns an
            # eligible candidate wins, so a bad phrasing costs a query, not a match.
            queries=tuple([query] if isinstance(query, str) else query),
            must=tuple(c.get("must") or ()),
            confidence=str(c.get("confidence", "low")),
            products=tuple(c.get("products") or ()),
            note=c.get("note"),
            pin=None if c.get("pin") is None else int(c["pin"]),
        )
    return out


def save_concepts(concepts: dict[str, Concept], config: Config | None = None):
    doc = {
        "_comment": (
            "Food concepts: groups of logged products that pose the same food "
            "question, plus how to find that food in USDA FoodData Central. "
            "`must` gates search results on name relevance -- without it the "
            "search returns whatever is best measured, not what was eaten. "
            "`pin` forces a reviewed fdc_id. Confidence 'low' is recorded but "
            "never applied. Rerun `trinity food-map` after editing."
        ),
        "concepts": {
            key: {
                "query": list(c.queries),
                **({"must": list(c.must)} if c.must else {}),
                "confidence": c.confidence,
                **({"pin": c.pin} if c.pin is not None else {}),
                **({"note": c.note} if c.note else {}),
                "products": list(c.products),
            }
            for key, c in sorted(concepts.items())
        },
    }
    return save_json(concepts_path(config), doc)


@dataclass(frozen=True)
class Resolution:
    mappings: dict[str, Mapping]
    unmatched: list[str]
    skipped: list[tuple[str, str]]
    chosen: dict[str, int]           # concept key -> the fdc_id it settled on


def resolve(
    concepts: dict[str, Concept] | None = None,
    client: FoodDataClient | None = None,
    *,
    repin: bool = False,
) -> Resolution:
    """Search out a reference food per concept and expand it over its products.

    A concept is only converted once a record has been fetched AND shown to carry
    values, so the failure modes that matter — a 404 on the detail endpoint, a
    record with an empty nutrient panel — surface here rather than becoming
    silent gaps at import time.

    ``chosen`` exists so the caller can pin the result back. FDC's search
    ordering is not stable between runs: an unpinned rerun of these same concepts
    swapped 11 records, turning egg white into whole egg (a fix) and rice into
    rice FLOUR (a regression) purely by luck of ranking. Re-deciding a settled
    question every run is not reproducibility, so pins are the intended end
    state and ``repin`` is the deliberate way to reopen one.
    """
    concepts = load_concepts() if concepts is None else concepts
    client = client or FoodDataClient()

    mappings: dict[str, Mapping] = {}
    unmatched: list[str] = []
    skipped: list[tuple[str, str]] = []
    chosen: dict[str, int] = {}

    for key, concept in concepts.items():
        if repin:
            concept = replace(concept, pin=None)
        food = _first_usable(concept, client)
        if food is None:
            skipped.append((key, "no retrievable record with values"))
            unmatched.extend(concept.products)
            continue
        if concept.confidence not in ("high", "medium"):
            # Recorded anyway: a visible weak match invites correction, whereas a
            # dropped one just looks like a product nobody got round to.
            skipped.append((key, concept.note or "match too weak to apply"))
            unmatched.extend(concept.products)

        chosen[key] = int(food["fdcId"])
        for product in concept.products:
            mappings[product] = Mapping(
                product=product,
                confidence=concept.confidence,
                fdc_id=int(food["fdcId"]),
                fdc_description=str(food.get("description", "")),
                note=concept.note,
            )
    return Resolution(mappings, sorted(set(unmatched)), skipped, chosen)


def _first_usable(concept: Concept, client: FoodDataClient) -> dict | None:
    """Walk the shortlist until a record both fetches and carries numbers."""
    for fdc_id in _shortlist(concept, client):
        food = client.food(fdc_id)
        if food and per_100g(food):
            return food
    return None


def _shortlist(concept: Concept, client: FoodDataClient) -> list[int]:
    if concept.pin is not None:
        return [concept.pin]
    seen: list[int] = []
    for query in concept.queries:
        for candidate in client.search(query, limit=SHORTLIST_DEPTH, must=concept.must):
            if candidate.fdc_id not in seen:
                seen.append(candidate.fdc_id)
        if seen:
            break        # a later phrasing is a fallback, not extra breadth
    return seen[:SHORTLIST_DEPTH]
