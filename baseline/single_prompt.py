"""Baseline: ONE prompt to the same model, catalogue pasted in. No pipeline, no checks.

Run:  python -m baseline.single_prompt            (all tests, raw outputs saved to baseline/outputs/)
      python -m baseline.single_prompt "enquiry"  (one enquiry, printed)
"""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

import json  # stdlib: reads the test set and saves raw outputs
import sys  # stdlib: reads an optional enquiry from the command line
from pathlib import Path  # stdlib: locates the data and output folders

from src.llm import LLMResponse, complete  # project: the provider-agnostic LLM call

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "baseline" / "outputs"

PROMPT = """You work for BrightRoom, a small business-event studio. This is our catalogue:

{catalogue}

Write a proposal for this enquiry.

Enquiry:
{enquiry}"""


def run_baseline(enquiry: str) -> LLMResponse:
    catalogue = (ROOT / "data" / "catalogue.json").read_text(encoding="utf-8")
    return complete(system="You are a helpful assistant.",
                    messages=[{"role": "user", "content": PROMPT.format(catalogue=catalogue, enquiry=enquiry)}])


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print(run_baseline(argv[1]).text)
        return 0
    tests = json.loads((ROOT / "data" / "tests.json").read_text(encoding="utf-8"))["tests"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for test in tests:  # one at a time, saved as we go, so a failed call never loses earlier outputs
        reply = run_baseline(test["enquiry"])
        (OUTPUT_DIR / f"{test['id']}.json").write_text(json.dumps({
            "id": test["id"], "enquiry": test["enquiry"], "model": reply.model, "output": reply.text,
            "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
            "latency_s": reply.latency_s, "cost_usd": round(reply.cost_usd, 5)}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"{test['id']:4} {reply.latency_s:6.1f}s  {reply.output_tokens} tokens out")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
