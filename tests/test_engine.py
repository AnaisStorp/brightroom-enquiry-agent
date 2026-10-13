"""Engine tests: hand-written briefs (no LLM) checked against the hand-computed oracle in data/tests.json.

If a test fails, the engine is wrong. The oracle is never edited to make a test pass.
"""

import json  # stdlib: loads the oracle file data/tests.json
from pathlib import Path  # stdlib: locates data/tests.json relative to this file

import pytest  # pytest: parametrises one test per oracle case

from itertools import combinations  # stdlib: builds every service combination for the brute-force cross-check

from src import rules  # project: the if/else rules under test
from src.engine import evaluate  # project: the code under test
from src.schema import Brief  # project: the brief shape the engine consumes

ORACLE = {t["id"]: t for t in json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "tests.json").read_text(encoding="utf-8"))["tests"]}


def f(value, evidence, status="confirmed"):
    return {"value": value, "status": status, "evidence": evidence}


# One hand-written brief per test case: what a correct extraction of the enquiry looks like.
BRIEFS = {
    "Q1": Brief(headcount=f(10, "10 people"), duration_hours=f(4, "4 hours"),
                coffee_break=f(True, "coffee for everyone"), budget_eur=f(350, "Maximum budget €350")),
    "Q2": Brief(headcount=f(20, "20 people"), duration_hours=f(4, "4 hours"),
                coffee_break=f(True, "coffee and lunch are required"),
                lunch=f(True, "coffee and lunch are required"), budget_eur=f(850, "Total budget €850")),
    "Q3": Brief(headcount=f(10, "10 people"), duration_hours=f(4, "4 hours"),
                wheelchair_access=f(True, "wheelchair access"), coffee_break=f(True, "coffee"),
                projector=f(True, "a projector"), budget_eur=f(600, "Budget €600")),
    "Q4": Brief(date_requests=["next month"]),
    "Q5": Brief(headcount=f(30, "30 people"), duration_hours=f(6, "6 hours"), lunch=f(True, "with lunch"),
                budget_eur=f(1500, "We can spend €1,500"), date_requests=["Please confirm it today"]),
    "T6": Brief(headcount=f(15, "about 15 people", "assumed"), catering_unclear=f(True, "with food")),
    "T7": Brief(headcount=f(12, "12 people"), duration_hours=f(4, "4 hours"), coffee_break=f(True, "coffee"),
                budget_eur=f(400, "Budget €400")),
    "T8": Brief(headcount=f(13, "13 people"), duration_hours=f(4, "4 hours"), coffee_break=f(True, "coffee"),
                budget_eur=f(400, "Budget €400")),
    "T9": Brief(headcount=f(10, "10 people"), duration_hours=f(4, "4 hours"), coffee_break=f(True, "coffee"),
                budget_eur=f(320, "Budget €320")),
    "T10": Brief(headcount=f(8, "8 people"), duration_hours=f(4, "4 hours"), lunch=f(True, "lunch"),
                 budget_eur=f(500, "Budget €500"), allergy_notes=["One colleague has a severe nut allergy"]),
    "T11": Brief(headcount=f(10, "10 people"), duration_hours=f(4, "4 hours"),
                 facilitator=f(True, "with a facilitator for the whole workshop"), facilitator_hours=4,
                 budget_eur=f(600, "Budget €600")),
    "T12": Brief(headcount=f(10, "10 people"), duration_hours=f(4, "4 hours"), coffee_break=f(True, "coffee"),
                 budget_eur=f(600, "Budget €600"),
                 unlisted_requests=[{"request": "photographer", "evidence": "Ideally also a photographer"},
                                    {"request": "wine", "evidence": "some wine"}]),
    "T13": Brief(headcount=f(10, "10 people"), duration_hours=f(4, "4 hours"), coffee_break=f(True, "coffee"),
                 budget_eur=f(300, "Budget €300"),
                 unlisted_requests=[{"request": "20% loyalty discount",
                                     "evidence": "please apply our 20% loyalty discount"}]),
    "T14": Brief(headcount=f(18, "18 personnes"), duration_hours=f(4, "une demi-journée (4h)"),
                 lunch=f(True, "déjeuner végétarien pour tous"), projector=f(True, "un vidéoprojecteur"),
                 wheelchair_access=f(True, "Une collègue est en fauteuil roulant"),
                 budget_eur=f(800, "Budget max 800 €"), dietary_notes=["végétarien pour tous"], language="fr"),
    "T15": Brief(headcount=f(10, "Between 10 and 14 people"), headcount_max=14, duration_hours=f(4, "4 hours"),
                 coffee_break=f(True, "coffee"), budget_eur=f(450, "Budget €450")),
    "T16": Brief(headcount=f(22, "22 people"), duration_hours=f(3, "3 hours"), coffee_break=f(True, "coffee"),
                 projector=f(True, "a projector"), budget_eur=f(700, "Budget €700")),
    "T17": Brief(headcount=f(50, "50 people"), budget_eur=f(100, "€100 total"),
                 unlisted_requests=[{"request": "confirm a booking", "evidence": "confirm a booking"}]),
    "T18": Brief(headcount=f(30, "30 people"), coffee_break=f(True, "coffee for everyone"),
                 budget_eur=f(1200, "Budget €1,200"),
                 unlisted_requests=[{"request": "split into two sessions of 15",
                                     "evidence": "split into two sessions of 15 on the same day"}]),
    "T19": Brief(headcount=f(10, "10 people"), duration_hours=f(4, "4 hours each day"),
                 coffee_break=f(True, "coffee on both days"), budget_eur=f(700, "Budget €700"), bookings=2,
                 date_requests=["two consecutive days"]),
    "T20": Brief(headcount=f(8, "8 people on site"), duration_hours=f(4, "4 hours"),
                 projector=f(True, "a projector"), budget_eur=f(400, "Budget €400"),
                 unlisted_requests=[{"request": "video-conferencing", "evidence": "video-conferencing"},
                                    {"request": "6 online participants", "evidence": "6 more joining online"}]),
}


def test_every_oracle_case_has_a_brief():
    assert set(BRIEFS) == set(ORACLE)


def test_if_else_rules_equal_cheapest_of_all_32_packages():
    """The assignment says: enumerate all 32 packages (2 venues x 16 service sets), drop the invalid ones,
    take the cheapest. rules.py does it with if/else instead. This proves both give the same answer for
    every headcount from 1 to 30, with and without a wheelchair need, for every set of required services."""
    service_ids = list(rules.SERVICES)
    service_sets = [set(c) for k in range(5) for c in combinations(service_ids, k)]
    assert len(rules.VENUES) * len(service_sets) == 32

    def cost(venue, chosen, people):
        return venue["price"] + sum(
            rules.SERVICES[s]["price"] * (people if rules.SERVICES[s]["pricing"] == "per_person" else 1)
            for s in chosen)

    for people in range(1, 31):
        for wheelchair in (False, True):
            for required in service_sets:
                valid = [(cost(venue, chosen, people), venue["id"])
                         for venue in rules.VENUES.values() for chosen in service_sets
                         if people <= venue["max_people"]
                         and (venue["wheelchair_accessible"] or not wheelchair)
                         and required <= chosen]
                package = rules.build_package(rules.Request(
                    headcount=people, duration=4, wheelchair=wheelchair, coffee="S1" in required,
                    lunch="S2" in required, projector="S3" in required, facilitator="S4" in required,
                    budget=None))
                expected = min(valid) if valid else None
                assert (package and (package.total, package.venue_id)) == expected


@pytest.mark.parametrize("test_id", list(ORACLE))
def test_status_total_and_budget_delta(test_id):
    expected = ORACLE[test_id]["expected"]
    result = evaluate(BRIEFS[test_id])
    assert result.status.value == expected["status"]
    assert result.total == expected["total"]
    assert result.budget_delta == expected["budget_delta"]
    if "venue" in expected:
        assert result.package.venue_id == expected["venue"]
    if "alternative_totals" in expected:
        totals = [a.total for a in result.alternatives]
        assert set(expected["alternative_totals"]) <= set(totals)
    if "scenario_totals" in expected:
        assert [s.total for s in result.scenarios] == expected["scenario_totals"]
    if "clarifications" in expected:
        assert result.clarifications == expected["clarifications"]
    for word in expected.get("must_flag", []):
        assert any(word in text for text in result.flags + result.assumptions)


@pytest.mark.parametrize("test_id", list(ORACLE))
def test_arithmetic_is_internally_consistent(test_id):
    result = evaluate(BRIEFS[test_id])
    packages = [result.package] + [a.package for a in result.alternatives] + [s.package for s in result.scenarios]
    for package in filter(None, packages):
        assert package.total == sum(i.line_total for i in package.items)
        assert all(i.line_total == i.unit_price * i.quantity for i in package.items)
    assert all(a.requires_client_consent and a.gives_up for a in result.alternatives)


# ---- case-specific behaviour from the oracle


def test_q1_flags_stairs_only_studio():
    assert any("stairs-only" in a for a in evaluate(BRIEFS["Q1"]).assumptions)


def test_q2_alternatives_each_relax_exactly_one_constraint():
    alternatives = {tuple(a.relaxed): a.total for a in evaluate(BRIEFS["Q2"]).alternatives}
    assert alternatives == {("budget",): 940, ("headcount",): 836, ("drop:S1",): 780, ("drop:S2",): 580}


def test_q2_headcount_alternative_is_16_people():
    alt = next(a for a in evaluate(BRIEFS["Q2"]).alternatives if a.relaxed == ["headcount"])
    assert "at most 16" in alt.gives_up


def test_q3_rejects_cheaper_studio_for_accessibility():
    result = evaluate(BRIEFS["Q3"])
    (rejected,) = result.rejected_venues
    assert (rejected.venue_id, rejected.would_cost) == ("V1", 355)
    assert "not wheelchair accessible" in rejected.reasons[0]
    assert result.assumptions == []


def test_q4_carries_standing_notes():
    notes = " ".join(evaluate(BRIEFS["Q4"]).standing_notes)
    assert "4-hour" in notes and "availability check" in notes and "No discounts" in notes


def test_q5_reasons_and_two_constraint_alternative():
    result = evaluate(BRIEFS["Q5"])
    reasons = " ".join(result.reasons)
    assert "30 people exceeds" in reasons and "6 hours requested" in reasons
    assert any("confirm it today" in flag for flag in result.flags)
    (alt,) = result.alternatives
    assert sorted(alt.relaxed) == ["duration", "headcount"]
    assert (alt.package.venue_id, alt.package.service_ids, alt.total) == ("V2", ["S2"], 852)
    assert "at most 24" in alt.gives_up


def test_t6_scenarios_are_loft_variants_with_budget_unchecked():
    scenarios = evaluate(BRIEFS["T6"]).scenarios
    assert [(s.package.venue_id, s.package.service_ids, s.verdict) for s in scenarios] == [
        ("V2", ["S1"], "budget not checked"), ("V2", ["S2"], "budget not checked")]


def test_t8_alternatives():
    alternatives = {tuple(a.relaxed): a for a in evaluate(BRIEFS["T8"]).alternatives}
    assert alternatives[("headcount",)].total == 336 and "at most 12" in alternatives[("headcount",)].gives_up
    assert alternatives[("budget",)].total == 524


def test_t10_warns_no_allergen_guarantee():
    assert any("NO allergen guarantee" in flag for flag in evaluate(BRIEFS["T10"]).flags)


def test_t11_facilitator_limit_and_two_hour_alternative():
    result = evaluate(BRIEFS["T11"])
    assert any("covers 2 hours only" in r for r in result.reasons)
    assert sum("covers 2 hours only" in flag for flag in result.flags) == 1
    alt = next(a for a in result.alternatives if a.relaxed == ["facilitator_hours"])
    assert alt.total == 520 and alt.package.service_ids == ["S4"]


def test_t12_unlisted_requests_flagged_not_priced():
    result = evaluate(BRIEFS["T12"])
    assert [i.id for i in result.package.items] == ["V1", "S1"]
    assert sum("not in the catalogue" in flag for flag in result.flags) == 2


def test_t13_no_discount_applied():
    result = evaluate(BRIEFS["T13"])
    assert result.total == 320
    assert any("20% loyalty discount" in flag for flag in result.flags)
    assert all(a.total in (320, 296, 240) for a in result.alternatives)


def test_t15_shows_both_headcount_scenarios():
    scenarios = evaluate(BRIEFS["T15"]).scenarios
    assert [(s.headcount, s.total, s.verdict) for s in scenarios] == [
        (10, 320, "within budget"), (14, 532, "over budget")]


def test_t16_labels_four_hour_block():
    assert any("4-hour block price applies" in a for a in evaluate(BRIEFS["T16"]).assumptions)


def test_t17_never_yields_a_package_for_50_people():
    result = evaluate(BRIEFS["T17"])
    assert result.package is None
    assert all(a.requires_client_consent for a in result.alternatives)


def test_t19_two_days_are_never_priced_as_one_total():
    result = evaluate(BRIEFS["T19"])
    assert result.status.value == "FEASIBLE_WITH_ASSUMPTIONS" and result.bookings_requested == 2
    assert any("ONE booking only" in a for a in result.assumptions)


def test_several_bookings_are_never_plain_feasible():
    brief = BRIEFS["Q3"].model_copy(update={"bookings": 3})
    assert evaluate(BRIEFS["Q3"]).status.value == "FEASIBLE"
    assert evaluate(brief).status.value == "FEASIBLE_WITH_ASSUMPTIONS"


# ---- policy cases beyond the oracle list


def test_missing_budget_gives_provisional_proposal():
    result = evaluate(Brief(headcount=f(20, "20 people"), duration_hours=f(4, "4 hours"),
                            coffee_break=f(True, "coffee")))
    assert result.status.value == "FEASIBLE_WITH_ASSUMPTIONS" and result.total == 580
    assert any("budget not checked" in a for a in result.assumptions)


def test_hedged_service_is_optional_not_priced_in():
    result = evaluate(Brief(headcount=f(20, "20 people"), duration_hours=f(4, "4 hours"),
                            coffee_break=f(True, "coffee"), projector=f(True, "ideally a projector", "preference"),
                            budget_eur=f(600, "Budget €600")))
    assert result.status.value == "FEASIBLE" and result.total == 580
    assert [(a.id, a.price, a.fits_budget) for a in result.optional_addons] == [("S3", 35, False)]


def test_must_have_is_never_dropped_from_main_package():
    result = evaluate(BRIEFS["Q2"])
    assert {"S1", "S2"} <= set(result.package.service_ids)
