"""Thin wrapper in front of the LLM provider, so the provider is swappable via env vars.

.env:  LLM_PROVIDER = anthropic | openrouter | openai
       MODEL        = model id as the chosen provider names it
       plus the matching API key (see .env.example)

Conversations use one provider-neutral message format:
    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "..."}                        plain text turn
    {"role": "assistant", "tool_call": ToolCall}                   the model called a tool
    {"role": "tool", "tool_call_id": "...", "content": "...", "is_error": False}   the tool's answer
"""

from __future__ import annotations  # stdlib: lets type hints refer to classes defined further down

import json  # stdlib: encodes and decodes tool-call arguments for the OpenAI-style API
import os  # stdlib: reads provider, model and API key from environment variables
import time  # stdlib: measures latency per call and waits between retries
from dataclasses import dataclass  # stdlib: small records for a tool call and an LLM response
from typing import Optional  # stdlib: marks values that may be absent (None)

from dotenv import load_dotenv  # python-dotenv: loads .env so the API key is never hard-coded

load_dotenv()

# EDIT ME: price per million tokens in USD, used only for the cost estimate in the evaluation.
# Defaults are the list prices for Claude Sonnet 5.5 on OpenRouter (checked 2026-10-13).
PRICE_PER_MTOK_INPUT_USD = float(os.getenv("PRICE_PER_MTOK_INPUT_USD", "2.00"))
PRICE_PER_MTOK_OUTPUT_USD = float(os.getenv("PRICE_PER_MTOK_OUTPUT_USD", "10.00"))

OPENAI_COMPATIBLE = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": (None, "OPENAI_API_KEY"),
}
RETRY_STATUS = {429, 500, 502, 503, 529}
RETRY_WAITS_S = (5, 15, 45)


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    tool_call: Optional[ToolCall]
    input_tokens: int
    output_tokens: int
    latency_s: float
    model: str

    @property
    def cost_usd(self) -> float:
        return (self.input_tokens * PRICE_PER_MTOK_INPUT_USD
                + self.output_tokens * PRICE_PER_MTOK_OUTPUT_USD) / 1_000_000


def provider() -> str:
    return os.getenv("LLM_PROVIDER", "anthropic").lower()


def model() -> str:
    name = os.getenv("MODEL")
    if not name:
        raise LLMError("MODEL is not set. Copy .env.example to .env and fill it in.")
    return name


def _api_key(env_var: str) -> str:
    key = os.getenv(env_var)
    if not key:
        raise LLMError(f"{env_var} is not set. Copy .env.example to .env and fill it in.")
    return key


def complete(system: str, messages: list[dict], tools: Optional[list[dict]] = None,
             force_tool: Optional[str] = None, max_tokens: int = 2000,
             temperature: float = 0.0) -> LLMResponse:
    """One model turn, retried on rate/overload errors.

    tools:      [{"name", "description", "input_schema"}]
    force_tool: name of the tool the model must call this turn. Providers that support a forced
                tool choice get one; otherwise the tool is offered and the caller must check that
                response.tool_call is set. When tools are given without force_tool, tool use is
                switched off and the model can only write text.
    """
    call = _anthropic_turn if provider() == "anthropic" else _openai_turn
    if provider() != "anthropic" and provider() not in OPENAI_COMPATIBLE:
        raise LLMError(f"Unknown LLM_PROVIDER '{provider()}'. Use anthropic, openrouter or openai.")
    for wait in (*RETRY_WAITS_S, None):
        start = time.perf_counter()
        try:
            text, tool_call, tokens_in, tokens_out = call(system, messages, tools, force_tool,
                                                          max_tokens, temperature)
            return LLMResponse(text=text, tool_call=tool_call, input_tokens=tokens_in, output_tokens=tokens_out,
                               latency_s=round(time.perf_counter() - start, 2), model=model())
        except Exception as error:  # both SDKs expose the HTTP status as .status_code
            if wait is None or getattr(error, "status_code", None) not in RETRY_STATUS:
                raise
            time.sleep(wait)


def _anthropic_turn(system, messages, tools, force_tool, max_tokens, temperature):
    import anthropic  # anthropic SDK: direct access to Claude models

    converted = []
    for m in messages:
        if m["role"] == "tool":
            converted.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"],
                "is_error": m.get("is_error", False)}]})
        elif "tool_call" in m:
            call = m["tool_call"]
            converted.append({"role": "assistant", "content": [{
                "type": "tool_use", "id": call.id, "name": call.name, "input": call.input}]})
        else:
            converted.append({"role": m["role"], "content": m["content"]})
    extra = {}
    if tools:
        extra["tools"] = tools
        # Current Claude models reject a forced tool_choice ("tool"/"any"), so the tool is offered with
        # "auto" and the CALLER enforces the call: agent.py refuses to continue without a valid tool call.
        extra["tool_choice"] = ({"type": "auto", "disable_parallel_tool_use": True} if force_tool
                                else {"type": "none"})
    # No temperature here: the Anthropic API rejects it for current Claude models ("temperature is
    # deprecated for this model"). Run-to-run consistency is measured in the evaluation instead.
    reply = anthropic.Anthropic(api_key=_api_key("ANTHROPIC_API_KEY")).messages.create(
        model=model(), max_tokens=max_tokens, system=system, messages=converted, **extra)
    text = "".join(block.text for block in reply.content if block.type == "text")
    used = next((block for block in reply.content if block.type == "tool_use"), None)
    tool_call = ToolCall(id=used.id, name=used.name, input=dict(used.input)) if used else None
    return text, tool_call, reply.usage.input_tokens, reply.usage.output_tokens


def _openai_turn(system, messages, tools, force_tool, max_tokens, temperature):
    from openai import OpenAI  # openai SDK: also talks to OpenRouter, which exposes the same API

    converted = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "tool":
            converted.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        elif "tool_call" in m:
            call = m["tool_call"]
            converted.append({"role": "assistant", "content": None, "tool_calls": [{
                "id": call.id, "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.input, ensure_ascii=False)}}]})
        else:
            converted.append({"role": m["role"], "content": m["content"]})
    extra = {}
    if tools:
        extra["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        extra["tool_choice"] = ({"type": "function", "function": {"name": force_tool}} if force_tool else "none")
    base_url, key_var = OPENAI_COMPATIBLE[provider()]
    reply = OpenAI(api_key=_api_key(key_var), base_url=base_url).chat.completions.create(
        model=model(), max_tokens=max_tokens, temperature=temperature, messages=converted, **extra)
    message = reply.choices[0].message
    tool_call = None
    if message.tool_calls:
        used = message.tool_calls[0]
        try:
            arguments = json.loads(used.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"_unparseable_arguments": used.function.arguments}
        tool_call = ToolCall(id=used.id, name=used.function.name, input=arguments)
    return message.content or "", tool_call, reply.usage.prompt_tokens, reply.usage.completion_tokens
