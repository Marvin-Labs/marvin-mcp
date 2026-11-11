"""MCP tasks (``io.modelcontextprotocol/tasks``, SEP-2663) over Marvin's own answers.

FastMCP's stock ``TasksExtension`` runs a tasked tool's body on a Docket worker
and keeps the task in Redis. Marvin needs neither, because an answer already is
a background job that the public API keeps: ``/v1/responses/{id}`` goes queued,
in_progress, then completed, failed or cancelled. It can be read and stopped
with the caller's own credentials from whichever process serves the poll. So
this extension maps the protocol onto that job:

- A tasked ``tools/call`` starts the answer and returns its response id as the
  ``taskId``.
- ``tasks/get`` reads the response. Once it is complete, the result is the same
  one the plain tool would have returned.
- ``tasks/cancel`` cancels the response.

Nothing is stored here, and the API's ownership checks decide who may read or
stop a task.

Only a client on protocol 2026-07-28 that declares the extension on the call
gets a task. Every other client (handshake-era connections, which cannot
negotiate extensions) keeps the plain tool, which waits and then hands back a
``response_id`` for ``get_response``.

Only the protocol is borrowed from ``fastmcp_tasks``: its wire models. A
``tools/call`` result whose ``resultType`` is ``task`` reaches the wire as is,
because the MCP SDK's server runner (mcp >= 2.3) passes extension result types
through unvalidated on 2026-07-28.
"""

from __future__ import annotations

import datetime
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

import mcp_types
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.extensions import (
    MethodBinding,
    ServerExtension,
    read_client_extension_settings,
)
from fastmcp.utilities.tasks import TASKS_EXTENSION_ID
from fastmcp_tasks.models import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    CancelTaskParams,
    CancelTaskResult,
    CreateTaskResult,
    GetTaskParams,
    GetTaskResult,
    TaskStatus,
    UpdateTaskParams,
    UpdateTaskResult,
    missing_capability_error_data,
)
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import MCP_NAME_HEADER, decode_header_value
from mcp_types.jsonrpc import HEADER_MISMATCH
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

if TYPE_CHECKING:
    from fastmcp.server.context import Context
    from fastmcp.server.extensions import ToolCallContinuation, ToolCallOutcome
    from mcp.server.context import ServerRequestContext

# The extensions mechanism, and so these methods, exist only at 2026-07-28.
TASK_METHOD_VERSIONS = frozenset(MODERN_PROTOCOL_VERSIONS)
# An answer is a turn of its conversation and the API keeps it as long as the
# conversation. ttlMs must be a number, so this states a floor, not a deadline.
TASK_TTL_MS = 30 * 24 * 60 * 60 * 1000

Response = dict[str, Any]


class ResponseTasks(ServerExtension):
    """Runs the tool named ``tool`` as a task backed by a background response.

    Args:
        tool: The tool whose calls become tasks; it must declare task support
            (``task=TaskConfig(mode="optional")``) so tools/list advertises it.
        start: Starts an answer from the call's arguments; returns the response.
        fetch: The response with this id, or ``None`` when there is none the
            caller may see.
        cancel: Cancels the response with this id; ``None`` when there is none.
        summarize: The plain tool's answer for a response, so a finished task
            returns exactly what an untasked call would.
        poll_interval_ms: How often a client should poll ``tasks/get``.
    """

    identifier = TASKS_EXTENSION_ID

    def __init__(
        self,
        *,
        tool: str,
        start: Callable[[dict[str, Any]], Awaitable[Response]],
        fetch: Callable[[str], Awaitable[Response | None]],
        cancel: Callable[[str], Awaitable[Response | None]],
        summarize: Callable[[Response], Any],
        poll_interval_ms: int,
    ) -> None:
        self._tool = tool
        self._start = start
        self._fetch = fetch
        self._cancel = cancel
        self._summarize = summarize
        self._poll_interval_ms = poll_interval_ms

    def methods(self) -> Sequence[MethodBinding]:
        return [
            MethodBinding(
                method="tasks/get",
                params_type=GetTaskParams,
                handler=self._handle_get,
                protocol_versions=TASK_METHOD_VERSIONS,
            ),
            MethodBinding(
                method="tasks/update",
                params_type=UpdateTaskParams,
                handler=self._handle_update,
                protocol_versions=TASK_METHOD_VERSIONS,
            ),
            MethodBinding(
                method="tasks/cancel",
                params_type=CancelTaskParams,
                handler=self._handle_cancel,
                protocol_versions=TASK_METHOD_VERSIONS,
            ),
        ]

    async def intercept_tool_call(
        self,
        params: mcp_types.CallToolRequestParams,
        context: Context,
        call_next: ToolCallContinuation,
    ) -> ToolCallOutcome:
        if params.name != self._tool or not _opted_in(context):
            return await call_next()
        response = await self._start(dict(params.arguments or {}))
        return CreateTaskResult(**self._task_fields(response))

    async def _handle_get(
        self, ctx: ServerRequestContext[Any, Any], params: GetTaskParams
    ) -> GetTaskResult:
        _check_task_request(ctx, params.task_id)
        response = await self._fetch(params.task_id)
        if response is None:
            raise _task_not_found(params.task_id)
        fields = self._task_fields(response)
        if fields["status"] == "completed":
            return GetTaskResult(**fields, result=await self._call_tool_result(response))
        if fields["status"] == "failed":
            message = (response.get("error") or {}).get("message") or "The answer failed."
            error = {"code": mcp_types.INTERNAL_ERROR, "message": message}
            return GetTaskResult(**{**fields, "status_message": message}, error=error)
        return GetTaskResult(**fields)

    async def _handle_update(
        self, ctx: ServerRequestContext[Any, Any], params: UpdateTaskParams
    ) -> UpdateTaskResult:
        _check_task_request(ctx, params.task_id)
        # An answer never stops to ask the client for input, so a task never
        # reaches input_required and there is nothing to deliver.
        raise MCPError(
            code=mcp_types.INVALID_PARAMS,
            message=f"Task {params.task_id} is not waiting for input",
        )

    async def _handle_cancel(
        self, ctx: ServerRequestContext[Any, Any], params: CancelTaskParams
    ) -> CancelTaskResult:
        _check_task_request(ctx, params.task_id)
        if await self._cancel(params.task_id) is None:
            raise _task_not_found(params.task_id)
        return CancelTaskResult()

    def _task_fields(self, response: Response) -> dict[str, Any]:
        status = _TASK_STATUS[response["status"]]
        created = datetime.datetime.fromtimestamp(response["created_at"], datetime.UTC)
        return {
            "task_id": response["id"],
            "status": status,
            "status_message": (
                f"Marvin is working ({response['status']})" if status == "working" else None
            ),
            "created_at": created.isoformat(),
            "last_updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "ttl_ms": TASK_TTL_MS,
            "poll_interval_ms": self._poll_interval_ms,
        }

    async def _call_tool_result(self, response: Response) -> dict[str, Any]:
        """The ``CallToolResult`` the plain tool returns for this response."""
        tool = await self.server.get_tool(self._tool)
        if tool is None:
            raise MCPError(code=mcp_types.INTERNAL_ERROR, message=f"No tool {self._tool!r}")
        converted = tool.convert_result(self._summarize(response)).to_mcp_result()
        if isinstance(converted, mcp_types.CallToolResult):
            result = converted
        elif isinstance(converted, tuple):
            content, structured = converted
            result = mcp_types.CallToolResult(content=content, structured_content=structured)
        else:
            result = mcp_types.CallToolResult(content=converted)
        return result.model_dump(by_alias=True, mode="json", exclude_none=True)


_TASK_STATUS: dict[str, TaskStatus] = {
    "queued": "working",
    "in_progress": "working",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _opted_in(context: Context) -> bool:
    """Whether the client declared the tasks extension on this call.

    Handshake-era connections cannot negotiate extensions (the SDK strips them),
    and their result validation has no room for a task result.
    """
    request = context.request_context
    return (
        request is not None
        and request.protocol_version in MODERN_PROTOCOL_VERSIONS
        and context.client_extension_settings(TASKS_EXTENSION_ID) is not None
    )


def _check_task_request(ctx: ServerRequestContext[Any, Any], task_id: str) -> None:
    """The two gates SEP-2663 puts on every ``tasks/*`` request.

    The client must declare the extension on the request itself (-32021
    otherwise), and the routing header ``Mcp-Name``, when sent, must name the
    same task as the body (SEP-2243).
    """
    if read_client_extension_settings(ctx, TASKS_EXTENSION_ID) is None:
        raise MCPError(
            code=MISSING_REQUIRED_CLIENT_CAPABILITY,
            message=(
                f"This request targets the tasks extension ({TASKS_EXTENSION_ID}); "
                "the client did not declare it for this request."
            ),
            data=missing_capability_error_data(),
        )
    try:
        request = get_http_request()
    except RuntimeError:
        return  # not over HTTP: no routing headers
    header = request.headers.get(MCP_NAME_HEADER)
    if header is not None and decode_header_value(header) != task_id:
        raise MCPError(
            code=HEADER_MISMATCH,
            message=f"{MCP_NAME_HEADER} header does not match the request body's 'taskId'",
        )


def _task_not_found(task_id: str) -> MCPError:
    # One answer for a missing id and for someone else's, so ids cannot be probed.
    return MCPError(code=mcp_types.INVALID_PARAMS, message=f"Task {task_id} not found")
