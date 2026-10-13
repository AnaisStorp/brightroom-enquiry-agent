"""Evaluation: runs the baseline and the prototype on every test, scores both, writes the results.

    python -m eval.run_tests                 3 runs per test, reusing saved outputs
    python -m eval.run_tests --runs 1        quicker
    python -m eval.run_tests --only Q3,T17   a subset
    python -m eval.run_tests --fresh         ignore saved outputs and run everything again

Writes  eval/raw/<system>/<test>_run<n>.json   every raw output, with tokens, latency and cost
        eval/results.csv and eval/results.md   the test table and the summary

How a run is scored (the same five criteria for both systems):
    status         prototype: the code-computed status equals the expected one.
                   baseline:  it has no status field, so a grader model reads the reply (see below).
    total          the expected total, to the euro. Prototype: the engine total. Baseline: an amount
                   stated on a line of its reply that mentions "total".
    violations     no catalogue rule or "must not" from the expected behaviour is broken (grader model),
                   plus, for the prototype, the structured checks (venue, alternatives, flags).
    promises       no promise word (confirmed, booked, reserved, guaranteed, discount, free) used as
                   a promise. Negated or conditional uses ("cannot be guaranteed", "to be confirmed")
                   are not counted. Same detector for both systems.
    clarification  when the expected status is NEEDS_CLARIFICATION: the decision-critical questions are asked.

The grader model is the same model as the one being tested. Its judgments are an ESTIMATE and are
labelled as such in the results. Totals and promise words are checked by code.
"""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

import argparse  # stdlib: the command-line options
import csv  # stdlib: writes eval/results.csv
import json  # stdlib: reads the tests and saves raw outputs
import re  # stdlib: finds stated totals and promise words in free text
import statistics  # stdlib: medians for time and cost
from concurrent.futures import ThreadPoolExecutor  # stdlib: runs several enquiries at the same time
from pathlib import Path  # stdlib: locates data, raw outputs and result files

from baseline.single_prompt import run_baseline  # project: the single-prompt baseline
from src import llm, pipeline  # project: the model wrapper (for the grader) and the prototype
from src.guard import FORBIDDEN_WORDS  # project: the promise words, shared with the prototype's guard

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "eval" / "raw"
SYSTEMS = ("baseline", "prototype")

# --------------------------------------------------------------------------- code-based checks on free text

AMOUNT = re.compile(r"€\s?(\d[\d  .,]*)|(\d[\d  .,]*)\s?(?:€|euros?\b|EUR\b)", re.IGNORECASE)
NOT_A_PROMISE = re.compile(
    r"(\b(no|not|never|nothing|cannot|unable|without|neither|nor"
    r"|once|until|before|after|when|if|to be|must be|needs? to be|subject to|pending|feel|rien|pas|aucune?"
    r"|ne|sous réserve|à)\b|n['’]t\b)[^.!?\n]{0,60}$", re.IGNORECASE)


def to_number(text: str) -> float | None:
    cleaned = text.replace(" ", "").replace(" ", "").strip(".,")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", cleaned):       # 1,500 or 1.500 = thousands
        cleaned = re.sub(r"[.,]", "", cleaned)
    elif re.fullmatch(r"\d+[.,]\d{1,2}", cleaned):           # 320.00 or 320,00 = decimals
        cleaned = cleaned.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}([.,]\d{3})+[.,]\d{1,2}", cleaned):
        cleaned = re.sub(r"[.,](?=\d{3})", "", cleaned).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def stated_totals(text: str) -> list[float]:
    """Every amount on a line that mentions 'total'."""
    totals = []
    for line in text.splitlines():
        if "total" not in line.lower():
            continue
        for match in AMOUNT.finditer(line):
            number = to_number(match.group(1) or match.group(2))
            if number is not None and number not in totals:
                totals.append(number)
    return totals


def promises(text: str) -> list[str]:
    """Promise words used as a promise: negated or conditional uses are skipped."""
    found = []
    for match in FORBIDDEN_WORDS.finditer(text):
        before = text[max(0, match.start() - 70):match.start()]
        after = text[match.end():match.end() + 12].lower()
        if NOT_A_PROMISE.search(before):
            continue
        if match.group(0).lower() == "free" and (after.startswith(" to ") or before.rstrip().endswith("-")):
            continue  # "free to contact us", "step-free"
        found.append(match.group(0))
    return found


# --------------------------------------------------------------------------- the grader model

GRADER_TOOL = {
    "name": "record_grade",
    "description": "Record the grade for one reply.",
    "input_schema": {"type": "object", "required": ["verdict_matches_expected", "violations"], "properties": {
        "verdict_matches_expected": {"type": "boolean", "description": (
            "true only if the reply reaches the same overall verdict as the expected behaviour: it proposes "
            "when a proposal is expected, says the request cannot be met as specified when INFEASIBLE is "
            "expected, and asks instead of proposing a firm offer when NEEDS_CLARIFICATION is expected.")},
        "violations": {"type": "array", "items": {"type": "string"}, "description": (
            "Each real rule break in the reply, one short sentence each. Empty list if none. Count: a wrong "
            "price or total presented to the client; an item that is not in the catalogue priced or promised; "
            "a venue that breaks capacity or accessibility; a required service silently dropped or changed; a "
            "discount applied or promised; a booking, date or availability confirmed; allergen-safe catering "
            "promised; a total for part of the event presented as the price of the whole event; anything the "
            "expected behaviour says must not happen. Do not count style, length or tone.")},
        "asks_decision_critical_questions": {"type": ["boolean", "null"], "description": (
            "Only when the expected behaviour is NEEDS_CLARIFICATION: true if the reply asks the client for "
            "the missing information named there. Otherwise null.")},
        "reason": {"type": "string", "description": "One or two sentences explaining the grade."}}},
}

GRADER_SYSTEM = """You grade one draft reply written for BrightRoom, a small business-event studio. Be strict and literal. You are given the catalogue (the only source of truth), the client's enquiry, the expected behaviour (worked out by hand and correct), and the reply. Judge only what the reply says. Call the tool record_grade exactly once."""


def grade(test: dict, reply: str) -> dict:
    catalogue = (ROOT / "data" / "catalogue.json").read_text(encoding="utf-8")
    user = (f"CATALOGUE\n{catalogue}\n\nENQUIRY\n{test['enquiry']}\n\nEXPECTED BEHAVIOUR\n"
            f"{test['expected_behaviour']}\n\nREPLY TO GRADE\n{reply}")
    answer = llm.complete(GRADER_SYSTEM, [{"role": "user", "content": user}], tools=[GRADER_TOOL],
                          force_tool="record_grade", max_tokens=700)
    if answer.tool_call:
        return {**answer.tool_call.input, "graded": True}
    return {"verdict_matches_expected": False, "violations": ["grader gave no usable answer"], "graded": False}


# --------------------------------------------------------------------------- running


def raw_path(system: str, test_id: str, run: int) -> Path:
    return RAW / system / f"{test_id}_run{run}.json"


def produce(job: tuple[str, dict, int]) -> None:
    """Run one system on one test once, grade the reply, save everything."""
    system, test, run = job
    path = raw_path(system, test["id"], run)
    if path.exists():
        return
    if system == "baseline":
        reply = run_baseline(test["enquiry"])
        record = {"text": reply.text, "usage": {
            "llm_calls": 1, "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
            "latency_s": reply.latency_s, "cost_usd": round(reply.cost_usd, 6), "model": reply.model}}
    else:
        outcome = pipeline.run(test["enquiry"])
        record = {"text": outcome.draft, "result": outcome.model_dump(mode="json"),
                  "usage": outcome.usage.model_dump()}
    record["grade"] = grade(test, record["text"]) if record["text"] else {
        "verdict_matches_expected": False, "violations": ["no reply was produced"], "graded": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": test["id"], "system": system, "run": run, **record},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {system:9} {test['id']:4} run {run} done", flush=True)


# --------------------------------------------------------------------------- scoring


def score(system: str, test: dict, record: dict) -> dict:
    """The five criteria for one run. None = not applicable to this test."""
    expected, grade_, text = test["expected"], record["grade"], record["text"]
    needs_questions = expected["status"] == "NEEDS_CLARIFICATION"
    checks = {"status": None, "total": None, "violations": None, "promises": None, "clarification": None}
    notes = []

    found_promises = promises(record.get("result", {}).get("prose", "") if system == "prototype" else text)
    checks["promises"] = not found_promises
    if found_promises:
        notes.append("promise words: " + ", ".join(sorted(set(found_promises))))

    violations = list(grade_.get("violations", []))
    if system == "baseline":
        checks["status"] = bool(grade_.get("verdict_matches_expected"))
        totals = stated_totals(text)
        if expected["total"] is not None:
            checks["total"] = float(expected["total"]) in totals
            if not checks["total"]:
                notes.append("stated totals: " + (", ".join(f"€{t:g}" for t in totals) or "none"))
        if needs_questions:
            checks["clarification"] = bool(grade_.get("asks_decision_critical_questions"))
        summary = "stated total " + (", ".join(f"€{t:g}" for t in totals[:3]) or "none")
    else:
        result = record["result"]
        engine = result.get("engine")
        if result.get("error") or not engine:
            notes.append("error: " + str(result.get("error")))
            return {"checks": {**checks, "status": False}, "passed": False, "notes": notes, "summary": "ERROR"}
        checks["status"] = engine["status"] == expected["status"]
        checks["total"] = engine["total"] == expected["total"]
        if not checks["status"]:
            notes.append(f"status {engine['status']}")
        if not checks["total"]:
            notes.append(f"total {engine['total']}")
        # structured checks from the oracle
        if "venue" in expected and (engine["package"] or {}).get("venue_id") != expected["venue"]:
            violations.append(f"venue {(engine['package'] or {}).get('venue_id')} instead of {expected['venue']}")
        offered = {a["total"] for a in engine["alternatives"]}
        if not set(expected.get("alternative_totals", [])) <= offered:
            violations.append(f"alternatives {sorted(offered)} miss {expected['alternative_totals']}")
        flagged = " ".join(engine["flags"] + engine["assumptions"])
        for word in expected.get("must_flag", []):
            if word not in flagged:
                violations.append(f"did not flag '{word}'")
        if result.get("blocked"):
            violations.append("guard blocked the final draft")
        if needs_questions:
            asked = set(engine["clarifications"])
            scenarios = [s["total"] for s in engine["scenarios"]]
            checks["clarification"] = (set(expected.get("clarifications", [])) <= asked
                                       and scenarios == expected.get("scenario_totals", scenarios)
                                       and bool(grade_.get("asks_decision_critical_questions", True)))
        summary = engine["status"] + (f" €{engine['total']}" if engine["total"] is not None else "")
        if result.get("rewrites"):
            notes.append("1 rewrite")

    checks["violations"] = not violations
    notes += violations
    passed = all(value for value in checks.values() if value is not None)
    return {"checks": checks, "passed": passed, "notes": notes, "summary": summary}


def short(text: str, limit: int) -> str:
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def write_results(tests: list[dict], runs: int) -> None:
    rows, per_system = [], {s: {"passed": 0, "runs": 0, "tests_all_pass": 0, "latency": [], "cost": [],
                                "tokens_in": [], "tokens_out": [], "criteria": {}, "consistent": 0}
                            for s in SYSTEMS}
    for test in tests:
        row = {"id": test["id"], "label": test["label"], "input": short(test["enquiry"], 90),
               "expected": short(test["expected_behaviour"], 170)}
        for system in SYSTEMS:
            stats = per_system[system]
            scored = []
            for run in range(1, runs + 1):
                path = raw_path(system, test["id"], run)
                if not path.exists():
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                scored.append(score(system, test, record))
                usage = record["usage"]
                stats["latency"].append(usage["latency_s"])
                stats["cost"].append(usage["cost_usd"])
                stats["tokens_in"].append(usage["input_tokens"])
                stats["tokens_out"].append(usage["output_tokens"])
                for name, value in scored[-1]["checks"].items():
                    if value is not None:
                        hit, total = stats["criteria"].get(name, (0, 0))
                        stats["criteria"][name] = (hit + bool(value), total + 1)
            passes = sum(s["passed"] for s in scored)
            stats["passed"] += passes
            stats["runs"] += len(scored)
            stats["tests_all_pass"] += bool(scored) and passes == len(scored)
            stats["consistent"] += len({(s["passed"], s["summary"]) for s in scored}) <= 1
            summaries = list(dict.fromkeys(s["summary"] for s in scored))
            notes = list(dict.fromkeys(note for s in scored for note in s["notes"]))
            row[f"{system}_result"] = short("; ".join(summaries), 70)
            row[f"{system}_pass"] = f"{passes}/{len(scored)}"
            row[f"{system}_verdict"] = "PASS" if scored and passes == len(scored) else "FAIL"
            row[f"{system}_notes"] = short("; ".join(notes), 260)
        rows.append(row)

    with (ROOT / "eval" / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    model = llm.model()
    lines = [
        "# Test results: baseline against prototype", "",
        f"{len(tests)} test enquiries, {runs} runs each per system, model `{model}`. "
        "Q1 to Q5 are the original enquiries; T6 onwards are ADDED – fictional, generated for testing.", "",
        "A test counts as PASS only if every run passed every applicable criterion.", "",
        "| ID | Input | Expected behaviour | Baseline result | Baseline | Prototype result | Prototype | Notes |",
        "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        notes = []
        if r["baseline_notes"]:
            notes.append("Baseline: " + r["baseline_notes"])
        if r["prototype_notes"]:
            notes.append("Prototype: " + r["prototype_notes"])
        lines.append(f"| {r['id']} | {r['input']} | {r['expected']} | {r['baseline_result']} | "
                     f"**{r['baseline_verdict']}** ({r['baseline_pass']}) | {r['prototype_result']} | "
                     f"**{r['prototype_verdict']}** ({r['prototype_pass']}) | {' '.join(notes) or '–'} |")

    def med(values):
        return statistics.median(values) if values else 0

    lines += ["", "## Summary", "",
              "| | Baseline (single prompt) | Prototype (tool + guard) |", "|---|---|---|"]
    b, p = per_system["baseline"], per_system["prototype"]
    lines.append(f"| Tests passed on every run | {b['tests_all_pass']} of {len(tests)} | "
                 f"{p['tests_all_pass']} of {len(tests)} |")
    lines.append(f"| Individual runs passed | {b['passed']} of {b['runs']} | {p['passed']} of {p['runs']} |")
    lines.append(f"| Tests with the same outcome on every run | {b['consistent']} of {len(tests)} | "
                 f"{p['consistent']} of {len(tests)} |")
    labels = {"status": "Verdict correct", "total": "Total correct to the euro",
              "violations": "No rule violation", "promises": "No promise word used as a promise",
              "clarification": "Asks the needed questions"}
    for name, label in labels.items():
        cells = []
        for stats in (b, p):
            hit, total = stats["criteria"].get(name, (0, 0))
            cells.append(f"{hit} of {total} runs" if total else "–")
        lines.append(f"| {label} | {cells[0]} | {cells[1]} |")
    lines.append(f"| Median time per enquiry | {med(b['latency']):.1f} s | {med(p['latency']):.1f} s |")
    lines.append(f"| Median cost per enquiry (estimate) | ${med(b['cost']):.4f} | ${med(p['cost']):.4f} |")
    lines.append(f"| Median tokens in / out | {med(b['tokens_in']):.0f} / {med(b['tokens_out']):.0f} | "
                 f"{med(p['tokens_in']):.0f} / {med(p['tokens_out']):.0f} |")
    lines += ["", "## How to read this", "",
              "- **Checked by code:** the totals, the promise words and, for the prototype, the status, venue, "
              "alternatives, questions and flags.",
              "- **Judged by a grader model (estimate):** the baseline's verdict, and rule violations in the "
              "text of both systems. The grader is the same model as the one tested, so treat these judgments "
              "as an estimate, not a measurement.",
              f"- **Cost is an estimate:** tokens times a price constant in `src/llm.py` "
              f"(${llm.PRICE_PER_MTOK_INPUT_USD:g} in / ${llm.PRICE_PER_MTOK_OUTPUT_USD:g} out per million "
              "tokens). The prototype's time and cost do not include the grader call.",
              "- **Not measured:** how long a salesperson takes to prepare a proposal by hand, and how long "
              "they take to review a draft. No human timing was done.",
              "- **Run-to-run variation:** the model does not accept a temperature setting, so wording differs "
              "between runs. The prototype's numbers cannot differ, because code computes them.",
              "- Raw outputs for every run are in `eval/raw/`.", ""]
    (ROOT / "eval" / "results.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nbaseline : {b['tests_all_pass']}/{len(tests)} tests, {b['passed']}/{b['runs']} runs")
    print(f"prototype: {p['tests_all_pass']}/{len(tests)} tests, {p['passed']}/{p['runs']} runs")
    print("written: eval/results.csv, eval/results.md")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--only", default="", help="comma-separated test ids")
    parser.add_argument("--fresh", action="store_true", help="delete saved outputs first")
    parser.add_argument("--score-only", action="store_true", help="do not call the model, only rescore")
    args = parser.parse_args()

    tests = json.loads((ROOT / "data" / "tests.json").read_text(encoding="utf-8"))["tests"]
    if args.only:
        tests = [t for t in tests if t["id"] in args.only.split(",")]
    if args.fresh:
        for system in SYSTEMS:
            for test in tests:
                for path in (RAW / system).glob(f"{test['id']}_run*.json"):
                    path.unlink()
    if not args.score_only:
        jobs = [(system, test, run) for run in range(1, args.runs + 1) for test in tests for system in SYSTEMS]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(produce, jobs))
    write_results(tests, args.runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
