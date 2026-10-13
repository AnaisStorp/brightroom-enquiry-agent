"""Tests for the UI: the brief table conversion (plain pytest) and the Streamlit app (headless AppTest)."""

import pytest  # pytest: fixtures and error assertions
from streamlit.testing.v1 import AppTest  # streamlit: runs app.py in-process without a browser

import json  # stdlib: reads the test enquiries
from pathlib import Path  # stdlib: locates data/tests.json

from src import llm, pipeline, tools  # project: the model call to script, saved runs, and the evidence check
from src.brief_table import SALESPERSON_NOTE, brief_to_rows, rows_to_brief  # project: the code under test
from tests.test_engine import BRIEFS  # project: hand-written briefs
from tests.test_tool_and_guard import CLEAN, ScriptedModel  # project: a scripted stand-in for the model

Q3 = "10 people, 4 hours, wheelchair access, coffee and a projector. Budget €600."
TESTS_ENQUIRY = {t["id"]: t["enquiry"] for t in json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "tests.json").read_text(encoding="utf-8"))["tests"]}


def value_of(rows, label):
    return next(row for row in rows if row["Field"] == label)


@pytest.mark.parametrize("test_id", list(BRIEFS))
def test_table_round_trip_keeps_the_brief(test_id):
    brief = BRIEFS[test_id]
    assert rows_to_brief(brief_to_rows(brief), brief) == brief


def test_salesperson_correction_is_applied_and_marked():
    brief = BRIEFS["Q1"]
    rows = brief_to_rows(brief)
    value_of(rows, "Headcount")["Value"] = "14"
    value_of(rows, "Wheelchair access needed")["Value"] = "yes"
    corrected = rows_to_brief(rows, brief)
    assert corrected.headcount.value == 14
    assert corrected.wheelchair_access.value is True and corrected.wheelchair_access.status.value == "confirmed"
    assert corrected.wheelchair_access.evidence == SALESPERSON_NOTE
    assert tools.execute(corrected).package.venue_id == "V2"
    enquiry = "10 people, 4 hours, coffee for everyone. Maximum budget €350."
    assert not any("wheelchair" in warning for warning in tools.check_evidence(corrected, enquiry))


def test_unreadable_value_is_reported_by_field_name():
    rows = brief_to_rows(BRIEFS["Q1"])
    value_of(rows, "Budget (€)")["Value"] = "a lot"
    with pytest.raises(ValueError, match="Budget"):
        rows_to_brief(rows, BRIEFS["Q1"])


@pytest.fixture
def app(monkeypatch):
    """The app with no saved runs on disk, so every request goes through the (scripted) model."""
    monkeypatch.setattr(pipeline, "load_saved", lambda test_id: None)
    return lambda: AppTest.from_file("../app.py", default_timeout=20)


def shown(app_run):
    return " ".join(element.value for kind in ("markdown", "success", "error", "info", "warning")
                    for element in getattr(app_run, kind))


def test_request_list_is_on_the_left_and_own_request_starts_empty(app):
    running = app()
    running.session_state["chosen"] = "own"
    running.run()
    assert not running.exception
    assert len(running.sidebar.radio) == 1 and any("**Q3**" in o for o in running.sidebar.radio[0].options)
    assert running.text_area[0].value == "" and running.button[0].disabled


@pytest.mark.parametrize("test_id, banner, must_show", [
    ("Q3", "success", "**€535**"),
    ("Q2", "error", "Not sendable as an offer"),
    ("Q4", "info", "Headcount is missing"),
])
def test_clicking_a_request_shows_its_draft_and_code_computed_prices(app, monkeypatch, test_id, banner, must_show):
    prose = {"Q3": CLEAN, "Q2": "We cannot meet this.\n{{PRICE_TABLE}}\n{{ALTERNATIVES_TABLE}}",
             "Q4": "Could you tell us how many people will attend?"}[test_id]
    model = ScriptedModel(BRIEFS[test_id].model_dump(mode="json"), prose)
    monkeypatch.setattr(llm, "complete", model)
    running = app()
    running.session_state["chosen"] = test_id
    running.run()  # one click on the request: no further button needed
    assert not running.exception and len(model.calls) == 2
    assert len(getattr(running, banner)) >= 1
    assert must_show in shown(running) and "requires salesperson approval" in shown(running)
    assert TESTS_ENQUIRY[test_id] in shown(running)


def test_saved_run_opens_without_calling_the_model(monkeypatch):
    saved = pipeline.finish_offline(BRIEFS["Q3"], Q3, CLEAN)
    monkeypatch.setattr(pipeline, "load_saved", lambda test_id: saved)
    model = ScriptedModel()
    monkeypatch.setattr(llm, "complete", model)
    running = AppTest.from_file("../app.py", default_timeout=20)
    running.session_state["chosen"] = "Q3"
    running.run()
    assert not running.exception and model.calls == []
    assert "**€535**" in shown(running) and "saved run" in " ".join(c.value for c in running.caption)
