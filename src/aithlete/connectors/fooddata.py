"""USDA FoodData Central connector — reference nutrient composition per 100 g.

Fitatu's food database leaves most micronutrient cells blank, and a blank is not
a zero (see :mod:`aithlete.connectors.fitatu`). This connector supplies the
missing numbers from a citable source instead of a guess.

Division of labour, deliberately:

* This module NEVER decides which food a logged product is. It only fetches
  candidates and composition. Guessing a nutrient value is exactly the failure
  the coverage tracking exists to prevent, and a plausible-looking invented
  number is worse than an honest blank because nothing downstream can tell.
* The product -> food match is recorded in ``data/food_mappings.json`` by a
  human or agent and can be reviewed, corrected, or withdrawn per product.

Every fetched food is cached under ``data/raw/fooddata/foods/{fdc_id}.json``, so
a resolved mapping keeps working offline and the API is hit once per food ever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aithlete.config import Config, get_config
from aithlete.storage.io import load_json, save_json

# FoodData Central nutrient ids -> our field names. Units are FDC's own and are
# asserted on read rather than assumed, because a silent unit change (mg vs ug)
# would move a value by 1000x without tripping anything.
FDC_NUTRIENTS: dict[int, tuple[str, str]] = {
    1003: ("protein_g", "G"),
    1004: ("fat_g", "G"),
    1005: ("carbs_g", "G"),
    1008: ("calories_kcal", "KCAL"),
    1057: ("caffeine_mg", "MG"),
    1079: ("fibre_g", "G"),
    1087: ("calcium_mg", "MG"),
    1089: ("iron_mg", "MG"),
    1090: ("magnesium_mg", "MG"),
    1092: ("potassium_mg", "MG"),
    1093: ("sodium_mg", "MG"),
    1095: ("zinc_mg", "MG"),
    1114: ("vitamin_d_ug", "UG"),
    1162: ("vitamin_c_mg", "MG"),
    1253: ("cholesterol_mg", "MG"),
    1258: ("saturated_g", "G"),
    2000: ("sugars_g", "G"),
}

# omega3_g / omega6_g have no single FDC nutrient; they are sums over individual
# fatty acids, which is also how food tables derive them. The catch is that FDC
# reports some chains twice — a generic "PUFA 18:3" alongside the specific
# "18:3 n-3 (ALA)" — in 20 of the records used here, so adding every id that
# looks relevant double counts. Each entry below is therefore a PREFERENCE
# ORDER: take the first id present and ignore the rest of that chain.
_OMEGA3_CHAINS: tuple[tuple[int, ...], ...] = (
    (1404, 1270),   # 18:3 — ALA specifically, else the generic 18:3
    (1276,),        # 18:4 stearidonic
    (1278,),        # 20:5 EPA
    (1280,),        # 22:5 DPA
    (1272,),        # 22:6 DHA
)
_OMEGA6_CHAINS: tuple[tuple[int, ...], ...] = (
    (1316, 1269),   # 18:2 — linoleic n-6 specifically, else the generic 18:2
    (1321,),        # 18:3 n-6 GLA
    (1313,),        # 20:2 n-6
    (1406,),        # 20:3 n-6
    (1408, 1271),   # 20:4 — arachidonic n-6 specifically, else the generic 20:4
)
# When the generic 18:3 stands in for ALA, it still bundles any n-6 GLA reported
# separately; subtracting that back out keeps the two totals from overlapping.
_GENERIC_18_3, _SPECIFIC_ALA, _GLA = 1270, 1404, 1321

# Reference data types hold per-100 g composition for generic foods. "Branded"
# entries are label transcriptions: patchier, and their serving basis varies, so
# they rank last.
PREFERRED_DATA_TYPES = ("Foundation", "SR Legacy", "Survey (FNDDS)", "Branded")


@dataclass(frozen=True)
class FoodCandidate:
    """One possible match for a logged product, for a human/agent to choose from."""

    fdc_id: int
    description: str
    data_type: str
    # Nutrients we care about that this record actually carries; a candidate with
    # a great name but no calcium is useless for closing a calcium gap.
    available: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fdc_id": self.fdc_id,
            "description": self.description,
            "data_type": self.data_type,
            "available": list(self.available),
        }


def _unit(raw: object) -> str:
    """Canonical unit name.

    Micrograms come back as "µg", "mcg" or "UG" depending on endpoint, and
    upper-casing "µg" yields a Greek capital Mu rather than a "U" — so a naive
    comparison drops vitamin D and folate without a word.
    """
    u = str(raw).strip().upper()
    return "UG" if u in {"ΜG", "MCG", "UG"} else u


def per_100g(food: dict) -> dict[str, float]:
    """Extract the nutrients we track from an FDC food record, per 100 g.

    Accepts both shapes the API returns, because they differ and nothing in the
    payload flags which one you have: ``/food/{id}?format=full`` nests the
    nutrient (``{"nutrient": {"id", "unitName"}, "amount"}``) while
    ``/foods/search`` flattens it (``{"nutrientId", "unitName", "value"}``).
    Reading only the nested form makes every search result look nutrient-free,
    which silently turns "best available record" into "first record".

    The ``abridged`` detail format is deliberately not supported: it identifies
    nutrients only by their legacy number, a second id space to keep in step for
    no gain, so :meth:`FoodDataClient.food` always asks for ``full``.

    Silently skips any nutrient whose unit is not the one we expect, rather than
    coercing it — a wrong unit is a 1000x error and must not be guessed at.
    """
    grams: dict[int, float] = {}          # fatty acids, for the omega sums
    out: dict[str, float] = {}
    for entry in food.get("foodNutrients") or []:
        nested = entry.get("nutrient") or {}
        nutrient_id = nested.get("id", entry.get("nutrientId"))
        unit = _unit(nested.get("unitName", entry.get("unitName", "")))
        amount = entry.get("amount", entry.get("value"))
        if amount is None:
            continue
        if unit == "G":
            grams[nutrient_id] = float(amount)
        mapped = FDC_NUTRIENTS.get(nutrient_id)
        if mapped is None:
            continue
        field, expected_unit = mapped
        if unit != expected_unit:
            continue
        out[field] = float(amount)

    out.update(_omega_totals(grams))
    return out


def _omega_totals(grams: dict[int, float]) -> dict[str, float]:
    """Sum the individual fatty acids into omega-3 and omega-6 totals.

    Reported only when at least one constituent is present; a food with no fatty
    acid data at all must stay absent rather than become a confident 0.0.
    """
    def total(chains: tuple[tuple[int, ...], ...]) -> float | None:
        found = False
        acc = 0.0
        for chain in chains:
            for nutrient_id in chain:
                if nutrient_id in grams:
                    acc += grams[nutrient_id]
                    found = True
                    break          # most specific id wins; skip the generic
        return acc if found else None

    out: dict[str, float] = {}
    if (n3 := total(_OMEGA3_CHAINS)) is not None:
        if _SPECIFIC_ALA not in grams and _GENERIC_18_3 in grams:
            n3 -= grams.get(_GLA, 0.0)
        out["omega3_g"] = max(n3, 0.0)
    if (n6 := total(_OMEGA6_CHAINS)) is not None:
        out["omega6_g"] = n6
    return out


class FoodDataClient:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()
        self.cache_dir = self.config.raw_dir / "fooddata" / "foods"

    @property
    def online(self) -> bool:
        return not self.config.offline and self.config.food_data.configured

    # --- internal http -----------------------------------------------------
    def _get(self, path: str, params: dict[str, Any]) -> Any:
        import httpx

        with httpx.Client(base_url=self.config.food_data.base_url, timeout=30.0) as client:
            resp = client.get(path, params={**params, "api_key": self.config.food_data.api_key})
            resp.raise_for_status()
            return resp.json()

    # --- reads -------------------------------------------------------------
    def food(self, fdc_id: int) -> dict | None:
        """One food record, from cache when present, else fetched and cached.

        Returns ``None`` offline for an uncached food, so callers degrade to
        "no reference available" instead of failing the whole import.
        """
        import httpx

        cached = load_json(self.cache_dir / f"{fdc_id}.json", default=None)
        if cached is not None:
            return cached
        if not self.online:
            return None
        try:
            food = self._get(f"/food/{fdc_id}", {"format": "full"})
        except httpx.HTTPStatusError as exc:
            # Search can surface ids the detail endpoint will not serve. Treat
            # that as "no reference" so one dead record cannot abort a bulk run.
            if exc.response.status_code == 404:
                return None
            raise
        save_json(self.cache_dir / f"{fdc_id}.json", food)
        return food

    def _search(self, query: str, page_size: int) -> dict:
        """Raw search, narrowed to reference data types then progressively widened.

        FDC rejects some query/dataType combinations with a 400 rather than an
        empty result — ``Survey (FNDDS)`` on its own always does. Falling back
        keeps one awkward query from aborting a bulk run, and the last attempt
        drops the filter entirely so branded label data can still answer.
        """
        import httpx

        attempts = (
            {"dataType": "Foundation,SR Legacy"},
            {"dataType": "Foundation,SR Legacy,Survey (FNDDS)"},
            {},
        )
        last: dict = {}
        for extra in attempts:
            try:
                last = self._get("/foods/search", {"query": query, "pageSize": page_size, **extra})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400:
                    raise
                continue
            if last.get("foods"):
                return last
        return last

    def search(self, query: str, *, limit: int = 5,
               must: tuple[str, ...] = ()) -> list[FoodCandidate]:
        """Candidate foods for a product name, best reference types first.

        ``must`` gates candidates on name relevance before anything is ranked: a
        description has to contain one of these substrings to be eligible at all.
        FDC's own relevance is weak, and ranking on panel completeness alone
        picks whichever food is best MEASURED rather than the right food — that
        is how a query for blueberries came back with beets.

        Returns an empty list offline — candidate generation is the one step that
        genuinely needs the network.
        """
        if not self.online:
            return []
        payload = self._search(query, max(limit * 4, 50 if must else 0))
        terms = tuple(t.lower() for t in must)
        candidates = [
            FoodCandidate(
                fdc_id=int(f["fdcId"]),
                description=str(f.get("description", "")),
                data_type=str(f.get("dataType", "")),
                available=tuple(sorted(per_100g(f))),
            )
            for f in (payload.get("foods") or [])
            if f.get("fdcId")
            and (not terms or any(t in str(f.get("description", "")).lower() for t in terms))
        ]
        rank = {t: i for i, t in enumerate(PREFERRED_DATA_TYPES)}
        candidates.sort(key=lambda c: (rank.get(c.data_type, len(rank)), -len(c.available)))
        return candidates[:limit]
