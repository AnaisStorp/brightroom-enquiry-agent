"""BrightRoom enquiry assistant: one screen for the salesperson.

Left: the list of requests. Click one and its draft appears straight away, next to what the tool
understood and calculated. The salesperson reviews once, at the end, and sends the reply themselves.

Run:  streamlit run app.py
"""

import json  # stdlib: loads the test enquiries for the request list
from pathlib import Path  # stdlib: locates data/tests.json next to this file

import pandas as pd  # pandas: the editable "what the tool understood" table
import streamlit as st  # streamlit: the web UI

from src import pipeline, render  # project: runs the agent and builds the price tables
from src.brief_table import COLUMNS, STATUS_OPTIONS, brief_to_rows, rows_to_brief  # project: brief <-> table rows
from src.schema import EngineStatus  # project: the four statuses, for the banner colour

st.set_page_config(page_title="BrightRoom enquiry assistant", page_icon="🌷", layout="wide")

TESTS = {t["id"]: t for t in json.loads(
    (Path(__file__).parent / "data" / "tests.json").read_text(encoding="utf-8"))["tests"]}
OWN = "own"  # the "write my own" entry in the request list

BANNERS = {  # status -> (streamlit banner, icon, headline)
    EngineStatus.FEASIBLE: (st.success, ":material/check_circle:", "Feasible"),
    EngineStatus.FEASIBLE_WITH_ASSUMPTIONS: (st.warning, ":material/rule:", "Feasible, with assumptions"),
    EngineStatus.NEEDS_CLARIFICATION: (st.info, ":material/help:", "Needs clarification"),
    EngineStatus.INFEASIBLE: (st.error, ":material/block:", "Infeasible as requested"),
}

st.session_state.setdefault("results", {})   # request id -> PipelineResult shown for it
st.session_state.setdefault("sessions", {})  # request id -> open model conversation (None for saved runs)
st.session_state.setdefault("saved", set())  # request ids whose result on screen is a saved run
st.session_state.setdefault("version", 0)    # changes on every new result so the brief table resets


def remember(request_id, outcome, session=None, saved=False):
    st.session_state.results[request_id] = outcome
    st.session_state.sessions[request_id] = session
    st.session_state.saved.discard(request_id)
    if saved:
        st.session_state.saved.add(request_id)
    st.session_state.version += 1


def run_live(request_id, enquiry):
    with st.spinner("Reading the enquiry, doing the sums, writing the draft and double-checking it..."):
        try:
            session, partial = pipeline.extract(enquiry)
            remember(request_id, pipeline.finish(session, partial), session)
        except Exception as error:  # shown to the salesperson instead of a stack trace
            remember(request_id, pipeline.PipelineResult(enquiry=enquiry, error=f"{type(error).__name__}: {error}"))


def request_label(request_id):
    if request_id == OWN:
        return ":material/edit: **Write my own**"
    text = TESTS[request_id]["enquiry"]
    return f"**{request_id}** · {text[:58]}{'…' if len(text) > 58 else ''}"


# --------------------------------------------------------------------------- left: the list of requests

with st.sidebar:
    st.header("🌷 Requests")
    st.caption("Click a request to see its draft.")
    chosen = st.radio("Requests", [OWN, *TESTS], index=1, format_func=request_label,
                      label_visibility="collapsed", key="chosen")
    st.caption("Q1 to Q5 are the original enquiries. T6 onwards are added test cases (fictional).")

# --------------------------------------------------------------------------- top: the request itself

st.title("BrightRoom enquiry assistant")
st.markdown(":violet-badge[:material/calculate: Prices come from code, not from the AI] "
            ":green-badge[:material/lock: Nothing is sent or booked] "
            ":blue-badge[:material/person_check: You review and send]")

if chosen == OWN:
    with st.container(border=True):
        own_text = st.text_area("What did the client write?", key="own_text", height=110,
                                placeholder="Paste the client's message here, exactly as received.")
        if st.button("Prepare my draft", type="primary", icon=":material/auto_awesome:",
                     disabled=not own_text.strip()):
            run_live(OWN, own_text.strip())
    enquiry = own_text.strip()
else:
    enquiry = TESTS[chosen]["enquiry"]
    with st.container(border=True):
        st.caption(f":material/mail: The client wrote ({chosen})")
        st.markdown(f"#### {enquiry}")
    if chosen not in st.session_state.results:
        saved = pipeline.load_saved(chosen)
        if saved:
            remember(chosen, saved, saved=True)  # instant: a run saved by the evaluation
        else:
            run_live(chosen, enquiry)

outcome = st.session_state.results.get(chosen)
if outcome is None:
    with st.container(border=True, horizontal=True):
        st.markdown(":material/mail: **1. Paste**  \nthe client's message, exactly as received")
        st.markdown(":material/calculate: **2. Wait a few seconds**  \nthe tool reads it and code works out the price")
        st.markdown(":material/rate_review: **3. Review**  \nthe draft next to what the tool understood")
        st.markdown(":material/send: **4. Send it yourself**  \nafter checking availability")
    st.stop()
if outcome.error:
    st.error(f"The tool could not process this enquiry, so there is no draft.\n\n{outcome.error}",
             icon=":material/error:")
    st.stop()

engine = outcome.engine

if chosen in st.session_state.saved:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(":material/history: Showing a saved run of this request, so it opens instantly.")
        if st.button("Run it again live", icon=":material/play_arrow:", type="tertiary"):
            run_live(chosen, enquiry)
            st.rerun()

# --------------------------------------------------------------------------- status banner (computed by code)

banner, icon, headline = BANNERS[engine.status]
banner(f"**{headline}**\n\n" + "\n".join(f"- {reason}" for reason in engine.reasons), icon=icon)

left, right = st.columns([11, 9], gap="large")

# --------------------------------------------------------------------------- the draft

with left:
    st.subheader(":material/mail: Draft reply")
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
        st.caption(":green[:material/verified_user:] Safety check passed: no prices, promises or dates in the "
                   "AI's text." + (" One rewrite was needed." if outcome.rewrites else ""))

    with st.container(border=True):
        st.markdown(outcome.draft.split("\n\n", 1)[1])  # the header lines are shown above as banners
    with st.expander("Copy the draft as plain text"):
        st.code(outcome.draft, language=None, wrap_lines=True)

# --------------------------------------------------------------------------- the information for the salesperson

with right:
    st.subheader(":material/psychology_alt: What the tool understood")
    st.caption("Each value with the client's own words. Correct anything that is wrong and recalculate.")
    for warning in outcome.evidence_warnings:
        st.warning(warning, icon=":material/format_quote:")

    edited = st.data_editor(
        pd.DataFrame(brief_to_rows(outcome.brief), columns=COLUMNS), key=f"brief_{st.session_state.version}",
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

    if st.button("Recalculate with my corrections", icon=":material/refresh:"):
        try:
            corrected = rows_to_brief(edited.to_dict("records"), outcome.brief)
        except ValueError as problem:
            st.error(str(problem), icon=":material/error:")
        else:
            with st.spinner("Recalculating and redrafting..."):
                session = st.session_state.sessions.get(chosen)
                remember(chosen, pipeline.finish(session, outcome, corrected), session)
            st.rerun()

    st.subheader(":material/calculate: The sums")
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
