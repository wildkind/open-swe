"""Utilities for extracting repository configuration from text."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from langgraph_sdk.client import LangGraphClient

logger = logging.getLogger(__name__)

_DEFAULT_REPO_OWNER = os.environ.get("DEFAULT_REPO_OWNER", "langchain-ai")

# Known non-repo path prefixes — used to reject false positives when
# scanning for bare ``owner/repo`` tokens in free text.
_NON_REPO_LEFT_TOKENS = frozenset(
    {
        "app",
        "apps",
        "bin",
        "build",
        "config",
        "dist",
        "doc",
        "docs",
        "examples",
        "issues",
        "lib",
        "node_modules",
        "packages",
        "pkg",
        "pull",
        "pulls",
        "src",
        "test",
        "tests",
        "tmp",
        "vendor",
    }
)

# Matches bare ``owner/repo`` tokens: both sides must be plausible GitHub
# identifiers (no dots — that rules out file extensions like foo.py/bar).
_BARE_REPO_RE = re.compile(
    r"(?<![\w/.-])"
    r"([a-zA-Z0-9][a-zA-Z0-9_-]{0,38})"
    r"/"
    r"([a-zA-Z][a-zA-Z0-9_-]{0,99})"
    r"(?![\w./-])"
)


def extract_repo_from_text(text: str, default_owner: str | None = None) -> dict[str, str] | None:
    """Extract owner/name repo config from text containing repo: syntax or GitHub URLs.

    Checks for explicit ``repo:owner/name`` or ``repo owner/name`` first, then
    GitHub URL extraction, then bare ``owner/repo`` tokens as a last resort.

    Returns:
        A dict with ``owner`` and ``name`` keys, or ``None`` if no repo found.
    """
    if default_owner is None:
        default_owner = _DEFAULT_REPO_OWNER
    owner: str | None = None
    name: str | None = None

    if "repo:" in text or "repo " in text:
        match = re.search(r"repo[: ]([a-zA-Z0-9_.\-/]+)", text)
        if match:
            value = match.group(1).rstrip("/")
            if "/" in value:
                owner, name = value.split("/", 1)
            else:
                owner = default_owner
                name = value

    if not owner or not name:
        github_match = re.search(r"github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", text)
        if github_match:
            owner, name = github_match.group(1).split("/", 1)

    if not owner or not name:
        for bare_match in _BARE_REPO_RE.finditer(text):
            candidate_owner, candidate_name = bare_match.group(1), bare_match.group(2)
            if candidate_owner.lower() in _NON_REPO_LEFT_TOKENS:
                continue
            owner, name = candidate_owner, candidate_name
            break

    if owner and name:
        return {"owner": owner, "name": name}
    return None


def extract_repo_config_from_thread(thread: dict[str, Any]) -> dict[str, str] | None:
    """Extract repo config from persisted thread data."""
    metadata = thread.get("metadata")
    if not isinstance(metadata, dict):
        return None

    repo = metadata.get("repo")
    if isinstance(repo, dict):
        owner = repo.get("owner")
        name = repo.get("name")
        if isinstance(owner, str) and owner and isinstance(name, str) and name:
            return {"owner": owner, "name": name}

    owner = metadata.get("repo_owner")
    name = metadata.get("repo_name")
    if isinstance(owner, str) and owner and isinstance(name, str) and name:
        return {"owner": owner, "name": name}

    return None


def _is_not_found_error(exc: Exception) -> bool:
    """Best-effort check for LangGraph 404 errors."""
    return getattr(exc, "status_code", None) == 404


async def upsert_thread_repo_metadata(
    thread_id: str,
    repo_config: dict[str, str],
    langgraph_client: LangGraphClient,
) -> None:
    """Persist the selected repo config on the thread metadata.

    Creates the thread if it does not yet exist. Failures are logged but
    never raised — metadata is a best-effort optimisation.
    """
    try:
        await langgraph_client.threads.update(thread_id=thread_id, metadata={"repo": repo_config})
    except Exception as exc:  # noqa: BLE001
        if _is_not_found_error(exc):
            try:
                await langgraph_client.threads.create(
                    thread_id=thread_id,
                    if_exists="do_nothing",
                    metadata={"repo": repo_config},
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to create thread %s while persisting repo metadata",
                    thread_id,
                )
            return
        logger.exception(
            "Failed to persist repo metadata for thread %s",
            thread_id,
        )


async def resolve_repo_config(
    text: str,
    thread_id: str,
    langgraph_client: LangGraphClient,
    default_owner: str | None = None,
) -> dict[str, str] | None:
    """Resolve a repo config by parsing text then falling back to thread metadata.

    Does NOT apply any environment-level default — callers decide whether to
    fall back to one, since the right default is context-specific.
    """
    repo_config = extract_repo_from_text(text, default_owner=default_owner)
    if repo_config:
        return repo_config

    try:
        thread = await langgraph_client.threads.get(thread_id)
    except Exception as exc:  # noqa: BLE001
        if not _is_not_found_error(exc):
            logger.exception("Failed to fetch thread %s for repo resolution", thread_id)
        return None

    return extract_repo_config_from_thread(thread)
