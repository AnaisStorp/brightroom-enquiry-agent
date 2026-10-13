"""Runs one enquiry end to end and returns one result object.

    1. agent.request_tool_call   model fills in the tool input            (LLM, forced tool call)
    2. human checkpoint          salesperson may edit the brief           (UI; skipped on the CLI)
    3. tools.execute             engine prices and decides                (code)
    4. agent.write_draft         model writes prose around the verdict    (LLM, no tools, no prices)
    5. guard.check               code checks the prose, one rewrite       (code)
    6. render.assemble           code inserts the price tables and header (code)

CLI:  python -m src.pipeline "10 people, 4 hours, coffee. Budget €350."
      python -m src.pipeline --test Q3
"""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

import json  # stdlib: loads the test set for --test
import sys  # stdlib: reads the enquiry or test id from the command line
from pathlib import Path  # stdlib: locates data/tests.json
from typing import Optional  # stdlib: marks values that may be absent (None)

from pydantic import BaseModel, Field  # pydantic: the single result object returned to the UI and the evaluation

from src import agent, guard, render, tools  # project: the pipeline steps
from src.schema import Brief, EngineResult  # project: brief and engine result shapes


class Usage(BaseModel):
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    model: str = ""


class PipelineResult(BaseModel):
    enquiry: str
    brief: Optional[Brief] = None
    evidence_warnings: list[str] = Field(default_factory=list)
    engine: Optional[EngineResult] = None
    prose: str = ""   # what the model wrote, placeholders still in
    draft: str = ""   # header + prose + code-inserted tables
    guard_findings: list[guard.Finding] = Field(default_factory=list)
    first_draft_findings: list[guard.Finding] = Field(default_factory=list)  # what triggered a rewrite, if any
    blocked: bool = False
    rewrites: int = 0
    usage: Usage = Field(default_factory=Usage)
    error: Optional[str] = None


def _usage(session: agent.Session) -> Usage:
    return Usage(llm_calls=session.llm_calls, input_tokens=session.input_tokens,
                 output_tokens=session.output_tokens, latency_s=session.latency_s,
                 cost_usd=session.cost_usd, model=session.model)


def extract(enquiry: str) -> tuple[agent.Session, PipelineResult]:
    """Step 1. Returns the open session so the UI can pause for the human checkpoint."""
    session, brief = agent.request_tool_call(enquiry)
    return session, PipelineResult(enquiry=enquiry, brief=brief, usage=_usage(session),
                                   evidence_warnings=tools.check_evidence(brief, enquiry))


def finish(session: Optional[agent.Session], partial: PipelineResult,
           brief: Optional[Brief] = None) -> PipelineResult:
    """Steps 3-6, on the brief as extracted or as edited by the salesperson.

    session may be None when a saved result is reopened and corrected: a new conversation is started."""
    session = session or agent.new_session(partial.enquiry)
    brief = brief or partial.brief
    result = tools.execute(brief)
    prose = agent.write_draft(session, brief, result)
    findings = guard.check(prose, brief, result, partial.enquiry)
    rewrites, first_findings = 0, []
    if guard.blocked(findings):
        first_findings = findings
        prose = agent.rewrite_draft(session, [f"{f.message} (you wrote: \"{f.match}\")"
                                              for f in findings if f.severity == "block"])
        findings = guard.check(prose, brief, result, partial.enquiry)
        rewrites = 1
    return partial.model_copy(update={
        "brief": brief, "engine": result, "prose": prose, "draft": render.assemble(prose, result),
        "evidence_warnings": tools.check_evidence(brief, partial.enquiry),
        "guard_findings": findings, "first_draft_findings": first_findings, "blocked": guard.blocked(findings), "rewrites": rewrites,
        "usage": _usage(session)})


SAVED_RUNS = Path(__file__).resolve().parent.parent / "eval" / "raw" / "prototype"


def load_saved(test_id: str) -> Optional[PipelineResult]:
    """The first run the evaluation saved for a test enquiry, so the UI can open it instantly. None if absent."""
    path = SAVED_RUNS / f"{test_id}_run1.json"
    if not path.exists():
        return None
    try:
        saved = PipelineResult.model_validate(json.loads(path.read_text(encoding="utf-8"))["result"])
    except (KeyError, ValueError):
        return None
    return None if saved.error or saved.engine is None else saved


def finish_offline(brief: Brief, enquiry: str, prose: str) -> PipelineResult:
    """Steps 3, 5 and 6 on given prose, without calling the model. Used by tests and demos."""
    result = tools.execute(brief)
    findings = guard.check(prose, brief, result, enquiry)
    return PipelineResult(enquiry=enquiry, brief=brief, engine=result, prose=prose,
                          draft=render.assemble(prose, result),
                          evidence_warnings=tools.check_evidence(brief, enquiry),
                          guard_findings=findings, blocked=guard.blocked(findings))


def run(enquiry: str) -> PipelineResult:
    """The whole pipeline without a human edit. Errors are returned in the result, never raised."""
    try:
        session, partial = extract(enquiry)
        return finish(session, partial)
    except Exception as error:
        return PipelineResult(enquiry=enquiry, error=f"{type(error).__name__}: {error}")


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--test":
        path = Path(__file__).resolve().parent.parent / "data" / "tests.json"
        cases = {t["id"]: t["enquiry"] for t in json.loads(path.read_text(encoding="utf-8"))["tests"]}
        enquiry = cases[argv[2]]
    elif len(argv) == 2:
        enquiry = argv[1]
    else:
        print('usage: python -m src.pipeline "enquiry text"  |  python -m src.pipeline --test Q3', file=sys.stderr)
        return 2
    outcome = run(enquiry)
    if outcome.error:
        print(f"ERROR  {outcome.error}", file=sys.stderr)
        return 1
    print(f"ENQUIRY  {enquiry}\n")
    print("BRIEF (tool input)")
    for name, value in outcome.brief.model_dump(mode="json", exclude_defaults=True).items():
        print(f"  {name}: {json.dumps(value, ensure_ascii=False)}")
    for warning in outcome.evidence_warnings:
        print(f"  ! {warning}")
    engine = outcome.engine
    print(f"\nSTATUS  {engine.status.value}   total={engine.total}   budget_delta={engine.budget_delta}")
    for line in engine.reasons + engine.assumptions + engine.flags:
        print(f"  - {line}")
    print(f"\n{'-' * 72}\n{outcome.draft}{'-' * 72}")
    print("GUARD  " + ("BLOCKED" if outcome.blocked else "passed") + f"   rewrites={outcome.rewrites}")
    for finding in outcome.guard_findings:
        print(f"  [{finding.severity}] {finding.rule}: \"{finding.match}\"  {finding.message}")
    for finding in outcome.first_draft_findings:
        print(f"  first draft was rewritten because of [{finding.severity}] {finding.rule}: \"{finding.match}\"")
    usage = outcome.usage
    print(f"USAGE  {usage.llm_calls} calls, {usage.input_tokens} in / {usage.output_tokens} out tokens, "
          f"{usage.latency_s}s, ${usage.cost_usd:.4f} (estimate)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
