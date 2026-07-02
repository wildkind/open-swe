"""Require a changie changelog entry before opening a PR (Wildkind custom).

Intercepts ``open_pull_request``: when the target repo has a ``.changie.yaml``
but the branch adds no ``.changes/`` fragment, the call is blocked with a
ToolMessage instructing the agent to run ``changie_new``, commit, and push
first. Repos without changie are unaffected. Infra failures (no sandbox, git
errors) fail open — this guard enforces a convention, it must never wedge a
run.
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from ..utils.sandbox_paths import resolve_repo_dir
from .workflow_push_guard import (
    _backend,
    _thread_id,
    _tool_args,
    _tool_message_for_request,
    _tool_name,
)

logger = logging.getLogger(__name__)


def _blocked_message() -> ToolMessage:
    content = {
        "status": "error",
        "error_type": "ChangieEntryRequired",
        "error": (
            "This repository uses changie (.changie.yaml found) but your branch adds no "
            "`.changes/` fragment. Call the `changie_new` tool with a kind matching your "
            "change type and a concise body, then commit the generated file, push, and "
            "call `open_pull_request` again."
        ),
    }
    return ToolMessage(content=json.dumps(content), tool_call_id="", status="error")


def _missing_changie_entry(request: ToolCallRequest) -> bool:
    """Whether this open_pull_request call should be blocked for a missing entry."""
    args = _tool_args(request)
    repo = args.get("repo")
    base = args.get("base")
    if not isinstance(repo, str) or not repo or not isinstance(base, str) or not base:
        return False

    backend = _backend(_thread_id(request))
    if backend is None:
        return False

    try:
        repo_dir = resolve_repo_dir(backend, repo)
        has_config = backend.execute(f"test -f {shlex.quote(repo_dir)}/.changie.yaml")
        if has_config.exit_code != 0:
            return False

        diff = backend.execute(
            f"git -C {shlex.quote(repo_dir)} diff --name-only "
            f"origin/{shlex.quote(base)}...HEAD -- .changes"
        )
        if diff.exit_code != 0:
            logger.warning(
                "Changie guard: git diff against origin/%s failed (exit %s); allowing PR",
                base,
                diff.exit_code,
            )
            return False
        return not diff.output.strip()
    except Exception:  # noqa: BLE001
        logger.warning("Changie guard check failed; allowing PR", exc_info=True)
        return False


class ChangieGuardMiddleware(AgentMiddleware):
    """Block ``open_pull_request`` until a changie fragment is on the branch."""

    state_schema = AgentState

    def _should_block(self, request: ToolCallRequest) -> bool:
        if _tool_name(request) != "open_pull_request":
            return False
        return _missing_changie_entry(request)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        if self._should_block(request):
            return _tool_message_for_request(_blocked_message(), request)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        if self._should_block(request):
            return _tool_message_for_request(_blocked_message(), request)
        return await handler(request)
