"""Tests for the UI: the brief table conversion (plain pytest) and the Streamlit app (headless AppTest)."""

import pytest  # pytest: fixtures and error assertions
from streamlit.testing.v1 import AppTest  # streamlit: runs app.py in-process without a browser

from src import llm, tools  # project: the model call to replace with a script, and the evidence check
from src.brief_table import SALESPERSON_NOTE, brief_to_rows, rows_to_brief  # project: the code under test
from tests.test_engine import BRIEFS  # project: hand-written briefs
from tests.test_tool_and_guard import CLEAN, ScriptedModel  # project: a scripted stand-in for the model

Q3 = "10 people, 4 hours, wheelchair access, coffee and a projector. Budget €600."


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


def test_app_starts_empty_and_loads_a_test_enquiry():
    app = AppTest.from_file("../app.py", default_timeout=20).run()
    assert not app.exception and app.button[0].disabled
    app.selectbox[0].select("Q3").run()
    assert app.text_area[0].value == Q3 and not app.button[0].disabled


@pytest.mark.parametrize("test_id, banner, must_show", [
    ("Q3", "success", "**€535**"),
    ("Q2", "error", "Not sendable as an offer"),
    ("Q4", "info", "Headcount is missing"),
])
def test_app_shows_status_draft_and_code_computed_prices(monkeypatch, test_id, banner, must_show):
    prose = {"Q3": CLEAN, "Q2": "We cannot meet this.\n{{PRICE_TABLE}}\n{{ALTERNATIVES_TABLE}}",
             "Q4": "Could you tell us how many people will attend?"}[test_id]
    monkeypatch.setattr(llm, "complete", ScriptedModel(BRIEFS[test_id].model_dump(mode="json"), prose))
    app = AppTest.from_file("../app.py", default_timeout=20).run()
    app.selectbox[0].select(test_id).run()
    app.button[0].click().run()
    assert not app.exception
    assert len(getattr(app, banner)) >= 1
    everything = " ".join(element.value for kind in ("markdown", "success", "error", "info", "warning")
                          for element in getattr(app, kind))
    assert must_show in everything
    assert "requires salesperson approval" in everything
