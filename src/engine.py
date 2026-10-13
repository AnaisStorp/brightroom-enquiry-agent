"""Deterministic pricing and feasibility engine. Pure code, NO LLM.

Takes a Brief, enumerates all 32 catalogue packages, filters by hard constraints,
picks the cheapest valid one and assigns a status with explicit reasons.

CLI:  python -m src.engine brief.json
"""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

import json  # stdlib: reads the catalogue and brief files, prints the result on the CLI
import sys  # stdlib: reads the brief path from the command line
from dataclasses import dataclass, replace  # stdlib: small immutable "requirements" record that alternatives copy and tweak
from itertools import combinations, product  # stdlib: enumerates service subsets, relaxation combos and scenario grids
from pathlib import Path  # stdlib: locates data/catalogue.json relative to this file
from typing import Optional  # stdlib: marks values that may be absent (None)

from src.schema import (  # project: the shared data shapes (brief in, engine result out)
    Alternative,
    Brief,
    EngineResult,
    EngineStatus,
    FieldStatus,
    LineItem,
    OptionalAddon,
    Package,
    RequirementCheck,
    Scenario,
    SERVICE_FIELDS,
    VenueRejection,
)

CATALOGUE_PATH = Path(__file__).resolve().parent.parent / "data" / "catalogue.json"

STANDING_NOTES = [
    "Only 4-hour bookings exist; there is no longer-duration price.",
    "Dates always need a separate availability check; nothing is booked or held.",
    "No discounts of any kind are available.",
]

CLARIFICATION_TEXT = {
    "headcount": "Headcount is missing: it decides the venue and every per-person price.",
    "headcount_range": "The headcount range changes the venue or the verdict: an exact number is needed.",
    "catering": "Catering is unclear: coffee break, lunch or both?",
    "budget": "Budget is not stated.",
    "services": "No services were mentioned: are coffee, lunch, a projector or a facilitator needed?",
    "accessibility": "Accessibility is not stated: the Studio is stairs-only.",
}
CLARIFICATION_ORDER = list(CLARIFICATION_TEXT)


def load_catalogue(path: Path = CATALOGUE_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def eur(amount: float) -> str:
    return f"€{amount:,.0f}" if float(amount).is_integer() else f"€{amount:,.2f}"


def _num(x: float) -> float:
    """Whole amounts as int so that 30.0 prints and compares as 30."""
    return int(x) if float(x).is_integer() else x


# --------------------------------------------------------------------------- packages


@dataclass(frozen=True)
class Requirements:
    """The hard constraints the engine solves for, derived from the brief."""

    headcount: int
    duration: float
    accessible: bool
    services: frozenset  # must-have service ids
    budget: Optional[float]  # None = not checked
    facilitator_hours: Optional[float] = None


def enumerate_packages(cat: dict) -> list[tuple[dict, tuple[str, ...]]]:
    """All 32 packages: 2 venues x every subset of S1-S4."""
    service_ids = [s["id"] for s in cat["services"]]
    subsets = [c for k in range(len(service_ids) + 1) for c in combinations(service_ids, k)]
    return [(venue, subset) for venue in cat["venues"] for subset in subsets]


def price_package(cat: dict, venue: dict, service_ids, headcount: int) -> Package:
    items = [LineItem(id=venue["id"], name=venue["name"], unit_price=venue["price"], quantity=1,
                      line_total=venue["price"], note=f"{venue['hours']} hours")]
    for svc in cat["services"]:
        if svc["id"] not in service_ids:
            continue
        per_person = svc["pricing"] == "per_person"
        qty = headcount if per_person else 1
        items.append(LineItem(id=svc["id"], name=svc["name"], unit_price=svc["price"], quantity=qty,
                              line_total=svc["price"] * qty,
                              note=f"{headcount} people x {eur(svc['price'])}" if per_person else svc["conditions"]))
    return Package(venue_id=venue["id"], service_ids=[i.id for i in items[1:]], items=items,
                   total=sum(i.line_total for i in items))


def _venue_reasons(venue: dict, req: Requirements) -> list[str]:
    reasons = []
    if req.headcount > venue["max_people"]:
        reasons.append(f"{venue['name']} holds max {venue['max_people']} people ({req.headcount} requested).")
    if req.accessible and not venue["wheelchair_accessible"]:
        reasons.append(f"{venue['name']} is stairs-only: not wheelchair accessible.")
    return reasons


def find_package(cat: dict, req: Requirements) -> Optional[Package]:
    """Cheapest package passing capacity, accessibility, duration <= 4h and must-have services."""
    valid = [
        price_package(cat, venue, subset, req.headcount)
        for venue, subset in enumerate_packages(cat)
        if not _venue_reasons(venue, req)
        and req.duration <= cat["booking_hours"]
        and req.services <= set(subset)
    ]
    return min(valid, key=lambda p: (p.total, len(p.items)), default=None)


def find_problems(cat: dict, req: Requirements, package: Optional[Package]) -> dict[str, str]:
    """Everything that makes the request infeasible as specified. Empty = feasible."""
    problems = {}
    largest = max(cat["venues"], key=lambda v: v["max_people"])
    if req.headcount > largest["max_people"]:
        problems["capacity"] = (f"{req.headcount} people exceeds the largest venue "
                                f"({largest['name']}, max {largest['max_people']}).")
    if req.duration > cat["booking_hours"]:
        problems["duration"] = (f"{req.duration:g} hours requested: only {cat['booking_hours']}-hour bookings "
                                "exist and there is no longer-duration price.")
    facilitator = next(s for s in cat["services"] if s["id"] == "S4")
    if "S4" in req.services and req.facilitator_hours and req.facilitator_hours > facilitator["included_hours"]:
        problems["facilitator_hours"] = (f"Facilitator requested for {req.facilitator_hours:g} hours: the catalogue "
                                         f"facilitator covers {facilitator['included_hours']} hours only.")
    if package and req.budget is not None and package.total > req.budget:
        problems["budget"] = (f"Total {eur(package.total)} is {eur(package.total - req.budget)} over the "
                              f"{eur(req.budget)} budget.")
    return problems


def _solve(cat: dict, req: Requirements) -> tuple[Optional[Package], dict[str, str]]:
    package = find_package(cat, req)
    return package, find_problems(cat, req, package)


# --------------------------------------------------------------------------- alternatives


def _alternatives(cat: dict, req: Requirements, access_stated: bool) -> list[Alternative]:
    """Relax ONE constraint at a time. Only if no single relaxation works, try two, then three."""
    names = {s["id"]: s["name"] for s in cat["services"]}
    included_hours = next(s for s in cat["services"] if s["id"] == "S4")["included_hours"]
    max_capacity = max(v["max_people"] for v in cat["venues"])

    options = []
    if req.budget is not None:
        options.append("budget")
    if req.headcount > 1:
        options.append("headcount")
    options += [f"drop:{sid}" for sid in sorted(req.services)]
    if req.duration > cat["booking_hours"]:
        options.append("duration")
    if "S4" in req.services and req.facilitator_hours and req.facilitator_hours > included_hours:
        options.append("facilitator_hours")

    def attempt(combo: tuple[str, ...]) -> Optional[Alternative]:
        if "drop:S4" in combo and "facilitator_hours" in combo:
            return None
        relaxed, gives_up = req, []
        for opt in combo:
            if opt == "budget":
                relaxed = replace(relaxed, budget=None)
            elif opt.startswith("drop:"):
                sid = opt.split(":")[1]
                relaxed = replace(relaxed, services=relaxed.services - {sid})
                gives_up.append(f"{names[sid]} (a stated requirement) is removed")
            elif opt == "duration":
                relaxed = replace(relaxed, duration=cat["booking_hours"])
                gives_up.append(f"duration shortened from {req.duration:g} hours to the only available "
                                f"{cat['booking_hours']}-hour block")
            elif opt == "facilitator_hours":
                relaxed = replace(relaxed, facilitator_hours=included_hours)
                gives_up.append(f"facilitator covers {included_hours} hours only, not the requested "
                                f"{req.facilitator_hours:g}")

        package = None
        if "headcount" in combo:
            # Largest smaller group for which everything else holds.
            for n in range(min(req.headcount - 1, max_capacity), 0, -1):
                candidate = replace(relaxed, headcount=n)
                pkg, problems = _solve(cat, candidate)
                if pkg and not problems:
                    relaxed, package = candidate, pkg
                    gives_up.append(f"headcount reduced from {req.headcount} to at most {n}")
                    break
        else:
            pkg, problems = _solve(cat, relaxed)
            if pkg and not problems:
                package = pkg
        if package is None:
            return None

        if "budget" in combo:
            gives_up.append(f"budget raised from {eur(req.budget)} to {eur(package.total)} "
                            f"(+{eur(package.total - req.budget)})")
        notes = []
        venue = next(v for v in cat["venues"] if v["id"] == package.venue_id)
        if not venue["wheelchair_accessible"] and not access_stated:
            notes.append(f"{venue['name']} is stairs-only and accessibility needs were not stated.")
        item_names = " + ".join(i.name for i in package.items)
        return Alternative(
            relaxed=list(combo),
            description=f"{item_names} for {relaxed.headcount} people = {eur(package.total)}",
            headcount=relaxed.headcount,
            gives_up="; ".join(gives_up),
            package=package,
            total=package.total,
            budget_delta=None if req.budget is None else _num(req.budget - package.total),
            notes=notes,
        )

    for size in range(1, len(options) + 1):
        found = [alt for combo in combinations(options, size) if (alt := attempt(combo))]
        if found:
            return found
    return []


# --------------------------------------------------------------------------- evaluate


def _is_set(field) -> bool:
    return field.value is not None and field.status != FieldStatus.MISSING


def evaluate(brief: Brief, cat: Optional[dict] = None) -> EngineResult:
    cat = cat or load_catalogue()
    venues = {v["id"]: v for v in cat["venues"]}
    services = {s["id"]: s for s in cat["services"]}
    booking_hours = cat["booking_hours"]
    smallest_capacity = min(v["max_people"] for v in cat["venues"])
    assumptions: list[str] = []
    flags: list[str] = []

    # ---- flags that do not depend on the numbers
    for req_ in brief.unlisted_requests:
        flags.append(f"'{req_.request}' is not in the catalogue: flagged, not priced, not promised.")
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

    # ---- read the brief into hard constraints
    must = frozenset(sid for sid, f in SERVICE_FIELDS.items()
                     if getattr(brief, f).value and getattr(brief, f).status in
                     (FieldStatus.CONFIRMED, FieldStatus.ASSUMED))
    optional = [sid for sid, f in SERVICE_FIELDS.items()
                if getattr(brief, f).value and getattr(brief, f).status == FieldStatus.PREFERENCE]
    any_service_mentioned = any(_is_set(getattr(brief, f)) for f in SERVICE_FIELDS.values())
    catering_unclear = bool(brief.catering_unclear.value) and not ({"S1", "S2"} & must)

    access_stated = _is_set(brief.wheelchair_access)
    accessible = bool(brief.wheelchair_access.value) and access_stated  # a stated need is always hard

    budget = brief.budget_eur.value if _is_set(brief.budget_eur) else None
    budget_is_soft = budget is not None and brief.budget_eur.status == FieldStatus.PREFERENCE

    if not _is_set(brief.duration_hours):
        duration = booking_hours
        assumptions.append(f"Duration not stated: assumed the standard {booking_hours}-hour block.")
    elif brief.duration_hours.value < booking_hours:
        duration = booking_hours
        assumptions.append(f"{brief.duration_hours.value:g} hours requested: the {booking_hours}-hour block "
                           "price applies (no shorter booking exists).")
    else:
        duration = brief.duration_hours.value

    headcount_known = _is_set(brief.headcount) and brief.headcount.value > 0
    if headcount_known and brief.headcount.status == FieldStatus.ASSUMED:
        assumptions.append(f"Headcount given approximately: priced for {brief.headcount.value} people.")
    if not any_service_mentioned and not catering_unclear:
        assumptions.append("No services requested: venue only.")

    def make_req(headcount: int, service_ids=must) -> Requirements:
        return Requirements(headcount=headcount, duration=duration, accessible=accessible,
                            services=frozenset(service_ids), budget=None if budget_is_soft else budget,
                            facilitator_hours=brief.facilitator_hours)

    def stairs_note(package: Optional[Package]) -> Optional[str]:
        if package and not venues[package.venue_id]["wheelchair_accessible"] and not access_stated:
            return (f"Accessibility not stated: assumed no wheelchair access is needed. "
                    f"The {venues[package.venue_id]['name']} is stairs-only.")
        return None

    # ---- ask vs assume: only ask when the answer could change the venue or the verdict
    clarifications: list[str] = []
    headcounts: list[int] = []
    if not headcount_known:
        clarifications.append("headcount")
    else:
        low = brief.headcount.value
        high = brief.headcount_max if brief.headcount_max and brief.headcount_max > low else low
        headcounts = [low]
        if high != low:
            outcomes = []
            for n in (low, high):
                pkg, problems = _solve(cat, make_req(n))
                outcomes.append((pkg.venue_id if pkg else None, bool(problems)))
            if outcomes[0] != outcomes[1]:
                clarifications.append("headcount_range")
                headcounts = [low, high]
            else:
                headcounts = [high]
                assumptions.append(f"Headcount range {low}-{high}: priced for {high} "
                                   "(same venue and same verdict at both ends).")
    if catering_unclear:
        clarifications.append("catering")

    if clarifications:
        if budget is None:
            clarifications.append("budget")
        if not any_service_mentioned and not catering_unclear:
            clarifications.append("services")
        if not access_stated and (not headcounts or min(headcounts) <= smallest_capacity):
            clarifications.append("accessibility")
        clarifications.sort(key=CLARIFICATION_ORDER.index)
        if "services" in clarifications:  # being asked, so not assumed
            assumptions = [a for a in assumptions if not a.startswith("No services requested")]

        scenarios = []
        variants = ([(must | {"S1"}, "coffee break"), (must | {"S2"}, "lunch")]
                    if catering_unclear else [(must, "")])
        if len(headcounts) * len(variants) > 1:
            for n, (service_ids, variant) in product(headcounts, variants):
                pkg, problems = _solve(cat, make_req(n, service_ids))
                if pkg is None:
                    verdict = "not possible"
                elif budget is None:
                    verdict = "budget not checked"
                else:
                    verdict = "over budget" if pkg.total > budget else "within budget"
                note = stairs_note(pkg)
                scenarios.append(Scenario(
                    label=f"{n} people" + (f", {variant}" if variant else ""),
                    headcount=n, package=pkg, total=pkg.total if pkg else None,
                    budget_delta=None if pkg is None or budget is None else _num(budget - pkg.total),
                    verdict=verdict, problems=list(problems.values()), notes=[note] if note else []))

        return EngineResult(
            status=EngineStatus.NEEDS_CLARIFICATION,
            reasons=[CLARIFICATION_TEXT[c] for c in clarifications],
            budget=budget, assumptions=assumptions, flags=flags, clarifications=clarifications,
            scenarios=scenarios, standing_notes=STANDING_NOTES)

    # ---- single, fully specified request
    req = make_req(headcounts[0])
    package, problems = _solve(cat, req)

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

    # ---- requirement checklist for the UI
    if "capacity" in problems:
        capacity_detail = problems["capacity"]
    elif package:
        capacity_detail = (f"{venues[package.venue_id]['name']} holds up to "
                           f"{venues[package.venue_id]['max_people']}.")
    else:
        capacity_detail = "No venue selected."
    checks = [RequirementCheck(requirement=f"Capacity for {req.headcount} people", ok="capacity" not in problems,
                               detail=capacity_detail),
              RequirementCheck(requirement=f"Duration {req.duration:g} hours", ok="duration" not in problems,
                               detail=problems.get("duration", f"Fits the {booking_hours}-hour block."))]
    if accessible:
        ok = bool(package) and venues[package.venue_id]["wheelchair_accessible"]
        checks.append(RequirementCheck(requirement="Wheelchair access", ok=ok,
                                       detail=f"{venues[package.venue_id]['name']} is wheelchair accessible."
                                       if ok else "No accessible venue fits this request."))
    for sid in sorted(must):
        if sid == "S4" and "facilitator_hours" in problems:
            checks.append(RequirementCheck(requirement=f"{services[sid]['name']} for "
                                           f"{req.facilitator_hours:g} hours", ok=False,
                                           detail=problems["facilitator_hours"]))
        else:
            included = bool(package) and sid in package.service_ids
            checks.append(RequirementCheck(requirement=services[sid]["name"], ok=included,
                                           detail=services[sid]["conditions"] if included else "Not included."))
    if budget is not None:
        over = bool(package) and package.total > budget
        checks.append(RequirementCheck(
            requirement=f"Budget {eur(budget)}" + (" (preference)" if budget_is_soft else ""),
            ok=bool(package) and not over,
            detail="No package to compare." if not package else
            f"Total {eur(package.total)}: " + (f"{eur(package.total - budget)} over." if over
                                              else f"{eur(budget - package.total)} under.")))
    for req_ in brief.unlisted_requests:
        checks.append(RequirementCheck(requirement=req_.request, ok=False, detail="Not in the catalogue: not offered."))

    # ---- cheaper venues that had to be rejected, and why
    rejected = []
    for venue in cat["venues"]:
        reasons = _venue_reasons(venue, req)
        if not reasons or (package and venue["price"] >= venues[package.venue_id]["price"]):
            continue
        fits = req.headcount <= venue["max_people"]
        rejected.append(VenueRejection(
            venue_id=venue["id"], name=venue["name"], reasons=reasons,
            would_cost=price_package(cat, venue, must, req.headcount).total if fits else None))

    addons = []
    for sid in optional:
        price = services[sid]["price"] * (req.headcount if services[sid]["pricing"] == "per_person" else 1)
        addons.append(OptionalAddon(id=sid, name=services[sid]["name"], price=price,
                                    fits_budget=None if budget is None or not package
                                    else package.total + price <= budget))

    if problems:
        status = EngineStatus.INFEASIBLE
        reasons = list(problems.values())
        alternatives = _alternatives(cat, req, access_stated)
    elif assumptions:
        status = EngineStatus.FEASIBLE_WITH_ASSUMPTIONS
        reasons = ["All stated requirements are met, subject to the listed assumptions."]
        alternatives = []
    else:
        status = EngineStatus.FEASIBLE
        reasons = ["All stated requirements are met within budget."]
        alternatives = []

    return EngineResult(
        status=status, reasons=reasons, package=package,
        total=package.total if package else None, budget=budget,
        budget_delta=None if package is None or budget is None else _num(budget - package.total),
        assumptions=assumptions, flags=flags, checks=checks, rejected_venues=rejected,
        optional_addons=addons, alternatives=alternatives, standing_notes=STANDING_NOTES)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m src.engine brief.json", file=sys.stderr)
        return 2
    brief = Brief.model_validate_json(Path(argv[1]).read_text(encoding="utf-8"))
    print(evaluate(brief).model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
