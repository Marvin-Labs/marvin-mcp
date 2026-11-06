"""The Marvin MCP server.

Talks only to the public API (``/v1/responses``, ``/v1/conversations``,
``/v1/agent_results``) with
the user's own Marvin API key, so it runs anywhere and needs nothing from the
Marvin codebase.

Marvin's answers often take minutes, longer than an MCP client waits on one
tool call. So ``ask_marvin`` starts the turn in the background and waits for
it only up to ``MARVIN_WAIT_SECONDS``. If it is not done by then, the tool
returns the response id and ``get_response`` picks up the wait.

Configuration (environment):

- ``MARVIN_API_KEY``: required, a Marvin API key.
- ``MARVIN_API_URL``: optional; the public production API unless set (for
  example to stg or a local API).
- ``MARVIN_WAIT_SECONDS``: optional, how long one tool call waits for an
  answer (default 50).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

import httpx
from fastmcp import FastMCP

from marvin_mcp.formats import FORMAT_GUIDE

DEFAULT_API_URL = "https://app-api.marvin-labs.com"
# What the access gate's 404 means: the account has no access to the API yet.
ACCESS_MESSAGE = (
    "This is a private alpha version. Please email marvin@marvin-labs.com for access."
)
DEFAULT_WAIT_SECONDS = 50.0
POLL_SECONDS = 3.0
UNFINISHED = ("queued", "in_progress")

mcp = FastMCP(
    "Marvin",
    instructions=(
        "Marvin is Marvin Labs' equity research analyst. Use ask_marvin for questions about "
        "companies, their filings, financials and markets. Answers can take several minutes: "
        "when ask_marvin returns status queued or in_progress, call get_response with the "
        "response_id until it is completed. To follow up in the same conversation, pass the "
        "conversation_id from the previous answer. For reports Marvin's research agents have "
        "already written (earnings call summaries and the like), use list_agent_results and "
        "get_agent_result. If an answer or report has ```financial-table or "
        "```echarts blocks, call get_format_guide for an explanation of their format "
        "conventions."
    ),
)


def _client() -> httpx.AsyncClient:
    key = os.environ.get("MARVIN_API_KEY")
    if not key:
        raise RuntimeError("Set MARVIN_API_KEY to your Marvin API key.")
    return httpx.AsyncClient(
        base_url=os.environ.get("MARVIN_API_URL", DEFAULT_API_URL).rstrip("/") + "/v1",
        headers={"Authorization": f"Bearer {key}"},
        timeout=60.0,
    )


def _wait_seconds() -> float:
    return float(os.environ.get("MARVIN_WAIT_SECONDS", DEFAULT_WAIT_SECONDS))


async def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    async with _client() as client:
        response = await client.request(method, path, **kwargs)
    if response.is_error:
        message = None
        try:
            error = response.json().get("error", {})
            message = error.get("message")
            detail = f"{error.get('code')}: {message}"
        except ValueError:
            detail = response.text[:300]
        # The access gate answers every route with a bare "Not Found"; a missing
        # response or conversation names what it could not find.
        if response.status_code == 404 and message == "Not Found":
            detail += f" ({ACCESS_MESSAGE})"
        raise RuntimeError(f"Marvin API {response.status_code}: {detail}")
    return response.json()


def _summary(response: dict[str, Any]) -> dict[str, Any]:
    """What a model needs from a response object."""
    summary: dict[str, Any] = {
        "status": response["status"],
        "response_id": response["id"],
        "conversation_id": (response.get("conversation") or {}).get("id"),
    }
    if response["status"] == "completed":
        summary["answer"] = response.get("output_text", "")
    elif response["status"] in UNFINISHED:
        summary["next_step"] = "Still working. Call get_response with this response_id."
    elif response.get("error"):
        summary["error"] = response["error"].get("message")
    return summary


async def _wait(response: dict[str, Any], wait_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while response["status"] in UNFINISHED and time.monotonic() < deadline:
        await asyncio.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        response = await _request("GET", f"/responses/{response['id']}")
    return response


@mcp.tool
async def ask_marvin(question: str, conversation_id: str | None = None) -> dict[str, Any]:
    """Ask Marvin a question; returns its answer, or a response_id to poll.

    Args:
        question: The question, in plain language.
        conversation_id: Continue this conversation (the conversation_id of an earlier
            answer, or one from list_conversations). Omit to start a new one.
    """
    body: dict[str, Any] = {"model": "marvin", "input": question, "background": True}
    if conversation_id:
        body["conversation"] = conversation_id
    started = await _request("POST", "/responses", json=body)
    return _summary(await _wait(started, _wait_seconds()))


@mcp.tool
async def get_response(response_id: str) -> dict[str, Any]:
    """Wait a little longer for an answer that ask_marvin left running.

    Args:
        response_id: The response_id ask_marvin returned.
    """
    current = await _request("GET", f"/responses/{response_id}")
    return _summary(await _wait(current, _wait_seconds()))


@mcp.tool
async def cancel_response(response_id: str) -> dict[str, Any]:
    """Stop an answer that is still running.

    Args:
        response_id: The response_id ask_marvin returned.
    """
    summary = _summary(await _request("POST", f"/responses/{response_id}/cancel"))
    if summary["status"] in UNFINISHED:
        summary["next_step"] = "Stopping. Call get_response to confirm it was cancelled."
    return summary


@mcp.tool
async def list_conversations(limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
    """Your Marvin conversations, most recently updated first, including ones from the app.

    Args:
        limit: How many to return (1-100).
        cursor: The next_cursor of a previous call, for the next page.
    """
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    page = await _request("GET", "/conversations", params=params)
    return {
        "conversations": [
            {
                "conversation_id": c["id"],
                "title": c.get("title"),
                "updated_at": c.get("updated_at"),
                "companies": [co.get("ticker") or co.get("name") for co in c.get("companies", [])],
            }
            for c in page["data"]
        ],
        "next_cursor": page.get("next_cursor"),
    }


@mcp.tool
async def get_conversation(
    conversation_id: str, limit: int = 20, cursor: str | None = None
) -> dict[str, Any]:
    """A conversation's questions and answers, oldest first.

    Args:
        conversation_id: From list_conversations or an earlier answer.
        limit: How many turns to return (1-100).
        cursor: The next_cursor of a previous call, for the next page.
    """
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    conversation = await _request("GET", f"/conversations/{conversation_id}")
    items = await _request("GET", f"/conversations/{conversation_id}/items", params=params)
    return {
        "conversation_id": conversation["id"],
        "title": conversation.get("title"),
        "messages": [
            {"role": item["role"], "text": "\n".join(part["text"] for part in item["content"])}
            for item in items["data"]
        ],
        "next_cursor": items.get("next_cursor"),
    }


@mcp.tool
async def list_agent_results(
    company: str | None = None,
    agent: str | None = None,
    mine: bool = False,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Reports Marvin's research agents have written that you can read, newest first.

    Includes your own runs, reports shared with you, and public ones. Read one with
    get_agent_result.

    Args:
        company: Only reports about this company: a ticker (e.g. AAPL) or company id.
        agent: Only reports from this agent, by the `agent` name an earlier result shows.
        mine: Only reports you ran yourself.
        limit: How many to return (1-100).
        cursor: The next_cursor of a previous call, for the next page.
    """
    params: dict[str, Any] = {"limit": limit}
    if company:
        params["company"] = company
    if agent:
        params["agent"] = agent
    if mine:
        params["mine"] = "true"
    if cursor:
        params["cursor"] = cursor
    page = await _request("GET", "/agent_results", params=params)
    return {
        "agent_results": [
            {
                "agent_result_id": r["id"],
                "title": r.get("title"),
                "agent": r.get("agent"),
                "status": r.get("status"),
                "created_at": r.get("created_at"),
                "companies": [co.get("ticker") or co.get("name") for co in r.get("companies", [])],
                "abstract": r.get("abstract"),
            }
            for r in page["data"]
        ],
        "next_cursor": page.get("next_cursor"),
    }


@mcp.tool
async def get_agent_result(agent_result_id: str) -> dict[str, Any]:
    """One research agent report, with its full text in markdown.

    Args:
        agent_result_id: From list_agent_results.
    """
    result = await _request("GET", f"/agent_results/{agent_result_id}")
    summary: dict[str, Any] = {
        "agent_result_id": result["id"],
        "title": result.get("title"),
        "agent": result.get("agent"),
        "status": result["status"],
        "companies": [co.get("ticker") or co.get("name") for co in result.get("companies", [])],
        "report": result.get("output_text"),
    }
    if result["status"] in ("pending", "running") and not result.get("output_text"):
        summary["next_step"] = "The agent is still writing this report. Try again later."
    if result.get("newer_version_in_progress"):
        summary["note"] = "A newer version is being written; this is the previous one."
    return summary


@mcp.tool
def get_format_guide() -> str:
    """How to render the ```financial-table and ```echarts blocks in Marvin's answers.

    Marvin writes tables and charts as JSON in fenced code blocks with those languages.
    Call this once before showing such an answer to the user, to turn the blocks into
    markdown tables (or charts, if you can draw them).
    """
    return FORMAT_GUIDE


def main() -> None:
    if sys.argv[1:2] == ["setup"]:
        from marvin_mcp.setup import main as setup  # setup imports this module

        sys.exit(setup(sys.argv[2:]))
    # stdio only; the banner would just be noise in the client's server log.
    mcp.run(show_banner=False)
