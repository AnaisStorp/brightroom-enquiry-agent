"""Turns the brief into the small "what the tool understood" table shown in the UI, and back.

Kept apart from app.py so that the conversion can be tested without starting Streamlit.
"""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

from src.schema import Brief, FieldStatus  # project: the brief being shown and edited

SALESPERSON_NOTE = "(entered by salesperson)"  # evidence text for values the salesperson typed in

# (brief field, label in the table, kind). "plain" fields are bare values without status or quote.
ROWS = [
    ("headcount", "Headcount", "int"),
    ("headcount_max", "Headcount, upper end of a range", "plain_int"),
    ("duration_hours", "Duration (hours)", "number"),
    ("budget_eur", "Budget (€)", "number"),
    ("wheelchair_access", "Wheelchair access needed", "yes_no"),
    ("coffee_break", "Coffee break", "yes_no"),
    ("lunch", "Lunch", "yes_no"),
    ("projector", "Projector", "yes_no"),
    ("facilitator", "Facilitator", "yes_no"),
    ("facilitator_hours", "Facilitator hours asked for", "plain_number"),
    ("catering_unclear", "Catering asked for but unclear", "yes_no"),
    ("bookings", "Separate bookings (days, sessions, rooms)", "plain_int"),
]
STATUS_OPTIONS = [status.value for status in FieldStatus] + ["n/a"]
COLUMNS = ["Field", "Value", "Status", "Quote from the enquiry"]


def _show(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:g}"


def brief_to_rows(brief: Brief) -> list[dict]:
    rows = []
    for name, label, kind in ROWS:
        field = getattr(brief, name)
        if kind.startswith("plain"):
            rows.append(dict(zip(COLUMNS, [label, _show(field), "n/a", ""])))
        else:
            rows.append(dict(zip(COLUMNS, [label, _show(field.value), field.status.value, field.evidence])))
    return rows


def _parse(text, kind: str, label: str):
    text = str(text or "").strip()
    if not text:
        return None
    try:
        if kind == "yes_no":
            answer = text.lower()
            if answer in ("yes", "y", "true", "oui", "ja", "1"):
                return True
            if answer in ("no", "n", "false", "non", "nein", "0"):
                return False
            raise ValueError
        number = float(text.replace("€", "").replace(",", "."))
        return int(number) if kind in ("int", "plain_int") else number
    except ValueError:
        expected = "yes or no" if kind == "yes_no" else "a number"
        raise ValueError(f"'{label}': \"{text}\" is not {expected}.") from None


def rows_to_brief(rows: list[dict], original: Brief) -> Brief:
    """Apply the salesperson's edits. Lists (unlisted requests, allergies, dates) are kept from the original."""
    data = original.model_dump(mode="json")
    for (name, label, kind), row in zip(ROWS, rows):
        value = _parse(row["Value"], kind, label)
        if kind.startswith("plain"):
            data[name] = value
            continue
        status = row["Status"] if row["Status"] in [s.value for s in FieldStatus] else "missing"
        evidence = str(row["Quote from the enquiry"] or "")
        if value is not None and status == "missing":
            status = "confirmed"  # the salesperson typed a value in: it counts
        if value is not None and not evidence.strip():
            evidence = SALESPERSON_NOTE
        data[name] = {"value": value, "status": status, "evidence": evidence if value is not None else ""}
    return Brief.model_validate(data)
