import json

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp_tasks.client import call_tool_task

from marvin_mcp import server

API = "https://marvin.test/v1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MARVIN_API_KEY", "ak_test")
    monkeypatch.setenv("MARVIN_API_URL", "https://marvin.test")
    monkeypatch.setenv("MARVIN_WAIT_SECONDS", "1")
    monkeypatch.setattr(server, "POLL_SECONDS", 0.01)


def _response(status: str, text: str = "") -> dict:
    return {
        "id": "resp_1",
        "created_at": 1_790_000_000,
        "status": status,
        "conversation": {"id": "conv-1"},
        "output_text": text,
        "error": {"code": "turn_failed", "message": "The turn did not complete."}
        if status == "failed"
        else None,
    }


@respx.mock
async def test_ask_starts_in_the_background_and_waits_for_the_answer():
    create = respx.post(f"{API}/responses").mock(
        return_value=httpx.Response(200, json=_response("queued"))
    )
    respx.get(f"{API}/responses/resp_1").mock(
        side_effect=[
            httpx.Response(200, json=_response("in_progress")),
            httpx.Response(200, json=_response("completed", "Apple sells iPhones.")),
        ]
    )

    # A handshake-era client (Claude desktop and web, ChatGPT): the plain tool.
    async with Client(server.mcp, mode="legacy") as client:
        result = await client.call_tool(
            "ask_marvin", {"question": "What does Apple sell?", "conversation_id": "conv-1"}
        )

    # Through the protocol, so the client also checks the answer against the outputSchema.
    assert result.structured_content == {
        "status": "completed",
        "response_id": "resp_1",
        "conversation_id": "conv-1",
        "answer": "Apple sells iPhones.",
    }
    sent = create.calls.last.request
    assert sent.headers["authorization"] == "Bearer ak_test"
    assert b'"background":true' in sent.content.replace(b" ", b"")
    assert b'"conversation":"conv-1"' in sent.content.replace(b" ", b"")


@respx.mock
async def test_an_answer_still_running_at_the_wait_cap_reports_progress_and_says_to_poll():
    respx.post(f"{API}/responses").mock(return_value=httpx.Response(200, json=_response("queued")))
    respx.get(f"{API}/responses/resp_1").mock(
        return_value=httpx.Response(200, json=_response("in_progress"))
    )
    progress: list[tuple[float, str | None]] = []

    async def on_progress(value: float, total: float | None, message: str | None) -> None:
        progress.append((value, message))

    async with Client(server.mcp, mode="legacy", progress_handler=on_progress) as client:
        result = await client.call_tool("ask_marvin", {"question": "Long question"})

    answer = result.structured_content
    assert answer is not None
    assert answer["status"] == "in_progress"
    assert "get_response" in answer["next_step"]
    # Seconds waited, strictly rising (MCP requires it), while the answer is running.
    values = [value for value, _ in progress]
    assert len(values) > 1
    assert all(earlier < later for earlier, later in zip(values, values[1:]))
    assert all(message == "Marvin is working (in_progress)" for _, message in progress)


@respx.mock
async def test_a_tasks_client_gets_ask_marvin_as_a_task_on_the_response():
    """A 2026-07-28 client with the tasks extension (Claude Code) is not kept waiting:
    the call returns at once, the task is the API's background response, and the
    finished task is the plain tool's answer."""
    create = respx.post(f"{API}/responses").mock(
        return_value=httpx.Response(200, json=_response("queued"))
    )
    respx.get(f"{API}/responses/resp_1").mock(
        side_effect=[
            httpx.Response(200, json=_response("in_progress")),
            httpx.Response(200, json=_response("completed", "Apple sells iPhones.")),
        ]
    )

    async with Client(server.mcp) as client:
        task = await call_tool_task(client, "ask_marvin", {"question": "What does Apple sell?"})
        assert task.task_id == "resp_1"
        assert (await task.status()).status == "working"
        result = await task.result()

    assert create.call_count == 1
    assert result.structured_content == {
        "status": "completed",
        "response_id": "resp_1",
        "conversation_id": "conv-1",
        "answer": "Apple sells iPhones.",
    }


@respx.mock
@pytest.mark.parametrize(
    "answer, expected",
    [
        (httpx.Response(200, json=_response("failed")), "The turn did not complete."),
        # Missing, or another user's: the API says 404 either way.
        (
            httpx.Response(404, json={"error": {"code": "not_found", "message": "x"}}),
            "Task resp_1 not found",
        ),
    ],
    ids=["failed", "not-found"],
)
async def test_a_task_whose_answer_failed_or_is_unknown_is_an_error(answer, expected):
    respx.post(f"{API}/responses").mock(return_value=httpx.Response(200, json=_response("queued")))
    respx.get(f"{API}/responses/resp_1").mock(return_value=answer)

    async with Client(server.mcp) as client:
        task = await call_tool_task(client, "ask_marvin", {"question": "Q"})
        with pytest.raises(Exception, match=expected):
            await task.result()


@respx.mock
async def test_cancelling_a_task_cancels_the_answer():
    respx.post(f"{API}/responses").mock(return_value=httpx.Response(200, json=_response("queued")))
    cancel = respx.post(f"{API}/responses/resp_1/cancel").mock(
        return_value=httpx.Response(200, json=_response("cancelled"))
    )
    respx.get(f"{API}/responses/resp_1").mock(
        return_value=httpx.Response(200, json=_response("cancelled"))
    )

    async with Client(server.mcp) as client:
        task = await call_tool_task(client, "ask_marvin", {"question": "Q"})
        await task.cancel()
        assert (await task.status()).status == "cancelled"

    assert cancel.call_count == 1
    # Task requests are ask_marvin's, for the API's MCP usage attribution.
    assert cancel.calls.last.request.headers[server.TOOL_HEADER] == "ask_marvin"


@respx.mock
@pytest.mark.parametrize(
    "message, expected",
    [
        ("Not Found", r"404: not_found: Not Found \(This is a private alpha.*marvin@marvin-labs.com"),
        ("Response not found", r"404: not_found: Response not found$"),
    ],
    ids=["access-gate", "missing-response"],
)
async def test_only_the_access_gate_404_carries_the_access_hint(message, expected):
    respx.get(f"{API}/responses/resp_1").mock(
        return_value=httpx.Response(404, json={"error": {"code": "not_found", "message": message}})
    )

    async with Client(server.mcp) as client:
        with pytest.raises(ToolError, match=expected):
            await client.call_tool("get_response", {"response_id": "resp_1"})



@respx.mock
async def test_list_agent_results_passes_the_filters_and_shapes_the_page():
    route = respx.get(f"{API}/agent_results").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "ar-1",
                        "title": "Q3 call summary",
                        "agent": "earnings_call_summary",
                        "status": "completed",
                        "created_at": 1,
                        "companies": [{"id": "c-1", "name": "Apple", "ticker": "AAPL"}],
                        "abstract": "Strong quarter.",
                    }
                ],
                "next_cursor": "next",
            },
        )
    )

    result = await server.list_agent_results(company="AAPL", mine=True, limit=5)

    assert dict(route.calls.last.request.url.params) == {
        "limit": "5",
        "company": "AAPL",
        "mine": "true",
    }
    assert result == {
        "agent_results": [
            {
                "agent_result_id": "ar-1",
                "title": "Q3 call summary",
                "agent": "earnings_call_summary",
                "status": "completed",
                "created_at": 1,
                "companies": ["AAPL"],
                "abstract": "Strong quarter.",
            }
        ],
        "next_cursor": "next",
    }


@respx.mock
@pytest.mark.parametrize(
    "status, text, expected_extra",
    [
        ("completed", "## Report", {}),
        ("running", None, {"next_step": "The agent is still writing this report. Try again later."}),
    ],
)
async def test_get_agent_result_returns_the_report(status, text, expected_extra):
    respx.get(f"{API}/agent_results/ar-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "ar-1",
                "title": "Q3 call summary",
                "agent": "earnings_call_summary",
                "status": status,
                "companies": [{"id": "c-1", "name": "Apple", "ticker": None}],
                "output_text": text,
                "newer_version_in_progress": False,
            },
        )
    )

    result = await server.get_agent_result("ar-1")

    assert result == {
        "agent_result_id": "ar-1",
        "title": "Q3 call summary",
        "agent": "earnings_call_summary",
        "status": status,
        "companies": ["Apple"],
        "report": text,
        **expected_extra,
    }


@respx.mock
async def test_a_research_tool_posts_only_the_arguments_given():
    # The API rejects an explicit null for a list argument such as `terms`.
    route = respx.post(f"{API}/tools/documents/search").mock(
        return_value=httpx.Response(200, json={"hits": []})
    )

    result = await server.search_documents("AAPL", "How did services revenue grow?")

    assert json.loads(route.calls.last.request.content) == {
        "company": "AAPL",
        "query": "How did services revenue grow?",
        "n_results": 10,
    }
    assert result == {"hits": []}


@respx.mock
async def test_a_long_tool_call_that_fails_after_its_200_is_an_error():
    # The API answers a long call 200 before it is done, then sends the error envelope.
    respx.post(f"{API}/tools/documents/explore").mock(
        return_value=httpx.Response(
            200,
            content=b'   {"error": {"code": "tool_failed", "message": "Document is empty"}}',
        )
    )

    with pytest.raises(RuntimeError, match="tool_failed: Document is empty"):
        await server.explore_document("AAPL", "fd:k7qm2xp", "What changed?")


async def test_every_tool_declares_a_title_safety_hints_and_an_output_schema():
    """Claude and ChatGPT read these; a tool without hints is treated as a destructive write."""
    async with Client(server.mcp) as client:
        tools = await client.list_tools()

    hints = ("read_only_hint", "destructive_hint", "open_world_hint")
    for tool in tools:
        assert tool.title, tool.name
        assert tool.annotations is not None, tool.name
        assert all(isinstance(getattr(tool.annotations, h), bool) for h in hints), tool.name
        assert tool.annotations.open_world_hint is False, tool.name
        # Every answer is a JSON object with a declared shape (structuredContent).
        assert tool.output_schema is not None and tool.output_schema["type"] == "object", tool.name
    hinted = [(t.name, t.annotations) for t in tools if t.annotations is not None]
    writes = {name for name, hint in hinted if not hint.read_only_hint}
    destructive = {name for name, hint in hinted if hint.destructive_hint}
    assert writes == {"ask_marvin", "cancel_response"}
    assert destructive == {"cancel_response"}
    # The research tools answer the API's own response models (output_schemas.json).
    search = next(t.output_schema for t in tools if t.name == "search_documents")
    assert search is not None and search["title"] == "SearchDocumentsResponse"


@respx.mock
async def test_requests_name_the_client_and_the_tool_that_made_them():
    """The API attributes MCP usage from these headers; the client sends nothing else."""
    route = respx.post(f"{API}/tools/documents/search").mock(
        return_value=httpx.Response(200, json={"hits": []})
    )

    async with Client(server.mcp) as client:
        await client.call_tool("search_documents", {"company": "AAPL", "query": "services"})

    sent = route.calls.last.request.headers
    assert sent["user-agent"].startswith("marvin-mcp/")
    assert "(hosted)" not in sent["user-agent"]
    assert sent[server.TOOL_HEADER] == "search_documents"


TABLE = '{"title": "Revenue", "columns": [], "rows": []}'
CHART = '{"title": {"text": "Margins"}, "series": []}'
ANSWER = f"Up.\n\n```financial-table\n{TABLE}\n```\n\nAnd:\n\n```echarts\n{CHART}\n```\n"


@respx.mock
async def test_an_answers_figures_are_listed_and_show_figure_draws_the_one_asked_for():
    respx.get(f"{API}/responses/resp_1").mock(
        return_value=httpx.Response(200, json=_response("completed", ANSWER))
    )

    async with Client(server.mcp) as client:
        answer = await client.call_tool("get_response", {"response_id": "resp_1"})
        shown = await client.call_tool("show_figure", {"index": 1, "response_id": "resp_1"})

    assert answer.structured_content["figures"] == [
        {"index": 0, "kind": "financial-table", "title": "Revenue"},
        {"index": 1, "kind": "echarts", "title": "Margins"},
    ]
    assert shown.structured_content == {"kind": "echarts", "spec": CHART}
    assert 'chart "Margins"' in shown.content[0].text


async def test_the_figure_view_loads_the_configured_apps_script_and_allows_its_origin(
    monkeypatch,
):
    monkeypatch.setattr(server, "_app_base_url", None)
    monkeypatch.setitem(server._FIGURE_VIEW_UI["csp"], "resourceDomains", ["x"])
    server.configure_api("https://marvin.test", app_url="https://stg-app.test/")
    try:
        async with Client(server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            resources = {str(r.uri): r for r in await client.list_resources()}
            page = await client.read_resource(server.FIGURE_VIEW_URI)
    finally:
        monkeypatch.setattr(server, "_api_base_url", None)

    assert tools["show_figure"].meta["ui"]["resourceUri"] == server.FIGURE_VIEW_URI
    view = resources[server.FIGURE_VIEW_URI]
    assert view.mime_type == "text/html;profile=mcp-app"
    assert view.meta["ui"]["csp"]["resourceDomains"] == ["https://stg-app.test"]
    assert 'src="https://stg-app.test/static/figures/mcp-app-v1.js"' in page[0].text


@respx.mock
async def test_show_figure_says_when_an_answer_has_no_figures():
    respx.get(f"{API}/responses/resp_1").mock(
        return_value=httpx.Response(200, json=_response("in_progress"))
    )

    with pytest.raises(ValueError, match="has no tables or charts"):
        await server.show_figure(0, response_id="resp_1")
