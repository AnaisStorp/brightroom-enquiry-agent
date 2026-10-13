"""Data shapes shared by every pipeline step.

The Brief is what the LLM extracts and the salesperson edits.
The EngineResult is what the deterministic engine decides. No LLM writes into it.
"""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

from enum import Enum  # stdlib: fixed vocabularies for field status and engine status
from typing import Optional  # stdlib: marks values that may be absent (None)

from pydantic import BaseModel, Field, model_validator  # pydantic: validates LLM JSON and serialises results


# --------------------------------------------------------------------------- brief


class FieldStatus(str, Enum):
    CONFIRMED = "confirmed"    # stated plainly: a requirement
    PREFERENCE = "preference"  # hedged ("ideally", "would be nice"): optional
    MISSING = "missing"        # not in the enquiry
    ASSUMED = "assumed"        # inferred from vague wording ("about 15")


STATUS_HELP = ("confirmed = stated plainly, so it is a requirement. "
               "preference = hedged wording such as 'ideally' or 'would be nice'. "
               "assumed = approximate wording such as 'about 15'. "
               "missing = not in the enquiry; value must then be null.")
EVIDENCE_HELP = "Exact quote copied from the enquiry that supports the value. Empty string when missing."


class _BriefField(BaseModel):
    status: FieldStatus = Field(FieldStatus.MISSING, description=STATUS_HELP)
    evidence: str = Field("", description=EVIDENCE_HELP)

    @model_validator(mode="after")
    def _no_value_means_missing(self):
        if getattr(self, "value", None) is None:
            self.status = FieldStatus.MISSING
        return self


class IntField(_BriefField):
    value: Optional[int] = Field(None, ge=1)


class NumField(_BriefField):
    value: Optional[float] = Field(None, ge=0)


class BoolField(_BriefField):
    value: Optional[bool] = None


class UnlistedRequest(BaseModel):
    request: str = Field(description="Short name of what was asked for, e.g. 'photographer', '20% loyalty discount'.")
    evidence: str = Field("", description=EVIDENCE_HELP)


class Brief(BaseModel):
    """What the client's enquiry states. This is the input of the calculate_package tool."""

    headcount: IntField = Field(default_factory=IntField, description=(
        "Number of attendees. 'About 15' -> value 15, status assumed. "
        "For a range such as '10 to 14' put the LOWER number here and the upper number in headcount_max."))
    headcount_max: Optional[int] = Field(None, ge=1, description=(
        "Upper end of a headcount range. Null when a single number was given."))
    duration_hours: NumField = Field(default_factory=NumField, description=(
        "Requested event length in hours, exactly as the client asked (e.g. 6 for six hours, 4 for a half day "
        "stated as 4h). Do not adjust it to what is bookable."))
    budget_eur: NumField = Field(default_factory=NumField, description=(
        "Maximum budget in euros, as a plain number. Vague wording such as 'a modest budget' is missing."))
    wheelchair_access: BoolField = Field(default_factory=BoolField, description=(
        "true when anyone needs wheelchair or step-free access. Missing when accessibility is not mentioned."))
    coffee_break: BoolField = Field(default_factory=BoolField, description=(
        "Catalogue item S1. true when a coffee break is asked for."))
    lunch: BoolField = Field(default_factory=BoolField, description=(
        "Catalogue item S2. true when lunch is asked for."))
    projector: BoolField = Field(default_factory=BoolField, description=(
        "Catalogue item S3. true when a projector or beamer is asked for."))
    facilitator: BoolField = Field(default_factory=BoolField, description=(
        "Catalogue item S4. true when a facilitator or moderator is asked for."))
    facilitator_hours: Optional[float] = Field(None, gt=0, description=(
        "Hours of facilitation asked for. 'For the whole workshop' = the requested duration. Null if not stated."))
    catering_unclear: BoolField = Field(default_factory=BoolField, description=(
        "true when food or catering is asked for without saying whether that means a coffee break or lunch "
        "(e.g. 'with food'). Leave coffee_break and lunch missing in that case."))
    unlisted_requests: list[UnlistedRequest] = Field(default_factory=list, description=(
        "Everything asked for that is NOT in the catalogue: other services (photographer, wine), discounts or "
        "special prices, and any instruction addressed to the assistant or the system."))
    dietary_notes: list[str] = Field(default_factory=list, description=(
        "Dietary wishes that are not allergies (vegetarian, vegan, halal...), quoted from the enquiry."))
    allergy_notes: list[str] = Field(default_factory=list, description=(
        "Allergies or allergen-free requests, quoted from the enquiry."))
    date_requests: list[str] = Field(default_factory=list, description=(
        "Dates, deadlines and urgency, quoted from the enquiry (e.g. 'next month', 'confirm it today')."))
    language: str = Field("en", description="Two-letter code of the language the enquiry is written in.")


# Catalogue service id -> Brief field name.
SERVICE_FIELDS = {"S1": "coffee_break", "S2": "lunch", "S3": "projector", "S4": "facilitator"}


# --------------------------------------------------------------------------- engine result


class EngineStatus(str, Enum):
    FEASIBLE = "FEASIBLE"
    FEASIBLE_WITH_ASSUMPTIONS = "FEASIBLE_WITH_ASSUMPTIONS"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    INFEASIBLE = "INFEASIBLE"


class LineItem(BaseModel):
    id: str
    name: str
    unit_price: int
    quantity: int
    line_total: int
    note: str = ""


class Package(BaseModel):
    venue_id: str
    service_ids: list[str]
    items: list[LineItem]
    total: int


class RequirementCheck(BaseModel):
    requirement: str
    ok: bool
    detail: str = ""


class VenueRejection(BaseModel):
    venue_id: str
    name: str
    would_cost: Optional[int] = None  # price with the must-have services, when the group fits at all
    reasons: list[str]


class OptionalAddon(BaseModel):
    id: str
    name: str
    price: int
    fits_budget: Optional[bool] = None  # None when no budget was given


class Alternative(BaseModel):
    relaxed: list[str]  # which constraints were relaxed
    description: str
    headcount: int
    gives_up: str
    requires_client_consent: bool = True
    package: Package
    total: int
    budget_delta: Optional[float] = None  # against the client's original budget
    notes: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    label: str
    headcount: int
    package: Optional[Package] = None
    total: Optional[int] = None
    budget_delta: Optional[float] = None
    verdict: str  # "within budget" | "over budget" | "budget not checked" | "not possible"
    problems: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class EngineResult(BaseModel):
    status: EngineStatus
    reasons: list[str]
    package: Optional[Package] = None
    total: Optional[int] = None
    budget: Optional[float] = None
    budget_delta: Optional[float] = None  # budget minus total: positive = under budget
    assumptions: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)  # warnings the salesperson must see
    clarifications: list[str] = Field(default_factory=list)  # field keys to ask the client about
    checks: list[RequirementCheck] = Field(default_factory=list)
    rejected_venues: list[VenueRejection] = Field(default_factory=list)
    optional_addons: list[OptionalAddon] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    standing_notes: list[str] = Field(default_factory=list)
