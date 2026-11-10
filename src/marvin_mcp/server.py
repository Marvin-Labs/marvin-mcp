"""The Marvin MCP server.

Talks only to the public API (``/v1/responses``, ``/v1/conversations``,
``/v1/agent_results``, ``/v1/tools``) with
the user's own Marvin API key, so it runs anywhere and needs nothing from the
Marvin codebase.

Besides asking Marvin, it exposes the research tools Marvin's own analysts use
(``/v1/tools``): a company's documents (list, search, read, explore) and its
financials (figures, whole statements, the evidence behind a figure). They
answer in one call each, without a conversation.

Marvin's answers often take minutes, longer than an MCP client waits on one
tool call. So ``ask_marvin`` starts the turn in the background and waits for
it only up to ``MARVIN_WAIT_SECONDS``. If it is not done by then, the tool
returns the response id and ``get_response`` picks up the wait.

Run locally it authenticates with ``MARVIN_API_KEY``. Marvin also hosts this
same server for remote clients (Claude, ChatGPT): there the client signs in
with OAuth, and each tool call forwards that user's access token instead
(``configure_api`` points the tools at the hosting API).

Configuration (environment):

- ``MARVIN_API_KEY``: required when run locally, a Marvin API key.
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
from contextvars import ContextVar
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from marvin_mcp.formats import FORMAT_GUIDE

DEFAULT_API_URL = "https://app-api.marvin-labs.com"
# What the access gate's 404 means: the account has no access to the API yet.
ACCESS_MESSAGE = (
    "This is a private alpha version. Please email marvin@marvin-labs.com for access."
)
DEFAULT_WAIT_SECONDS = 50.0
# How long one research-tool call may take: the API's own longest tool limit
# (a wide financials query) plus a margin.
TOOL_TIMEOUT_SECONDS = 660.0
POLL_SECONDS = 3.0
UNFINISHED = ("queued", "in_progress")

# Tool annotations (MCP ``ToolAnnotations``): Claude runs read-only tools without
# asking each time and always asks before destructive ones; ChatGPT treats a
# tool without readOnlyHint as a write that needs confirmation, and its app
# review wants all three hints set explicitly. Every tool here touches only the
# user's own Marvin data, so none is open-world.
READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
# ask_marvin starts a conversation turn and spends credits: a write, additive
# rather than destructive, and not idempotent (each call is a new turn).
WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
# Stopping an answer cannot be taken back, which both clients count as
# destructive; cancelling twice changes nothing more.
CANCEL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}

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
        "conventions. To research yourself instead of asking Marvin, use the document tools "
        "(list_documents, search_documents, read_document, explore_document) and the "
        "financials tools (query_financials, get_financial_statements, "
        "get_financial_provenance). Cite figures with the `fd:` document ids they return; "
        "read_document opens any of them."
    ),
)


# Every request names this client and the tool that made it, so the API can
# tell MCP traffic from other API callers. The client itself collects nothing.
TOOL_HEADER = "X-Marvin-Client-Tool"
_current_tool: ContextVar[str | None] = ContextVar("marvin_mcp_tool", default=None)


def _package_version() -> str:
    try:
        return version("marvin-mcp")
    except PackageNotFoundError:
        return "unknown"


class _ToolNameMiddleware(Middleware):
    """Remember which tool is running, for the header its requests carry."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[override]
        token = _current_tool.set(context.message.name)
        try:
            return await call_next(context)
        finally:
            _current_tool.reset(token)


mcp.add_middleware(_ToolNameMiddleware())


# Where the tools send their requests when the server is hosted inside the
# Marvin API rather than run locally; set by ``configure_api``.
_api_base_url: str | None = None
_api_transport: httpx.AsyncBaseTransport | None = None


def configure_api(base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Point the tools at ``base_url`` (and optionally a transport), not the environment.

    For hosting the server inside the API itself, where requests go in-process.
    """
    global _api_base_url, _api_transport
    _api_base_url = base_url
    _api_transport = transport


def _credential() -> str:
    # Hosted: the MCP client signed in with OAuth and the request carries the
    # user's access token. Local: the user's own API key.
    if (access_token := get_access_token()) is not None:
        return access_token.token
    key = os.environ.get("MARVIN_API_KEY")
    if not key:
        raise RuntimeError("Set MARVIN_API_KEY to your Marvin API key.")
    return key


def _headers() -> dict[str, str]:
    # "(hosted)" marks the copy the API serves at /v1/mcp, set up by configure_api.
    user_agent = f"marvin-mcp/{_package_version()}"
    if _api_base_url is not None:
        user_agent += " (hosted)"
    headers = {"Authorization": f"Bearer {_credential()}", "User-Agent": user_agent}
    if (tool := _current_tool.get()) is not None:
        headers[TOOL_HEADER] = tool
    return headers


def _client() -> httpx.AsyncClient:
    base_url = _api_base_url or os.environ.get("MARVIN_API_URL", DEFAULT_API_URL)
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/") + "/v1",
        headers=_headers(),
        timeout=60.0,
        transport=_api_transport,
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
    body = response.json()
    # A long /v1/tools call answers 200 before it is done, so an error after
    # that arrives as the error envelope in the 200 body.
    if isinstance(body, dict) and body.keys() == {"error"} and isinstance(body["error"], dict):
        error = body["error"]
        raise RuntimeError(f"Marvin API: {error.get('code')}: {error.get('message')}")
    return body


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


@mcp.tool(title="Ask Marvin", annotations=WRITE)
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


@mcp.tool(title="Get Marvin's answer", annotations=READ)
async def get_response(response_id: str) -> dict[str, Any]:
    """Wait a little longer for an answer that ask_marvin left running.

    Args:
        response_id: The response_id ask_marvin returned.
    """
    current = await _request("GET", f"/responses/{response_id}")
    return _summary(await _wait(current, _wait_seconds()))


@mcp.tool(title="Cancel a running answer", annotations=CANCEL)
async def cancel_response(response_id: str) -> dict[str, Any]:
    """Stop an answer that is still running.

    Args:
        response_id: The response_id ask_marvin returned.
    """
    summary = _summary(await _request("POST", f"/responses/{response_id}/cancel"))
    if summary["status"] in UNFINISHED:
        summary["next_step"] = "Stopping. Call get_response to confirm it was cancelled."
    return summary


@mcp.tool(title="List conversations", annotations=READ)
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


@mcp.tool(title="Read a conversation", annotations=READ)
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


@mcp.tool(title="List research reports", annotations=READ)
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


@mcp.tool(title="Read a research report", annotations=READ)
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


@mcp.tool(title="Table and chart format guide", annotations=READ)
def get_format_guide() -> str:
    """How to render the ```financial-table and ```echarts blocks in Marvin's answers.

    Marvin writes tables and charts as JSON in fenced code blocks with those languages.
    Call this once before showing such an answer to the user, to turn the blocks into
    markdown tables (or charts, if you can draw them).
    """
    return FORMAT_GUIDE


# --- Research tools (/v1/tools) ---------------------------------------------


async def _tool(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST a research tool's arguments, leaving out the ones not given."""
    payload = {key: value for key, value in body.items() if value is not None}
    return await _request("POST", f"/tools/{path}", json=payload, timeout=TOOL_TIMEOUT_SECONDS)


@mcp.tool(title="List a company's documents", annotations=READ)
async def list_documents(
    company: str,
    document_type: list[str] | None = None,
    period: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 10,
    cursor: str | None = None,
) -> dict[str, Any]:
    """A company's documents, newest first: filings, transcripts, press releases,
    Marvin's reports and your own uploads.

    Each document has a short `id` (e.g. `fd:k7qm2xp`) to pass to read_document and
    explore_document; it stays valid.

    Args:
        company: A ticker (e.g. AAPL) or company id.
        document_type: Only these kinds: `filing_document`, `agent_result` (Marvin's
            reports) or `user_document` (your uploads).
        period: Earnings periods, e.g. ["1Q-2024"]; keeps to filings.
        date_from: Only documents dated on or after this day (YYYY-MM-DD).
        date_to: Only documents dated on or before this day (YYYY-MM-DD).
        limit: How many to return.
        cursor: The next_cursor of a previous call, for the next page.
    """
    return await _tool(
        "documents/list",
        {
            "company": company,
            "document_type": document_type,
            "period": period,
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
            "cursor": cursor,
        },
    )


@mcp.tool(title="Search a company's documents", annotations=READ)
async def search_documents(
    company: str,
    query: str,
    terms: list[str] | None = None,
    n_results: int = 10,
    document_type: list[str] | None = None,
    period: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """The passages in a company's documents that answer `query`, best first.

    Read around a passage with read_document, its document `id` and the passage's
    `start_token_offset`.

    Args:
        company: A ticker (e.g. AAPL) or company id.
        query: One sentence saying what you are looking for: the metric, the segment,
            the period.
        terms: Up to 8 keywords or phrases a matching passage would contain.
        n_results: How many documents to return.
        document_type: Only these kinds: `filing_document`, `agent_result` or
            `user_document`.
        period: Earnings periods, e.g. ["1Q-2024"]; keeps to filings.
        date_from: Only documents dated on or after this day (YYYY-MM-DD).
        date_to: Only documents dated on or before this day (YYYY-MM-DD): what was
            known then.
    """
    return await _tool(
        "documents/search",
        {
            "company": company,
            "query": query,
            "terms": terms,
            "n_results": n_results,
            "document_type": document_type,
            "period": period,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


@mcp.tool(title="Read a document", annotations=READ)
async def read_document(
    document_id: str,
    section: str | None = None,
    query: str | None = None,
    start_token_offset: int | None = None,
    max_token_length: int | None = None,
) -> dict[str, Any]:
    """Read a document by its id: whole, a window of it, or the text around a match.

    A long document read without `query` or offsets returns a preview with its
    `sections`. When `has_more` is true, read on from `end_token_offset`.

    Args:
        document_id: An `id` from list_documents or search_documents, or an `fd:`
            citation from the financials tools.
        section: Key of one section from a preview's `sections` (filings only).
        query: Text to find inside the document; overrides start_token_offset.
        start_token_offset: Where to start reading.
        max_token_length: How much to read.
    """
    return await _tool(
        "documents/read",
        {
            "document_id": document_id,
            "section": section,
            "query": query,
            "start_token_offset": start_token_offset,
            "max_token_length": max_token_length,
        },
    )


@mcp.tool(title="Explore a document", annotations=READ)
async def explore_document(
    company: str, document_id: str, question: str, section: str | None = None
) -> dict[str, Any]:
    """What one document says about `question`, as verbatim passages and a summary.

    The document is read for you; `coverage` says how much of it. Cheaper on your
    context than reading a long document whole.

    Args:
        company: The document's company: a ticker or company id.
        document_id: An `id` from list_documents or search_documents.
        question: What you want to know from this document, in one or two sentences.
        section: Key of one section from a read_document preview: read only that.
    """
    return await _tool(
        "documents/explore",
        {
            "company": company,
            "document_id": document_id,
            "question": question,
            "section": section,
        },
    )


@mcp.tool(title="Query financial figures", annotations=READ)
async def query_financials(
    company: str, queries: list[str], bias: str | None = None
) -> dict[str, Any]:
    """A company's reported figures (and consensus estimates) by metric and period.

    Name metrics and periods in plain words, e.g. "revenue and EBITDA FY2023-FY2024"
    or "iPhone net sales last 8 quarters". Put every metric and period you need in
    one call: they are resolved together. A whole statement asked for here comes
    back under `redirects`; read it with get_financial_statements. Not for company
    guidance or outlook.

    Each value has `citations` (`fd:` document ids) and an `id` (or a computed
    figure's `inputs`) for get_financial_provenance.

    Args:
        company: A ticker (e.g. AAPL) or company id.
        queries: Plain-language queries naming metrics and periods.
        bias: "common" for figures normalised to compare across companies,
            "as_reported" for the company's own lines and labels. Omit for the best
            match.
    """
    return await _tool(
        "financials/query", {"company": company, "queries": queries, "bias": bias}
    )


@mcp.tool(title="Get financial statements", annotations=READ)
async def get_financial_statements(
    company: str, statement: str | None = None, periods: list[str] | None = None
) -> dict[str, Any]:
    """A company's financial statements whole: every line in order, with subtotals
    and totals, across periods. Call without `statement` for the list it has.

    `common:<report>` statements are normalised to compare across companies;
    `as_reported:<table>` are the company's own, as it prints them. A row with a
    `dimension` is one segment, product or country, never the consolidated figure.

    Args:
        company: A ticker (e.g. AAPL) or company id.
        statement: A statement id from the list, e.g. "common:income_statement" or
            "as_reported:balance_sheet". Omit for the list.
        periods: Reporting periods, e.g. ["FY-2023", "FY-2024"] or ["2Q-2025"].
            Omit for the last five fiscal years.
    """
    return await _tool(
        "financials/statements",
        {"company": company, "statement": statement, "periods": periods},
    )


@mcp.tool(title="Trace a financial figure to its source", annotations=READ)
async def get_financial_provenance(
    company: str, datapoints: list[str | dict[str, Any]]
) -> dict[str, Any]:
    """Where figures come from: the document and statement line each was read from,
    or the inputs a computed figure was computed from.

    Only when asked where a figure comes from or to verify it; the `citations` the
    financials tools return are enough to cite a figure.

    Args:
        company: A ticker (e.g. AAPL) or company id.
        datapoints: The figures' `id`s (e.g. "dp:4hq8m2xta"), or a computed figure's
            `inputs`, from query_financials or get_financial_statements. Or name a
            stored figure as {"element", "period", "dimensions"}.
    """
    return await _tool(
        "financials/provenance", {"company": company, "datapoints": datapoints}
    )


def main() -> None:
    if sys.argv[1:2] == ["setup"]:
        from marvin_mcp.setup import main as setup  # setup imports this module

        sys.exit(setup(sys.argv[2:]))
    # stdio only; the banner would just be noise in the client's server log.
    mcp.run(show_banner=False)
