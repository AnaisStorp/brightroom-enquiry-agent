"""THE RULES AND THE CALCULATIONS. Plain if/else, no AI, read from top to bottom.

    RULE 1  Which venue?            choose_venue
    RULE 2  What does it cost?      price_package
    RULE 3  What is wrong with it?  find_problems
    RULE 4  Which status?           decide_status
    RULE 5  What else could work?   find_alternatives

Prices and limits are never typed in here: they are read from data/catalogue.json.
This file is written for BrightRoom's catalogue of two venues and four services. It is
deliberately not general: a third venue would mean rewriting RULE 1.
"""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

import json  # stdlib: reads data/catalogue.json
from dataclasses import dataclass, replace  # stdlib: the request record, and copies of it with one thing changed
from itertools import combinations  # stdlib: pairs of changes, used only when no single change works (RULE 5)
from pathlib import Path  # stdlib: locates data/catalogue.json relative to this file
from typing import Optional  # stdlib: marks values that may be absent (None)

from src.schema import Alternative, EngineStatus, LineItem, Package  # project: the shapes of the results

CATALOGUE_PATH = Path(__file__).resolve().parent.parent / "data" / "catalogue.json"


def load_catalogue(path: Path = CATALOGUE_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def eur(amount: float) -> str:
    return f"€{amount:,.0f}" if float(amount).is_integer() else f"€{amount:,.2f}"


# --------------------------------------------------------------------------- the catalogue, by name

CATALOGUE = load_catalogue()
VENUES = {venue["id"]: venue for venue in CATALOGUE["venues"]}
SERVICES = {service["id"]: service for service in CATALOGUE["services"]}

STUDIO = VENUES["V1"]        # €240, max 12 people, stairs only
LOFT = VENUES["V2"]          # €420, max 24 people, wheelchair accessible
COFFEE = SERVICES["S1"]      # €8 per person
LUNCH = SERVICES["S2"]       # €18 per person
PROJECTOR = SERVICES["S3"]   # €35 flat
FACILITATOR = SERVICES["S4"]  # €280 flat, 2 hours

BOOKING_HOURS = CATALOGUE["booking_hours"]            # 4: the only booking length that exists
FACILITATOR_HOURS = FACILITATOR["included_hours"]     # 2
MAX_PEOPLE = LOFT["max_people"]                       # 24: the largest group any venue holds


@dataclass(frozen=True)
class Request:
    """What has to be satisfied. Built from the brief by engine.py."""

    headcount: int
    duration: float                 # hours asked for (already raised to 4 if the client asked for less)
    wheelchair: bool                # wheelchair access needed
    coffee: bool                    # must-have services only; "nice to have" is not in here
    lunch: bool
    projector: bool
    facilitator: bool
    budget: Optional[float]         # None = no budget to check
    facilitator_hours: Optional[float] = None


# --------------------------------------------------------------------------- RULE 1: which venue?


def choose_venue(headcount: int, wheelchair: bool) -> Optional[dict]:
    """The cheapest venue that fits. The Studio is cheaper, so it wins whenever it is allowed."""
    if headcount > LOFT["max_people"]:
        return None      # more than 24: no venue is big enough
    if headcount > STUDIO["max_people"]:
        return LOFT      # 13 to 24: only the Loft is big enough
    if wheelchair:
        return LOFT      # up to 12, but the Studio is stairs-only
    return STUDIO        # up to 12, no access need: the cheaper Studio


def why_not(venue: dict, headcount: int, wheelchair: bool) -> list[str]:
    """The reasons a venue cannot be used for this request. Empty list = it can."""
    reasons = []
    if headcount > venue["max_people"]:
        reasons.append(f"{venue['name']} holds max {venue['max_people']} people ({headcount} requested).")
    if wheelchair and not venue["wheelchair_accessible"]:
        reasons.append(f"{venue['name']} is stairs-only: not wheelchair accessible.")
    return reasons


# --------------------------------------------------------------------------- RULE 2: what does it cost?


def price_package(venue: dict, request: Request) -> Package:
    """total = venue + coffee x people + lunch x people + projector + facilitator (only the ones required)."""
    people = request.headcount
    items = [LineItem(id=venue["id"], name=venue["name"], unit_price=venue["price"], quantity=1,
                      line_total=venue["price"], note=f"{venue['hours']} hours")]
    if request.coffee:
        items.append(_per_person(COFFEE, people))
    if request.lunch:
        items.append(_per_person(LUNCH, people))
    if request.projector:
        items.append(_flat(PROJECTOR))
    if request.facilitator:
        items.append(_flat(FACILITATOR))
    return Package(venue_id=venue["id"], service_ids=[item.id for item in items[1:]], items=items,
                   total=sum(item.line_total for item in items))


def _per_person(service: dict, people: int) -> LineItem:
    return LineItem(id=service["id"], name=service["name"], unit_price=service["price"], quantity=people,
                    line_total=service["price"] * people, note=f"{people} people x {eur(service['price'])}")


def _flat(service: dict) -> LineItem:
    return LineItem(id=service["id"], name=service["name"], unit_price=service["price"], quantity=1,
                    line_total=service["price"], note=service["conditions"])


def build_package(request: Request) -> Optional[Package]:
    """RULE 1 then RULE 2. None when nothing can be priced at all."""
    if request.duration > BOOKING_HOURS:
        return None      # longer than 4 hours: no such booking exists, so there is no price
    venue = choose_venue(request.headcount, request.wheelchair)
    if venue is None:
        return None      # group too large for any venue
    return price_package(venue, request)


# --------------------------------------------------------------------------- RULE 3: what is wrong with it?


def find_problems(request: Request, package: Optional[Package]) -> dict[str, str]:
    """Every reason the request cannot be met as written. Empty = it can."""
    problems = {}
    if request.headcount > MAX_PEOPLE:
        problems["capacity"] = (f"{request.headcount} people exceeds the largest venue "
                                f"({LOFT['name']}, max {MAX_PEOPLE}).")
    if request.duration > BOOKING_HOURS:
        problems["duration"] = (f"{request.duration:g} hours requested: only {BOOKING_HOURS}-hour bookings "
                                "exist and there is no longer-duration price.")
    if request.facilitator and request.facilitator_hours and request.facilitator_hours > FACILITATOR_HOURS:
        problems["facilitator_hours"] = (f"Facilitator requested for {request.facilitator_hours:g} hours: the "
                                         f"catalogue facilitator covers {FACILITATOR_HOURS} hours only.")
    if package and request.budget is not None and package.total > request.budget:
        problems["budget"] = (f"Total {eur(package.total)} is {eur(package.total - request.budget)} over the "
                              f"{eur(request.budget)} budget.")
    return problems


def solve(request: Request) -> tuple[Optional[Package], dict[str, str]]:
    package = build_package(request)
    return package, find_problems(request, package)


# --------------------------------------------------------------------------- RULE 4: which status?


def decide_status(questions: list[str], problems: dict[str, str], assumptions: list[str]) -> EngineStatus:
    if questions:
        return EngineStatus.NEEDS_CLARIFICATION        # an open question could change the venue or the verdict
    if problems:
        return EngineStatus.INFEASIBLE                 # cannot be met as written
    if assumptions:
        return EngineStatus.FEASIBLE_WITH_ASSUMPTIONS  # it fits, but something was assumed
    return EngineStatus.FEASIBLE                       # it fits, nothing assumed


# --------------------------------------------------------------------------- RULE 5: what else could work?

SERVICE_FLAGS = {"S1": "coffee", "S2": "lunch", "S3": "projector", "S4": "facilitator"}


def find_alternatives(request: Request, access_stated: bool) -> list[Alternative]:
    """Change ONE thing at a time and keep every change that makes the request possible.

    The things that may be changed:
        budget             raise it to the real total
        headcount          reduce it to the largest group that works
        drop:S1 .. S4      remove one required service
        duration           shorten to the 4-hour block
        facilitator_hours  accept the 2 hours that exist
    Accessibility is never changed. Only if NO single change works are two changes combined
    (then three), and every alternative says exactly what the client gives up.
    """
    changes = []
    if request.budget is not None:
        changes.append("budget")
    if request.headcount > 1:
        changes.append("headcount")
    for service_id, flag in SERVICE_FLAGS.items():
        if getattr(request, flag):
            changes.append(f"drop:{service_id}")
    if request.duration > BOOKING_HOURS:
        changes.append("duration")
    if request.facilitator and request.facilitator_hours and request.facilitator_hours > FACILITATOR_HOURS:
        changes.append("facilitator_hours")

    for how_many in range(1, len(changes) + 1):
        found = [alternative for combo in combinations(changes, how_many)
                 if (alternative := _try_changes(request, combo, access_stated))]
        if found:
            return found
    return []


def _try_changes(request: Request, combo: tuple[str, ...], access_stated: bool) -> Optional[Alternative]:
    """Apply the changes in `combo`. Returns an alternative only if the result has no problems left."""
    if "drop:S4" in combo and "facilitator_hours" in combo:
        return None      # pointless: shortening the hours of a facilitator that was removed
    changed, gives_up = request, []

    for change in combo:
        if change == "budget":
            changed = replace(changed, budget=None)
        elif change.startswith("drop:"):
            service_id = change.split(":")[1]
            changed = replace(changed, **{SERVICE_FLAGS[service_id]: False})
            gives_up.append(f"{SERVICES[service_id]['name']} (a stated requirement) is removed")
        elif change == "duration":
            changed = replace(changed, duration=BOOKING_HOURS)
            gives_up.append(f"duration shortened from {request.duration:g} hours to the only available "
                            f"{BOOKING_HOURS}-hour block")
        elif change == "facilitator_hours":
            changed = replace(changed, facilitator_hours=FACILITATOR_HOURS)
            gives_up.append(f"facilitator covers {FACILITATOR_HOURS} hours only, not the requested "
                            f"{request.facilitator_hours:g}")

    package = None
    if "headcount" in combo:
        # Try one person fewer, then two fewer, ... and stop at the first group size that works.
        for people in range(min(request.headcount - 1, MAX_PEOPLE), 0, -1):
            candidate = replace(changed, headcount=people)
            candidate_package, problems = solve(candidate)
            if candidate_package and not problems:
                changed, package = candidate, candidate_package
                gives_up.append(f"headcount reduced from {request.headcount} to at most {people}")
                break
    else:
        candidate_package, problems = solve(changed)
        if candidate_package and not problems:
            package = candidate_package
    if package is None:
        return None

    if "budget" in combo:
        gives_up.append(f"budget raised from {eur(request.budget)} to {eur(package.total)} "
                        f"(+{eur(package.total - request.budget)})")
    notes = []
    venue = VENUES[package.venue_id]
    if not venue["wheelchair_accessible"] and not access_stated:
        notes.append(f"{venue['name']} is stairs-only and accessibility needs were not stated.")
    budget_delta = None if request.budget is None else request.budget - package.total
    return Alternative(
        relaxed=list(combo),
        description=f"{' + '.join(item.name for item in package.items)} for {changed.headcount} people "
                    f"= {eur(package.total)}",
        headcount=changed.headcount,
        gives_up="; ".join(gives_up),
        package=package,
        total=package.total,
        budget_delta=budget_delta,
        notes=notes,
    )
