import json

import httpx
import pytest
import respx
from fastmcp import Client

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

    result = await server.ask_marvin("What does Apple sell?", conversation_id="conv-1")

    assert result == {
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
async def test_an_answer_still_running_at_the_wait_cap_says_to_poll():
    respx.post(f"{API}/responses").mock(return_value=httpx.Response(200, json=_response("queued")))
    respx.get(f"{API}/responses/resp_1").mock(
        return_value=httpx.Response(200, json=_response("in_progress"))
    )

    result = await server.ask_marvin("Long question")

    assert result["status"] == "in_progress"
    assert "get_response" in result["next_step"]


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

    with pytest.raises(RuntimeError, match=expected):
        await server.get_response("resp_1")



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


async def test_every_tool_declares_a_title_and_explicit_safety_hints():
    """Claude and ChatGPT read these; a tool without them is treated as a destructive write."""
    async with Client(server.mcp) as client:
        tools = await client.list_tools()

    hints = ("readOnlyHint", "destructiveHint", "openWorldHint")
    for tool in tools:
        assert tool.title, tool.name
        assert tool.annotations is not None, tool.name
        assert all(isinstance(getattr(tool.annotations, h), bool) for h in hints), tool.name
        assert tool.annotations.openWorldHint is False, tool.name
    writes = {t.name for t in tools if not t.annotations.readOnlyHint}
    destructive = {t.name for t in tools if t.annotations.destructiveHint}
    assert writes == {"ask_marvin", "cancel_response"}
    assert destructive == {"cancel_response"}


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
