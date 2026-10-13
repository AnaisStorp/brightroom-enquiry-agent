"""BrightRoom enquiry assistant: one screen for the salesperson.

The tool runs straight through (read, calculate, draft, check). The salesperson reviews once,
at the end: the draft on the left, what the tool understood and calculated on the right.

Run:  streamlit run app.py
"""

import json  # stdlib: loads the test enquiries for the dropdown
from pathlib import Path  # stdlib: locates data/tests.json next to this file

import pandas as pd  # pandas: the editable "what the tool understood" table
import streamlit as st  # streamlit: the web UI

from src import pipeline, render  # project: runs the agent and builds the price tables
from src.brief_table import COLUMNS, STATUS_OPTIONS, brief_to_rows, rows_to_brief  # project: brief <-> table rows
from src.schema import EngineStatus  # project: the four statuses, for the banner colour

st.set_page_config(page_title="BrightRoom enquiry assistant", page_icon=":material/meeting_room:", layout="wide")

TESTS = {t["id"]: t for t in json.loads(
    (Path(__file__).parent / "data" / "tests.json").read_text(encoding="utf-8"))["tests"]}
NO_TEST = "Write my own"

BANNERS = {  # status -> (streamlit banner, icon, headline)
    EngineStatus.FEASIBLE: (st.success, ":material/check_circle:", "Feasible"),
    EngineStatus.FEASIBLE_WITH_ASSUMPTIONS: (st.warning, ":material/rule:", "Feasible, with assumptions"),
    EngineStatus.NEEDS_CLARIFICATION: (st.info, ":material/help:", "Needs clarification"),
    EngineStatus.INFEASIBLE: (st.error, ":material/block:", "Infeasible as requested"),
}

st.session_state.setdefault("enquiry", "")
st.session_state.setdefault("outcome", None)   # the PipelineResult on screen
st.session_state.setdefault("session", None)   # the open model conversation, reused when recalculating
st.session_state.setdefault("run_id", 0)       # changes on every run so the brief table resets


def load_test_case():
    choice = st.session_state.test_choice
    st.session_state.enquiry = "" if choice == NO_TEST else TESTS[choice]["enquiry"]
    st.session_state.outcome = None


def show(outcome, session):
    st.session_state.outcome, st.session_state.session = outcome, session
    st.session_state.run_id += 1


# --------------------------------------------------------------------------- input

st.title("BrightRoom enquiry assistant")
st.caption("Turns a client enquiry into a draft reply. Prices and decisions come from code, not from the AI. "
           "Nothing is sent, booked or promised: you review and send.")

with st.container(border=True):
    st.selectbox("Load a test enquiry", [NO_TEST, *TESTS], key="test_choice", on_change=load_test_case,
                 format_func=lambda k: k if k == NO_TEST else f"{k}: {TESTS[k]['enquiry'][:70]}")
    st.text_area("Client enquiry", key="enquiry", height=110,
                 placeholder="Paste the client's message here, exactly as received.")
    prepare = st.button("Prepare draft", type="primary", icon=":material/edit_note:",
                        disabled=not st.session_state.enquiry.strip())

if prepare:
    with st.spinner("Reading the enquiry, calculating, drafting and checking..."):
        try:
            session, partial = pipeline.extract(st.session_state.enquiry.strip())
            show(pipeline.finish(session, partial), session)
        except Exception as error:  # shown to the salesperson instead of a stack trace
            show(pipeline.PipelineResult(enquiry=st.session_state.enquiry, error=f"{type(error).__name__}: {error}"),
                 None)

outcome = st.session_state.outcome
if outcome is None:
    st.stop()
if outcome.error:
    st.error(f"The tool could not process this enquiry, so there is no draft.\n\n{outcome.error}",
             icon=":material/error:")
    st.stop()

engine = outcome.engine

# --------------------------------------------------------------------------- status banner (computed by code)

banner, icon, headline = BANNERS[engine.status]
banner(f"**{headline}**\n\n" + "\n".join(f"- {reason}" for reason in engine.reasons), icon=icon)

left, right = st.columns([11, 9], gap="large")

# --------------------------------------------------------------------------- left: the draft

with left:
    st.subheader("Draft reply")
    st.warning(f"**{render.HEADER}**", icon=":material/draft:")
    if engine.status == EngineStatus.INFEASIBLE:
        st.error(f"**{render.NOT_SENDABLE}**", icon=":material/do_not_disturb_on:")

    if outcome.blocked:
        st.error("**The safety check blocked this draft.** Do not send it. See the findings below.",
                 icon=":material/gpp_bad:")
    for finding in outcome.guard_findings:
        notice = st.error if finding.severity == "block" else st.warning
        notice(f"**Check: {finding.rule.replace('_', ' ')}** (\"{finding.match}\"). {finding.message}",
               icon=":material/policy:")
    if not outcome.guard_findings:
        st.caption(":material/verified_user: Safety check passed: no prices, promises or dates in the AI's text."
                   + (" One rewrite was needed." if outcome.rewrites else ""))

    with st.container(border=True):
        st.markdown(outcome.draft.split("\n\n", 1)[1])  # the header lines are shown above as banners
    with st.expander("Copy the draft as plain text"):
        st.code(outcome.draft, language=None, wrap_lines=True)

# --------------------------------------------------------------------------- right: what the tool understood and calculated

with right:
    st.subheader("What the tool understood")
    st.caption("Each value with the client's own words. Correct anything that is wrong and recalculate.")
    for warning in outcome.evidence_warnings:
        st.warning(warning, icon=":material/format_quote:")

    edited = st.data_editor(
        pd.DataFrame(brief_to_rows(outcome.brief), columns=COLUMNS), key=f"brief_{st.session_state.run_id}",
        hide_index=True, height="content", disabled=["Field"],
        column_config={
            "Value": st.column_config.TextColumn(help="A number, or yes / no. Leave empty if not stated."),
            "Status": st.column_config.SelectboxColumn(options=STATUS_OPTIONS, required=True, help=(
                "confirmed = required. preference = nice to have. assumed = approximate. missing = not stated.")),
        })
    for label, entries in (("Not in the catalogue", [r.request for r in outcome.brief.unlisted_requests]),
                           ("Allergies", outcome.brief.allergy_notes),
                           ("Dietary wishes", outcome.brief.dietary_notes),
                           ("Dates and deadlines", outcome.brief.date_requests)):
        if entries:
            st.markdown(f"**{label}:** " + "; ".join(entries))

    if st.button("Recalculate with my corrections", icon=":material/refresh:",
                 disabled=st.session_state.session is None):
        try:
            corrected = rows_to_brief(edited.to_dict("records"), outcome.brief)
        except ValueError as problem:
            st.error(str(problem), icon=":material/error:")
        else:
            with st.spinner("Recalculating and redrafting..."):
                show(pipeline.finish(st.session_state.session, outcome, corrected), st.session_state.session)
            st.rerun()

    st.subheader("Calculation")
    if engine.checks:
        st.markdown("\n".join(
            f"- {':green[:material/check_circle:]' if check.ok else ':red[:material/cancel:]'} "
            f"**{check.requirement}**: {check.detail}" for check in engine.checks))
    if engine.package:
        st.markdown(render.price_table(engine))
        if engine.budget_delta is not None and engine.bookings_requested == 1:
            st.metric("Against the budget", render.budget_text(engine.budget_delta), border=True)
    for rejected in engine.rejected_venues:
        st.caption(f":material/info: {rejected.name} not used"
                   + (f" (it would have cost {render.eur(rejected.would_cost)})" if rejected.would_cost else "")
                   + ": " + " ".join(rejected.reasons))
    if engine.optional_addons:
        st.markdown("**Optional, not in the total:** " + "; ".join(
            f"{addon.name} +{render.eur(addon.price)}"
            + ("" if addon.fits_budget is None else " (fits the budget)" if addon.fits_budget else " (over budget)")
            for addon in engine.optional_addons))

    if engine.scenarios:
        st.markdown("**Scenarios, depending on the client's answer**")
        st.markdown(render.scenarios_table(engine))
    if engine.alternatives:
        st.markdown("**Alternatives, each needs the client's consent**")
        st.markdown(render.alternatives_table(engine))

    if engine.assumptions:
        st.markdown("**Assumptions**\n" + "\n".join(f"- {text}" for text in engine.assumptions))
    if engine.flags:
        st.markdown("**Warnings**\n" + "\n".join(f"- {text}" for text in engine.flags))
    st.markdown("**Always applies**\n" + "\n".join(f"- {text}" for text in engine.standing_notes))

    usage = outcome.usage
    st.caption(f"{usage.model} · {usage.llm_calls} AI calls · {usage.latency_s:.1f}s · "
               f"about ${usage.cost_usd:.3f} (estimate)")
