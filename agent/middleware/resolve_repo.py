"""Middleware that resolves repo config from message text or the sandbox.

For A2A callers (or any caller that cannot set ``configurable.repo`` up front)
this scans incoming messages for ``repo:owner/name``, ``github.com/...`` URLs,
or bare ``owner/repo`` tokens. If a repo is detected it is written to the
current run's ``configurable.repo`` and persisted to thread metadata so
subsequent runs on the same thread pick it up automatically.

A ``before_model`` sibling additionally inspects the sandbox filesystem for a
cloned repo's ``origin`` URL — this catches the case where the user message
contains no repo hint and the agent picks one via ``list_repos`` mid-run.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import shlex
from typing import Any

from langchain.agents.middleware import AgentState, before_agent, before_model
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph_sdk import get_client

from ..utils.github import git_get_remote_url
from ..utils.repo import (
    extract_repo_from_text,
    resolve_repo_config,
    upsert_thread_repo_metadata,
)
from ..utils.sandbox_paths import resolve_sandbox_work_dir
from ..utils.sandbox_state import SANDBOX_BACKENDS

logger = logging.getLogger(__name__)


def _flatten_message_text(messages: list[Any]) -> str:
    """Collect plain text from a list of LangChain-style messages."""
    chunks: list[str] = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_val = block.get("text")
                    if isinstance(text_val, str):
                        chunks.append(text_val)
    return "\n".join(chunks)


@before_agent
async def resolve_repo_from_messages(
    state: AgentState,
    runtime: Runtime,  # noqa: ARG001
) -> dict[str, Any] | None:
    """Populate ``configurable.repo`` from message text when it is missing."""
    try:
        config = get_config()
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id")

        if not thread_id:
            return None

        repo_config = configurable.get("repo")
        if isinstance(repo_config, dict) and repo_config.get("owner") and repo_config.get("name"):
            return None

        text = _flatten_message_text(state.get("messages", []))
        if not text.strip():
            return None

        langgraph_client = get_client()
        resolved = await resolve_repo_config(text, thread_id, langgraph_client)
        if not resolved:
            return None

        # Make it visible to tools running later in this same run. LangGraph's
        # `patch_config` is a shallow copy so the `configurable` dict ref is
        # shared across downstream node invocations; mutating in place is
        # sufficient.
        configurable["repo"] = resolved
        config["configurable"] = configurable

        # Persist for subsequent runs on this thread.
        await upsert_thread_repo_metadata(thread_id, resolved, langgraph_client)

        logger.info(
            "Resolved repo %s/%s from message text for thread %s",
            resolved["owner"],
            resolved["name"],
            thread_id,
        )
    except Exception:
        logger.exception("Error in resolve_repo_from_messages")
    return None


def _discover_repo_from_sandbox(sandbox_backend: Any) -> dict[str, str] | None:
    """Find a single cloned repo under the sandbox work dir and parse owner/name.

    Returns ``None`` if zero or multiple repos are cloned — multiple is
    ambiguous and is left for downstream tools to surface as an error.
    """
    try:
        work_dir = resolve_sandbox_work_dir(sandbox_backend)
    except Exception:
        return None

    find_cmd = f"find {shlex.quote(work_dir)} -mindepth 2 -maxdepth 2 -name .git -type d"
    result = sandbox_backend.execute(find_cmd)
    if result.exit_code != 0:
        return None

    git_dirs = [line.strip() for line in result.output.splitlines() if line.strip()]
    if not git_dirs:
        return None

    candidates: list[dict[str, str]] = []
    for git_dir in git_dirs:
        repo_dir = posixpath.dirname(git_dir)
        url = git_get_remote_url(sandbox_backend, repo_dir)
        if not url:
            continue
        # ``extract_repo_from_text`` matches ``github.com/owner/name`` but
        # would treat a trailing ``.git`` as part of the name.
        cleaned = url.removesuffix(".git").rstrip("/")
        parsed = extract_repo_from_text(cleaned)
        if parsed:
            candidates.append(parsed)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.info(
            "Multiple cloned repos in sandbox (%s); leaving repo config unresolved",
            ", ".join(f"{c['owner']}/{c['name']}" for c in candidates),
        )
    return None


@before_model
async def resolve_repo_from_sandbox(
    state: AgentState,  # noqa: ARG001
    runtime: Runtime,  # noqa: ARG001
) -> dict[str, Any] | None:
    """Populate ``configurable.repo`` by scanning the sandbox for a cloned repo.

    Runs before each model call. Short-circuits when the repo is already
    resolved, so the only real work happens in the narrow A2A window between
    the agent cloning a repo and calling a tool that needs ``configurable.repo``.
    """
    try:
        config = get_config()
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id")
        if not thread_id:
            return None

        repo_config = configurable.get("repo")
        if isinstance(repo_config, dict) and repo_config.get("owner") and repo_config.get("name"):
            return None

        sandbox_backend = SANDBOX_BACKENDS.get(thread_id)
        if not sandbox_backend:
            return None

        resolved = await asyncio.to_thread(_discover_repo_from_sandbox, sandbox_backend)
        if not resolved:
            return None

        configurable["repo"] = resolved
        config["configurable"] = configurable

        await upsert_thread_repo_metadata(thread_id, resolved, get_client())

        logger.info(
            "Resolved repo %s/%s from sandbox for thread %s",
            resolved["owner"],
            resolved["name"],
            thread_id,
        )
    except Exception:
        logger.exception("Error in resolve_repo_from_sandbox")
    return None
