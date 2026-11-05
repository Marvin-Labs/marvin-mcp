import httpx
import pytest
import respx

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
async def test_api_errors_carry_the_code_and_a_hint_for_404():
    respx.post(f"{API}/responses").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "not_found", "message": "Not Found"}}
        )
    )

    with pytest.raises(RuntimeError, match="404: not_found.*private alpha.*marvin@marvin-labs.com"):
        await server.ask_marvin("Hi")
