"""stdio MCP server: one tool per decision step.

The agent loop is already a state machine (`Agent.command`). This module exposes that
state machine as separate MCP tools instead of running it to completion, because
TYPE_TEXT needs a string that only the caller can supply: `predict` reports the field
context, the caller answers with `act(text=...)`. No text-model key is required.

Run: jev-mcp   (or: uv run --directory <repo> python -m jev_ultrafast.mcp)
"""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .agent import Agent
from .browser import StalePage
from .model import field_context
from .questions import MAX_STEPS

server = FastMCP("jev-ultrafast")

# The browser lives in the browser-harness daemon, so the run survives across tool calls
# inside this process. One run at a time, as in the demo.
_run = None


def load_environment(path=None):
    """Read the repo's .env so the server can be registered without inline credentials.

    Existing environment variables win, so an MCP registration can still override any value.
    """
    path = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _require_run():
    if _run is None:
        raise ValueError("No run in progress. Call browser_start first.")
    return _run


def _action_of(page, action_id):
    return next((a for a in page["actions"] if a["id"] == action_id), None)


def _decision_payload(agent, extra=None):
    """Shape shared by predict and by the re-prediction after a stale page."""
    state = agent.state
    decision = state["decision"]
    action = _action_of(state["page"], decision["choice"])
    payload = {
        "operation": decision["operation"],
        "choice": decision["choice"],
        "label": action["label"] if action else None,
        "confidence": round(decision["confidence"], 4),
        "status": state["status"],
        "steps_done": len(state["history"]),
    }
    # Hand the caller everything needed to produce the string; the caller owns the wording.
    if decision["operation"] == "TYPE_TEXT" and action is not None:
        payload["needs_text"] = True
        payload["field_context"] = field_context(state["goal"], action, state["page"], state["history"])
    payload.update(extra or {})
    return payload


@server.tool()
def browser_start(url: str, goal: str) -> dict:
    """Open a page and begin a run. One run at a time; an active run is closed first."""
    global _run
    load_environment()
    if _run is not None:
        _run.close()
        _run = None
    _run = Agent(url, goal)
    state = _run.state
    return {
        "status": state["status"],
        "url": state["page"]["url"],
        "title": state["page"]["title"],
        "actions": len(state["page"]["actions"]),
        "step_budget": MAX_STEPS,
    }


@server.tool()
def browser_predict() -> dict:
    """Choose the next operation and target. Returns needs_text with the field context for TYPE_TEXT."""
    agent = _require_run()
    agent.command("predict")
    return _decision_payload(agent)


@server.tool()
def browser_act(text: str = "") -> dict:
    """Execute the predicted step. Pass text when the prediction asked for it.

    A page that changed since the prediction is re-predicted rather than executed, and the
    fresh decision is returned with stale=true; nothing is typed on a page that moved.
    """
    agent = _require_run()
    state = agent.state
    if not state["decision"]:
        raise ValueError("Call browser_predict before browser_act.")
    action = _action_of(state["page"], state["decision"]["choice"])
    if action is not None and action["kind"] == "fill":
        if not text:
            return _decision_payload(agent, {"needs_text": True,
                                             "note": "This step types into a field; call browser_act with text."})
        # Pre-seed the agent's stale-retry cache so field_text() never runs: the caller owns the string.
        context = field_context(state["goal"], action, state["page"], state["history"])
        agent.pending_text = (context, text, {"model": "mcp-caller", "latency_ms": 0, "usage": {}})
    try:
        agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
    except StalePage as exc:
        agent.state["decision"] = None
        agent.state["status"] = "ready"
        agent.command("predict")
        return _decision_payload(agent, {"stale": True, "note": f"Page changed ({exc}); re-predicted."})
    finally:
        agent.pending_text = None
    state = agent.state
    return {
        "status": state["status"],
        "executed": state["history"][-1]["action"] if state["history"] else None,
        "url": state["page"]["url"],
        "steps_done": len(state["history"]),
        "finished": state["status"] in {"done", "blocked"},
    }


@server.tool()
def browser_status() -> dict:
    """Report the run's goal, current page, and the executed steps."""
    agent = _require_run()
    state = agent.state
    return {
        "goal": state["goal"],
        "status": state["status"],
        "url": state["page"]["url"],
        "steps_done": len(state["history"]),
        "history": [
            {"step": h["step"], "kind": h["kind"], "action": h["action"], "text": h["text"]}
            for h in state["history"]
        ],
    }


@server.tool()
def browser_stop() -> dict:
    """Close the run's browser tab."""
    global _run
    if _run is None:
        return {"status": "idle"}
    _run.close()
    _run = None
    return {"status": "closed"}


def main():
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
