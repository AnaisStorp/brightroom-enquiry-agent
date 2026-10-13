"""Applies the rules in rules.py to one brief. Pure code, NO AI.

    STEP 1  Collect the warnings that do not depend on any number.
    STEP 2  Read the brief: what is required, what is optional, what had to be assumed.
    STEP 3  Ask or assume: is anything missing that could change the venue or the verdict?
    STEP 4  If so: status NEEDS_CLARIFICATION, with priced scenarios where there are only a few.
    STEP 5  Otherwise: calculate the package, find the problems, decide the status.
    STEP 6  Build the checklist, the rejected venues, the optional add-ons and the alternatives.

The catalogue rules and every calculation are in rules.py.

CLI:  python -m src.engine brief.json
"""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

import sys  # stdlib: reads the brief path from the command line
from itertools import product  # stdlib: combines headcount options with catering options into scenarios
from pathlib import Path  # stdlib: reads the brief file given on the command line
from typing import Optional  # stdlib: marks values that may be absent (None)

from src import rules  # project: the catalogue rules and calculations (if/else, no AI)
from src.rules import eur, load_catalogue  # project: money formatting and catalogue loading, shared with other modules
from src.schema import (  # project: the shared data shapes (brief in, engine result out)
    Brief,
    EngineResult,
    FieldStatus,
    OptionalAddon,
    Package,
    RequirementCheck,
    Scenario,
    SERVICE_FIELDS,
    VenueRejection,
)

__all__ = ["evaluate", "eur", "load_catalogue"]

STANDING_NOTES = [
    "Only 4-hour bookings exist; there is no longer-duration price.",
    "Dates always need a separate availability check; nothing is booked or held.",
    "No discounts of any kind are available.",
]

# The questions the tool can ask the client, in the order they are listed.
QUESTIONS = {
    "headcount": "Headcount is missing: it decides the venue and every per-person price.",
    "headcount_range": "The headcount range changes the venue or the verdict: an exact number is needed.",
    "catering": "Catering is unclear: coffee break, lunch or both?",
    "budget": "Budget is not stated.",
    "services": "No services were mentioned: are coffee, lunch, a projector or a facilitator needed?",
    "accessibility": "Accessibility is not stated: the Studio is stairs-only.",
}


def _num(x: float) -> float:
    """Whole amounts as int so that 30.0 prints and compares as 30."""
    return int(x) if float(x).is_integer() else x


def _is_set(field) -> bool:
    return field.value is not None and field.status != FieldStatus.MISSING


def evaluate(brief: Brief) -> EngineResult:
    assumptions: list[str] = []
    flags: list[str] = []

    # ================================================================== STEP 1: warnings
    for unlisted in brief.unlisted_requests:
        flags.append(f"'{unlisted.request}' is not in the catalogue: flagged, not priced, not promised.")
    if brief.allergy_notes:
        flags.append("Allergy mentioned (" + "; ".join(brief.allergy_notes) + "): BrightRoom gives NO allergen "
                     "guarantee. Do not promise allergen-free catering.")
    if brief.date_requests:
        flags.append("Date/urgency request (" + "; ".join(brief.date_requests) + "): dates need a separate "
                     "availability check. Nothing can be confirmed or booked from this draft.")
    if brief.dietary_notes:
        if brief.lunch.value:
            flags.append("Dietary note (" + "; ".join(brief.dietary_notes) + "): vegetarian/vegan lunch is "
                         "available on request; nothing else is guaranteed.")
        else:
            flags.append("Dietary note (" + "; ".join(brief.dietary_notes) + "): only lunch has "
                         "vegetarian/vegan options on request.")

    # Several days, sessions or rooms: the rules price ONE booking. Never let that total stand for the whole event.
    bookings = brief.bookings or 1
    if bookings > 1:
        assumptions.append(f"The request involves {bookings} separate bookings (several days, sessions or rooms). "
                           "Every price shown is for ONE booking only; the other bookings have to be priced "
                           "separately by the salesperson.")

    # ================================================================== STEP 2: read the brief
    # Services: stated plainly = required. Hedged ("ideally") = optional, never forced into the package.
    required = {service_id for service_id, name in SERVICE_FIELDS.items()
                if getattr(brief, name).value
                and getattr(brief, name).status in (FieldStatus.CONFIRMED, FieldStatus.ASSUMED)}
    optional = [service_id for service_id, name in SERVICE_FIELDS.items()
                if getattr(brief, name).value and getattr(brief, name).status == FieldStatus.PREFERENCE]
    any_service_mentioned = any(_is_set(getattr(brief, name)) for name in SERVICE_FIELDS.values())
    catering_unclear = bool(brief.catering_unclear.value) and not ({"S1", "S2"} & required)

    # Accessibility: a stated need is always a hard requirement.
    access_stated = _is_set(brief.wheelchair_access)
    wheelchair = bool(brief.wheelchair_access.value) and access_stated

    # Budget: a limit, unless the client hedged it ("ideally under ..."), then it is only compared.
    budget = brief.budget_eur.value if _is_set(brief.budget_eur) else None
    budget_is_soft = budget is not None and brief.budget_eur.status == FieldStatus.PREFERENCE

    # Duration: missing -> assume 4 hours. Shorter than 4 -> the 4-hour price applies. Longer -> kept, RULE 3 rejects it.
    if not _is_set(brief.duration_hours):
        duration = rules.BOOKING_HOURS
        assumptions.append(f"Duration not stated: assumed the standard {rules.BOOKING_HOURS}-hour block.")
    elif brief.duration_hours.value < rules.BOOKING_HOURS:
        duration = rules.BOOKING_HOURS
        assumptions.append(f"{brief.duration_hours.value:g} hours requested: the {rules.BOOKING_HOURS}-hour block "
                           "price applies (no shorter booking exists).")
    else:
        duration = brief.duration_hours.value

    headcount_known = _is_set(brief.headcount) and brief.headcount.value > 0
    if headcount_known and brief.headcount.status == FieldStatus.ASSUMED:
        assumptions.append(f"Headcount given approximately: priced for {brief.headcount.value} people.")
    if not any_service_mentioned and not catering_unclear:
        assumptions.append("No services requested: venue only.")

    def make_request(headcount: int, service_ids=frozenset(required)) -> rules.Request:
        return rules.Request(
            headcount=headcount, duration=duration, wheelchair=wheelchair,
            coffee="S1" in service_ids, lunch="S2" in service_ids,
            projector="S3" in service_ids, facilitator="S4" in service_ids,
            budget=None if budget_is_soft else budget, facilitator_hours=brief.facilitator_hours)

    def stairs_note(package: Optional[Package]) -> Optional[str]:
        """Accessibility not mentioned is fine, EXCEPT when the stairs-only Studio was chosen: always say so."""
        if package and not rules.VENUES[package.venue_id]["wheelchair_accessible"] and not access_stated:
            return (f"Accessibility not stated: assumed no wheelchair access is needed. "
                    f"The {rules.VENUES[package.venue_id]['name']} is stairs-only.")
        return None

    # ================================================================== STEP 3: ask or assume?
    # Ask only when the answer could change the venue or the verdict.
    questions: list[str] = []
    headcounts: list[int] = []

    if not headcount_known:
        questions.append("headcount")
    else:
        low = brief.headcount.value
        high = brief.headcount_max if brief.headcount_max and brief.headcount_max > low else low
        if high == low:
            headcounts = [low]
        else:
            # A range: compare both ends. Same venue and same verdict -> price the upper end, no question.
            outcome_low, outcome_high = (
                (package.venue_id if package else None, bool(problems))
                for package, problems in (rules.solve(make_request(low)), rules.solve(make_request(high))))
            if outcome_low == outcome_high:
                headcounts = [high]
                assumptions.append(f"Headcount range {low}-{high}: priced for {high} "
                                   "(same venue and same verdict at both ends).")
            else:
                headcounts = [low, high]
                questions.append("headcount_range")

    if catering_unclear:
        questions.append("catering")

    # ================================================================== STEP 4: questions for the client
    if questions:
        # While we are asking anyway, also ask for the other things that are missing.
        if budget is None:
            questions.append("budget")
        if not any_service_mentioned and not catering_unclear:
            questions.append("services")
            assumptions = [a for a in assumptions if not a.startswith("No services requested")]
        if not access_stated and (not headcounts or min(headcounts) <= rules.STUDIO["max_people"]):
            questions.append("accessibility")  # only matters if the Studio is still possible
        questions.sort(key=list(QUESTIONS).index)

        # Price the few possible scenarios so the client sees what each answer would mean.
        scenarios = []
        catering_options = ([(required | {"S1"}, "coffee break"), (required | {"S2"}, "lunch")]
                            if catering_unclear else [(required, "")])
        if len(headcounts) * len(catering_options) > 1:
            for people, (service_ids, catering) in product(headcounts, catering_options):
                package, problems = rules.solve(make_request(people, service_ids))
                if package is None:
                    verdict = "not possible"
                elif budget is None:
                    verdict = "budget not checked"
                elif package.total > budget:
                    verdict = "over budget"
                else:
                    verdict = "within budget"
                note = stairs_note(package)
                scenarios.append(Scenario(
                    label=f"{people} people" + (f", {catering}" if catering else ""),
                    headcount=people, package=package, total=package.total if package else None,
                    budget_delta=None if package is None or budget is None else _num(budget - package.total),
                    verdict=verdict, problems=list(problems.values()), notes=[note] if note else []))

        return EngineResult(
            status=rules.decide_status(questions, {}, assumptions),
            reasons=[QUESTIONS[q] for q in questions],
            budget=budget, assumptions=assumptions, flags=flags, clarifications=questions,
            scenarios=scenarios, standing_notes=STANDING_NOTES, bookings_requested=bookings)

    # ================================================================== STEP 5: calculate and decide
    request = make_request(headcounts[0])
    package, problems = rules.solve(request)

    note = stairs_note(package)
    if note:
        assumptions.append(note)
    if budget is None:
        assumptions.append("Budget not stated: budget not checked.")
    elif budget_is_soft and package and package.total > budget:
        assumptions.append(f"Total {eur(package.total)} is {eur(package.total - budget)} above the preferred "
                           f"budget of {eur(budget)} (stated as a preference, not a limit).")
    if "facilitator_hours" in problems:
        flags.append(problems["facilitator_hours"])

    status = rules.decide_status([], problems, assumptions)
    if problems:
        reasons = list(problems.values())
        alternatives = rules.find_alternatives(request, access_stated)
    elif assumptions:
        reasons = ["All stated requirements are met, subject to the listed assumptions."]
        alternatives = []
    else:
        reasons = ["All stated requirements are met within budget."]
        alternatives = []

    # ================================================================== STEP 6: what the salesperson sees
    venue = rules.VENUES[package.venue_id] if package else None

    # Checklist: one line per requirement, met or not.
    if "capacity" in problems:
        capacity_detail = problems["capacity"]
    elif venue:
        capacity_detail = f"{venue['name']} holds up to {venue['max_people']}."
    else:
        capacity_detail = "No venue selected."
    checks = [
        RequirementCheck(requirement=f"Capacity for {request.headcount} people", ok="capacity" not in problems,
                         detail=capacity_detail),
        RequirementCheck(requirement=f"Duration {request.duration:g} hours", ok="duration" not in problems,
                         detail=problems.get("duration", f"Fits the {rules.BOOKING_HOURS}-hour block.")),
    ]
    if wheelchair:
        accessible = bool(venue) and venue["wheelchair_accessible"]
        checks.append(RequirementCheck(
            requirement="Wheelchair access", ok=accessible,
            detail=f"{venue['name']} is wheelchair accessible." if accessible
            else "No accessible venue fits this request."))
    for service_id in sorted(required):
        service = rules.SERVICES[service_id]
        if service_id == "S4" and "facilitator_hours" in problems:
            checks.append(RequirementCheck(requirement=f"{service['name']} for {request.facilitator_hours:g} hours",
                                           ok=False, detail=problems["facilitator_hours"]))
        else:
            included = bool(package) and service_id in package.service_ids
            checks.append(RequirementCheck(requirement=service["name"], ok=included,
                                           detail=service["conditions"] if included else "Not included."))
    if budget is not None:
        over = bool(package) and package.total > budget
        if not package:
            budget_detail = "No package to compare."
        elif over:
            budget_detail = f"Total {eur(package.total)}: {eur(package.total - budget)} over."
        else:
            budget_detail = f"Total {eur(package.total)}: {eur(budget - package.total)} under."
        checks.append(RequirementCheck(
            requirement=f"Budget {eur(budget)}" + (" (preference)" if budget_is_soft else ""),
            ok=bool(package) and not over, detail=budget_detail))
    for unlisted in brief.unlisted_requests:
        checks.append(RequirementCheck(requirement=unlisted.request, ok=False,
                                       detail="Not in the catalogue: not offered."))

    # Venues that could not be used, with the reason. Shows why the cheaper Studio was not chosen.
    rejected = []
    for other in (rules.STUDIO, rules.LOFT):
        why = rules.why_not(other, request.headcount, wheelchair)
        if not why or (venue and other["price"] >= venue["price"]):
            continue
        fits = request.headcount <= other["max_people"]
        rejected.append(VenueRejection(
            venue_id=other["id"], name=other["name"], reasons=why,
            would_cost=rules.price_package(other, request).total if fits else None))

    # Optional add-ons: services the client hedged. Priced separately, never added to the total.
    addons = []
    for service_id in optional:
        service = rules.SERVICES[service_id]
        price = service["price"] * (request.headcount if service["pricing"] == "per_person" else 1)
        addons.append(OptionalAddon(id=service_id, name=service["name"], price=price,
                                    fits_budget=None if budget is None or not package
                                    else package.total + price <= budget))

    return EngineResult(
        status=status, reasons=reasons, package=package,
        total=package.total if package else None, budget=budget,
        budget_delta=None if package is None or budget is None else _num(budget - package.total),
        assumptions=assumptions, flags=flags, checks=checks, rejected_venues=rejected,
        optional_addons=addons, alternatives=alternatives, standing_notes=STANDING_NOTES,
        bookings_requested=bookings)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m src.engine brief.json", file=sys.stderr)
        return 2
    brief = Brief.model_validate_json(Path(argv[1]).read_text(encoding="utf-8"))
    print(evaluate(brief).model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
