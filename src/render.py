"""Code-written parts of the draft: the price tables and the fixed header. No LLM involved."""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

from src.engine import eur  # project: formats euro amounts the same way everywhere (also used by app.py)
from src.schema import EngineResult, EngineStatus, Package  # project: the engine result being rendered
from src.tools import PLACEHOLDERS  # project: the placeholder names the model writes into its prose

HEADER = "DRAFT – requires salesperson approval – availability unconfirmed"
NOT_SENDABLE = "Not sendable as an offer"


def _package_text(package: Package) -> str:
    return " + ".join(item.name for item in package.items)


def budget_text(delta) -> str:
    if delta is None:
        return "budget not checked"
    if delta < 0:
        return f"{eur(-delta)} over budget"
    return f"{eur(delta)} under budget" if delta > 0 else "exactly on budget"


def price_table(result: EngineResult) -> str:
    if not result.package:
        return ""
    rows = ["| Item | Detail | Price |", "|---|---|---:|"]
    rows += [f"| {i.name} ({i.id}) | {i.note} | {eur(i.line_total)} |" for i in result.package.items]
    if result.bookings_requested > 1:
        rows.append(f"| **Total for ONE booking** (of {result.bookings_requested} requested) | | "
                    f"**{eur(result.total)}** |")
    else:
        rows.append(f"| **Total** | | **{eur(result.total)}** |")
    if result.budget is not None and result.bookings_requested > 1:
        rows.append(f"| Budget | | {eur(result.budget)} |")
        rows.append("| Difference | | not comparable: the total covers one booking only |")
    elif result.budget is not None:
        rows.append(f"| Budget | | {eur(result.budget)} |")
        rows.append(f"| Difference | | {budget_text(result.budget_delta)} |")
    else:
        rows.append("| Budget | | not stated – not checked |")
    return "\n".join(rows)


def alternatives_table(result: EngineResult) -> str:
    if not result.alternatives:
        return ""
    rows = ["| Option | Package | People | Total | What the client gives up |", "|---|---|---:|---:|---|"]
    rows += [f"| {n} | {_package_text(a.package)} | {a.headcount} | {eur(a.total)} | {a.gives_up} |"
             for n, a in enumerate(result.alternatives, 1)]
    rows.append("")
    rows.append("Every option changes a stated requirement and needs the client's explicit consent.")
    return "\n".join(rows)


def scenarios_table(result: EngineResult) -> str:
    if not result.scenarios:
        return ""
    rows = ["| Scenario | Package | Total | Budget |", "|---|---|---:|---|"]
    for s in result.scenarios:
        if s.package:
            rows.append(f"| {s.label} | {_package_text(s.package)} | {eur(s.total)} | {budget_text(s.budget_delta)} |")
        else:
            rows.append(f"| {s.label} | not possible | – | – |")
    return "\n".join(rows)


def assemble(prose: str, result: EngineResult) -> str:
    """Put the code-computed tables into the model's prose and add the fixed header."""
    tables = {PLACEHOLDERS["package"]: price_table(result),
              PLACEHOLDERS["alternatives"]: alternatives_table(result),
              PLACEHOLDERS["scenarios"]: scenarios_table(result)}
    body = prose.strip()
    for placeholder, table in tables.items():
        if placeholder in body:
            body = body.replace(placeholder, f"\n\n{table}\n\n" if table else "")
        elif table:  # the model forgot the placeholder: the numbers still must be shown
            body += f"\n\n{table}"
    header = HEADER + (f"\n{NOT_SENDABLE}" if result.status == EngineStatus.INFEASIBLE else "")
    return f"{header}\n\n{body.strip()}\n"
