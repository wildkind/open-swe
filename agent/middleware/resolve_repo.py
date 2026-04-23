"""Before-agent middleware that resolves repo config from message text.

For A2A callers (or any caller that cannot set ``configurable.repo`` up front)
this scans incoming messages for ``repo:owner/name``, ``github.com/...`` URLs,
or bare ``owner/repo`` tokens. If a repo is detected it is written to the
current run's ``configurable.repo`` and persisted to thread metadata so
subsequent runs on the same thread pick it up automatically.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentState, before_agent
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph_sdk import get_client

from ..utils.repo import resolve_repo_config, upsert_thread_repo_metadata

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
