"""Resolving concept groups into product -> reference-food mappings.

The guarantee under test: a match, once settled, stays settled. FDC's search
ordering is not stable, so anything that re-decides a resolved concept on every
run is a bug, not a refresh.
"""

from __future__ import annotations

import pytest

from trinity.connectors import food_concepts as fc
from trinity.connectors.fooddata import FoodCandidate

EGG_WHOLE = {"fdcId": 171287, "description": "Egg, whole, raw, fresh",
             "foodNutrients": [{"nutrient": {"id": 1087, "unitName": "MG"}, "amount": 56.0}]}
EGG_WHITE = {"fdcId": 323697, "description": "Egg, white, raw",
             "foodNutrients": [{"nutrient": {"id": 1087, "unitName": "MG"}, "amount": 9.0}]}
EMPTY = {"fdcId": 55555, "description": "Record with no panel", "foodNutrients": []}


class StubClient:
    """Stands in for FoodDataClient, recording what was asked of it."""

    def __init__(self, foods=None, results=None):
        self.foods = foods or {}
        self.results = results or {}
        self.searches: list[tuple[str, tuple[str, ...]]] = []
        self.fetched: list[int] = []

    def search(self, query, *, limit=5, must=()):
        self.searches.append((query, tuple(must)))
        return self.results.get(query, [])[:limit]

    def food(self, fdc_id):
        self.fetched.append(int(fdc_id))
        return self.foods.get(int(fdc_id))


def concept(key="eggs", **kw):
    base = dict(key=key, queries=("egg",), must=(), confidence="high",
                products=("Jajka",), note=None, pin=None)
    return fc.Concept(**{**base, **kw})


def cand(fdc_id, description, data_type="SR Legacy"):
    return FoodCandidate(fdc_id=fdc_id, description=description,
                         data_type=data_type, available=("calcium_mg",))


def test_pin_skips_the_search_entirely():
    """A pinned concept must not re-query: the search is exactly the step whose
    ordering drifts between runs."""
    client = StubClient(foods={171287: EGG_WHOLE})
    res = fc.resolve({"eggs": concept(pin=171287)}, client)

    assert client.searches == []
    assert res.mappings["Jajka"].fdc_id == 171287
    assert res.chosen == {"eggs": 171287}


def test_repin_reopens_a_pinned_concept():
    client = StubClient(foods={171287: EGG_WHOLE, 323697: EGG_WHITE},
                        results={"egg": [cand(323697, "Egg, white, raw")]})
    res = fc.resolve({"eggs": concept(pin=171287)}, client, repin=True)

    assert client.searches == [("egg", ())]
    assert res.chosen == {"eggs": 323697}


def test_relevance_terms_reach_the_search():
    """The gate belongs to the query, not to post-filtering: ranking on panel
    completeness alone picks the best-measured food rather than the right one."""
    client = StubClient(foods={171287: EGG_WHOLE},
                        results={"egg": [cand(171287, "Egg, whole, raw, fresh")]})
    fc.resolve({"eggs": concept(must=("egg,",))}, client)

    assert client.searches == [("egg", ("egg,",))]


def test_shortlist_falls_through_a_record_that_will_not_serve():
    """Foundation records regularly 404 on the detail endpoint. Losing the whole
    concept to that would look like a nutrient gap rather than a fetch failure."""
    client = StubClient(
        foods={999: None, 171287: EGG_WHOLE},
        results={"egg": [cand(999, "Egg, dead record"), cand(171287, "Egg, whole")]})
    res = fc.resolve({"eggs": concept()}, client)

    assert client.fetched == [999, 171287]
    assert res.chosen == {"eggs": 171287}


def test_record_with_an_empty_panel_is_not_accepted():
    """Fetching successfully is not the same as having numbers."""
    client = StubClient(foods={55555: EMPTY}, results={"egg": [cand(55555, "Egg, empty")]})
    res = fc.resolve({"eggs": concept()}, client)

    assert res.mappings == {}
    assert res.chosen == {}
    assert [k for k, _ in res.skipped] == ["eggs"]
    assert res.unmatched == ["Jajka"]


def test_later_query_is_a_fallback_not_extra_breadth():
    """Once a phrasing yields candidates the alternatives are not consulted,
    so adding a fallback cannot dilute a working query."""
    client = StubClient(foods={171287: EGG_WHOLE},
                        results={"egg": [cand(171287, "Egg, whole")],
                                 "hen fruit": [cand(323697, "Egg, white")]})
    fc.resolve({"eggs": concept(queries=("egg", "hen fruit"))}, client)

    assert client.searches == [("egg", ())]


def test_weak_match_is_recorded_but_reported_as_not_applied():
    """A visible weak match invites correction; a dropped one just looks like a
    product nobody got round to."""
    client = StubClient(foods={171287: EGG_WHOLE},
                        results={"egg": [cand(171287, "Egg, whole")]})
    res = fc.resolve({"eggs": concept(confidence="low", note="a guess")}, client)

    assert res.mappings["Jajka"].confidence == "low"
    assert res.mappings["Jajka"].trusted is False
    assert res.skipped == [("eggs", "a guess")]
    assert res.unmatched == ["Jajka"]


def test_one_decision_covers_every_product_in_the_group():
    """40 non-alcoholic beers are one food question. Answering it per product
    invites 40 different answers."""
    beers = ("Zatecky 0,0%", "Lech Free", "Birra Moretti Zero")
    client = StubClient(foods={171287: EGG_WHOLE},
                        results={"egg": [cand(171287, "Egg, whole")]})
    res = fc.resolve({"beer": concept(products=beers)}, client)

    assert set(res.mappings) == set(beers)
    assert {m.fdc_id for m in res.mappings.values()} == {171287}


def test_concepts_survive_a_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "concepts_path", lambda config=None: tmp_path / "c.json")
    original = {"eggs": concept(queries=("egg", "hen fruit"), must=("egg,",),
                                note="whole egg", pin=171287)}
    fc.save_concepts(original)

    assert fc.load_concepts() == original


def test_a_single_query_string_loads_as_one_query(tmp_path, monkeypatch):
    """Hand-edited concepts should not have to wrap a lone query in a list."""
    monkeypatch.setattr(fc, "concepts_path", lambda config=None: tmp_path / "c.json")
    (tmp_path / "c.json").write_text(
        '{"concepts": {"eggs": {"query": "egg", "confidence": "high", '
        '"products": ["Jajka"]}}}')

    assert fc.load_concepts()["eggs"].queries == ("egg",)


@pytest.mark.parametrize("pin", [None, 171287])
def test_chosen_reports_what_settled_so_the_caller_can_pin_it(pin):
    client = StubClient(foods={171287: EGG_WHOLE},
                        results={"egg": [cand(171287, "Egg, whole")]})
    res = fc.resolve({"eggs": concept(pin=pin)}, client)

    assert res.chosen == {"eggs": 171287}
