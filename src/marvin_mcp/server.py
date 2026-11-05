"""The Marvin MCP server.

Talks only to the public API (``/v1/responses``, ``/v1/conversations``) with
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

DEFAULT_API_URL = "https://app-api.marvin-labs.com"
# What a 404 on every call means: the account has no access to the API yet.
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
        "conversation_id from the previous answer."
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
        try:
            error = response.json().get("error", {})
            detail = f"{error.get('code')}: {error.get('message')}"
        except ValueError:
            detail = response.text[:300]
        if response.status_code == 404:
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
    return _summary(await _request("POST", f"/responses/{response_id}/cancel"))


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


def main() -> None:
    if sys.argv[1:2] == ["setup"]:
        from marvin_mcp.setup import main as setup  # setup imports this module

        sys.exit(setup(sys.argv[2:]))
    # stdio only; the banner would just be noise in the client's server log.
    mcp.run(show_banner=False)
