"""Offline contracts for the stdio MCP stepper. No browser, no paid APIs."""

import os
from types import SimpleNamespace

from jev_ultrafast import mcp as stepper


def _agent(operation="CLICK", choice="e1"):
    page = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "actions": [
            {"id": "e1", "kind": "fill" if operation == "TYPE_TEXT" else "click",
             "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
        ],
    }
    page["fingerprint"] = "fp"
    return SimpleNamespace(state={
        "goal": "Find the Ristretto article",
        "page": page,
        "history": [],
        "status": "predicted",
        "decision": {"operation": operation, "choice": choice, "confidence": 0.9},
    })


def test_type_text_payload_carries_the_field_context():
    """The caller cannot write the string without knowing the goal and the field."""
    payload = stepper._decision_payload(_agent(operation="TYPE_TEXT"))
    assert payload["needs_text"] is True
    assert payload["choice"] == "e1"
    assert payload["field_context"]["goal"] == "Find the Ristretto article"
    assert payload["field_context"]["field"]["label"] == "Search"


def test_non_typing_payload_omits_the_field_context():
    payload = stepper._decision_payload(_agent(operation="CLICK", choice="e2"))
    assert "needs_text" not in payload
    assert "field_context" not in payload
    assert payload["label"] == "Go"


def test_action_of_tolerates_a_choice_that_is_not_on_the_page():
    """Controls like DONE and BLOCKED have no node; they must not raise."""
    assert stepper._action_of(_agent().state["page"], "DONE") is None


def test_environment_loading_does_not_clobber_an_explicit_setting(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("JEV_MCP_TEST=from_file\n", encoding="utf-8")
    monkeypatch.setenv("JEV_MCP_TEST", "from_process")
    stepper.load_environment(env_file)
    assert os.environ["JEV_MCP_TEST"] == "from_process"


def test_environment_loading_fills_what_is_missing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nJEV_MCP_MISSING=ts_local\n", encoding="utf-8")
    monkeypatch.delenv("JEV_MCP_MISSING", raising=False)
    stepper.load_environment(env_file)
    assert os.environ["JEV_MCP_MISSING"] == "ts_local"


def test_environment_loading_ignores_a_missing_file(tmp_path):
    stepper.load_environment(tmp_path / "absent.env")
