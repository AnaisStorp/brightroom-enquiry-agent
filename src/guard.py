"""Guard: code that checks the model's prose before anyone sees it as a draft.

The prose must contain no prices, no numbers the tool did not hand over, no promise words,
no date promise and no unlisted item presented as available. Tables are inserted by code
afterwards, so any amount found here was written by the model and is a violation.
"""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

import re  # stdlib: pattern matching for amounts, numbers, forbidden words and dates

from pydantic import BaseModel  # pydantic: one typed record per guard finding

from src.engine import load_catalogue  # project: catalogue conditions define which numbers are legitimate
from src.schema import Brief, EngineResult  # project: the brief and result the prose is checked against
from src.tools import required_placeholders  # project: which table placeholders the prose must contain


class Finding(BaseModel):
    rule: str
    severity: str  # "block" = must not go out as is | "review" = salesperson must look
    match: str
    message: str


PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")
LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)  # "1. " at the start of a line is layout, not a number
MONEY = re.compile(r"(€|\beur\b|\beuros?\b)\s?\d[\d.,]*|\d[\d.,]*\s?(€|\beur\b|\beuros?\b)", re.IGNORECASE)
NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w]|[.,]\d)")
PERCENT = re.compile(r"\d+\s?%")

# The words from the brief (English), plus the same promises in French and German.
FORBIDDEN_WORDS = re.compile(
    r"\b(confirmed|booked|reserved|guaranteed|discount(?:s|ed)?|free"
    r"|confirmée?s?|réservée?s?|garantie?s?|remises?|réductions?|rabais|gratuite?s?|offerte?s?"
    r"|bestätigt|gebucht|reserviert|garantiert|rabatt|kostenlos|gratis)\b", re.IGNORECASE)

DATE_PROMISE = re.compile(
    r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:january|february|march|april|may|june|july|august|september|october"
    r"|november|december|janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\b"
    r"|\b(?:january|february|march|april|june|july|august|september|october|november|december)\s+\d{1,2}\b"
    r"|\bavailable on (?!request)"
    r"|\b(?:is|are) available (?:that|this|next|for your date)"
    r"|\b(?:hold|held|holding|secure[ds]?|pencil(?:led)?)\b[^.]{0,30}\b(?:date|slot|room|venue|studio|loft)\b"
    r"|\bdisponible le\b", re.IGNORECASE)


NEGATED = re.compile(r"\b(nothing|not|no|never|cannot|can't|won't|isn't|aren't|n't|rien|pas|aucune?)\b[^.]*$",
                     re.IGNORECASE)


def _allowed_numbers(brief: Brief, result: EngineResult, enquiry: str) -> set[float]:
    """Numbers the prose may repeat: the client's own (non-money) numbers and the catalogue's conditions."""
    catalogue = load_catalogue()
    allowed = {float(catalogue["booking_hours"])}
    allowed |= {float(v["max_people"]) for v in catalogue["venues"]}
    allowed |= {float(s["included_hours"]) for s in catalogue["services"] if "included_hours" in s}
    for value in (brief.headcount.value, brief.headcount_max, brief.duration_hours.value, brief.facilitator_hours):
        if value is not None:
            allowed.add(float(value))
    if brief.bookings:
        allowed |= {float(n) for n in range(1, brief.bookings + 1)}
    quoted = [r.request + " " + r.evidence for r in brief.unlisted_requests]
    quoted += brief.date_requests + brief.dietary_notes + brief.allergy_notes + [enquiry]
    for text in quoted:  # numbers inside what the client asked for, e.g. "two sessions of 15"
        allowed |= {float(n.replace(",", ".")) for n in NUMBER.findall(MONEY.sub(" ", text))}
    allowed |= {float(a.headcount) for a in result.alternatives}
    allowed |= {float(s.headcount) for s in result.scenarios}
    allowed |= {float(n) for n in range(1, len(result.alternatives) + 1)}  # "option 1", "option 2"
    return allowed


def check(prose: str, brief: Brief, result: EngineResult, enquiry: str = "") -> list[Finding]:
    findings: list[Finding] = []
    text = LIST_MARKER.sub("", PLACEHOLDER.sub(" ", prose))

    for m in MONEY.finditer(text):
        findings.append(Finding(rule="money_in_prose", severity="block", match=m.group(0),
                                message="The model wrote a money amount. Prices may only come from the tool's table."))
    for m in PERCENT.finditer(text):
        findings.append(Finding(rule="percentage_in_prose", severity="block", match=m.group(0),
                                message="The model wrote a percentage. No discount or rate exists in the catalogue."))

    without_money = PERCENT.sub(" ", MONEY.sub(" ", text))
    allowed = _allowed_numbers(brief, result, enquiry)
    for m in NUMBER.finditer(without_money):
        if float(m.group(0).replace(",", ".")) not in allowed:
            findings.append(Finding(rule="unknown_number", severity="block", match=m.group(0),
                                    message="This number is neither in the brief nor a catalogue condition."))

    for m in FORBIDDEN_WORDS.finditer(text):
        findings.append(Finding(rule="forbidden_word", severity="block", match=m.group(0),
                                message="Promise word. A draft never confirms, books, reserves, guarantees or discounts."))
    for m in DATE_PROMISE.finditer(text):
        if NEGATED.search(text[max(0, m.start() - 25):m.start()]):  # "nothing is held", "we cannot hold"
            continue
        findings.append(Finding(rule="date_promise", severity="block", match=m.group(0).strip(),
                                message="Looks like a date or availability promise. Dates need a separate check."))

    for request in brief.unlisted_requests:
        for word in re.findall(r"[^\W\d_]{4,}", request.request):
            if re.search(rf"\b{re.escape(word)}", text, re.IGNORECASE):
                findings.append(Finding(rule="unlisted_item_mentioned", severity="review", match=word,
                                        message=f"'{request.request}' is not in the catalogue. Check the draft "
                                                "declines it and does not offer it."))
                break

    for placeholder in required_placeholders(result):
        if prose.count(placeholder) != 1:
            findings.append(Finding(rule="placeholder", severity="review", match=placeholder,
                                    message="Placeholder missing or repeated. The table was added by code anyway."))
    return findings


def blocked(findings: list[Finding]) -> bool:
    return any(f.severity == "block" for f in findings)
