"""Fibery webhook handler — Fibery-triggered agent runs (Wildkind custom).

Follows the ``agent/webhooks/`` pattern: processing functions live here and
access shared webapp helpers through the module object (``webapp.X``) so tests
that monkeypatch them keep working. The FastAPI routes stay in webapp.py.

Only entities in the Tech department trigger agent work — other Wildkind
departments share the same Fibery databases.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

import httpx
from langchain_core.messages.content import create_text_block

from agent import webapp
from agent.dispatch import dispatch_client
from agent.utils.fibery import (
    FIBERY_API_TOKEN,
    FIBERY_WORKSPACE_URL,
)
from agent.utils.fibery import (
    create_comment as fibery_create_comment,
)
from agent.utils.fibery import (
    fetch_document as fibery_fetch_document,
)
from agent.utils.fibery import (
    fetch_entity_comments as fibery_fetch_entity_comments,
)
from agent.utils.fibery import (
    fetch_entity_repositories as fibery_fetch_entity_repositories,
)
from agent.utils.fibery import (
    fetch_user_email as fibery_fetch_user_email,
)

FIBERY_WEBHOOK_URL_TOKEN = os.environ.get("FIBERY_WEBHOOK_URL_TOKEN", "")

# Only process Fibery tasks belonging to the Tech department
_TECH_DEPARTMENT_ID = "491d5ee0-ca9a-11ee-a1e7-19aa7094fda1"

# Backlog state UUID from Fibery schema (workflow/state_Tools/Task)
_BACKLOG_STATE_ID = "9ac0d04f-a6f9-4271-b34f-a4919460d770"


def generate_thread_id_from_fibery_entity(entity_id: str) -> str:
    """Generate a deterministic thread ID from a Fibery entity ID.

    Args:
        entity_id: The Fibery entity UUID.

    Returns:
        A UUID-formatted thread ID derived from the entity ID.
    """
    hash_bytes = hashlib.sha256(f"fibery-entity:{entity_id}".encode()).hexdigest()
    return (
        f"{hash_bytes[:8]}-{hash_bytes[8:12]}-{hash_bytes[12:16]}-"
        f"{hash_bytes[16:20]}-{hash_bytes[20:32]}"
    )


def parse_repo_field(repo_value: str) -> list[dict[str, str]]:
    """Parse a comma-separated repo field value into repo config dicts.

    Expected format: "owner/repo" or "owner/repo1, owner/repo2"

    Args:
        repo_value: Raw repo field value from Fibery entity.

    Returns:
        List of repo config dicts with 'owner' and 'name' keys.
        Returns empty list if the field is empty or unparseable.
    """
    if not repo_value or not repo_value.strip():
        return []

    configs = []
    for entry in repo_value.split(","):
        entry = entry.strip()
        if "/" not in entry:
            continue
        parts = entry.split("/", 1)
        owner = parts[0].strip()
        name = parts[1].strip()
        if owner and name:
            configs.append({"owner": owner, "name": name})
    return configs


async def fetch_fibery_entity_details(
    database_type: str,
    entity_id: str,
) -> dict[str, Any] | None:
    """Fetch full details of a Fibery entity for building the agent prompt.

    Fetches the entity fields, resolves rich text descriptions via document secrets,
    collects comments, and fetches linked repositories from the Tech/Repository relation.

    Field mapping (Tools/Task schema):
    - Title: Tools/Name (text, UI title)
    - Description: Tools/Description (rich text document)
    - Github Tag: Tools/Github Tag (read-only formula: "[TASK-{PublicId}]")
    - Repositories: Tools/Repositories (collection → Tech/Repository, Full Name = "owner/repo")
    - Lead: Tools/Lead (user, used as assignee)
    - Workflow state: workflow/state (Backlog, In Progress, For Review, Done, etc.)

    Args:
        database_type: The Fibery database type (e.g., "Tools/Task").
        entity_id: The entity UUID.

    Returns:
        Dict with keys: id, title, description, comments, repo_configs, github_tag,
        lead_id, url, database_type. Returns None on failure.
    """
    # Fibery field names use the space prefix, not the full database type.
    # e.g., for "Tools/Task", fields are "Tools/Name", not "Tools/Task/Name".
    space_prefix = database_type.split("/")[0]
    name_field = f"{space_prefix}/Name"
    desc_field = f"{space_prefix}/Description"
    tag_field = f"{space_prefix}/Github Tag"

    brief_field = f"{space_prefix}/Background & Brief"
    ai_specced_field = f"{space_prefix}/AI Specced"

    # Description is a rich text document (not primitive) — needs a nested select
    # to get the document secret, then a separate fetch for the content.
    command = {
        "command": "fibery.entity/query",
        "args": {
            "query": {
                "q/from": database_type,
                "q/select": {
                    "id": "fibery/id",
                    "public_id": "fibery/public-id",
                    "name": name_field,
                    "tag": tag_field,
                    "desc_secret": [desc_field, "Collaboration~Documents/secret"],
                    "brief_secret": [brief_field, "Collaboration~Documents/secret"],
                    "ai_specced": ai_specced_field,
                },
                "q/where": ["=", "fibery/id", "$id"],
                "q/limit": 1,
            },
            "params": {"$id": entity_id},
        },
    }

    async with httpx.AsyncClient(timeout=30) as http_client:
        try:
            response = await http_client.post(
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers={
                    "Authorization": f"Token {FIBERY_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=[command],
                timeout=30,
            )
            response.raise_for_status()
            results = response.json()
        except Exception:
            webapp.logger.exception("Failed to fetch Fibery entity %s", entity_id)
            return None

    if not (results and isinstance(results, list) and results[0].get("success")):
        webapp.logger.error("Fibery entity query failed for %s", entity_id)
        return None

    rows = results[0].get("result", [])
    if not rows:
        return None

    entity = rows[0]

    # Resolve description from document secret (fetched via path select)
    description = ""
    desc_secret = entity.get("desc_secret", "")
    if desc_secret and isinstance(desc_secret, str):
        description = await fibery_fetch_document(desc_secret)

    # Resolve Background & Brief from document secret
    background_brief = ""
    brief_secret = entity.get("brief_secret", "")
    if brief_secret and isinstance(brief_secret, str):
        background_brief = await fibery_fetch_document(brief_secret)

    # Fetch comments
    comments = await fibery_fetch_entity_comments(database_type, entity_id)

    # Fetch linked repositories from the Tech/Repository collection
    repo_configs = await fibery_fetch_entity_repositories(database_type, entity_id)

    title = entity.get("name", "No title")
    github_tag = entity.get("tag", "")
    public_id = entity.get("public_id", "")

    lead_id = ""  # TODO: fetch via nested query if needed

    entity_url = ""
    if FIBERY_WORKSPACE_URL and public_id:
        entity_url = f"{FIBERY_WORKSPACE_URL}/{database_type.replace('/', '-')}/{public_id}"

    return {
        "id": entity_id,
        "title": title,
        "description": description or "No description",
        "background_brief": background_brief,
        "desc_secret": desc_secret if isinstance(desc_secret, str) else "",
        "ai_specced": bool(entity.get("ai_specced")),
        "comments": comments,
        "repo_configs": repo_configs,
        "github_tag": github_tag if isinstance(github_tag, str) else "",
        "lead_id": lead_id,
        "url": entity_url,
        "database_type": database_type,
    }


async def _is_tech_department(database_type: str, entity_id: str) -> bool:
    """Check if a Fibery entity belongs to the Tech department."""
    space_prefix = database_type.split("/")[0]
    dept_field = f"{space_prefix}/Department(s)"
    command = {
        "command": "fibery.entity/query",
        "args": {
            "query": {
                "q/from": database_type,
                "q/select": {
                    "departments": {
                        "q/from": dept_field,
                        "q/select": {"id": "fibery/id"},
                        "q/limit": 50,
                    },
                },
                "q/where": ["=", "fibery/id", "$id"],
                "q/limit": 1,
            },
            "params": {"$id": entity_id},
        },
    }
    async with httpx.AsyncClient(timeout=30) as http_client:
        try:
            response = await http_client.post(
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers={
                    "Authorization": f"Token {FIBERY_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=[command],
                timeout=30,
            )
            response.raise_for_status()
            results = response.json()
        except Exception:
            webapp.logger.exception("Failed to check department for entity %s", entity_id)
            return False

    if not (results and isinstance(results, list) and results[0].get("success")):
        return False

    rows = results[0].get("result", [])
    if not rows:
        return False

    departments = rows[0].get("departments", [])
    return any(dept.get("id") == _TECH_DEPARTMENT_ID for dept in departments)


def _is_state_backlog(state_value: Any) -> bool:
    """Check if a webhook state value represents the Backlog state."""
    if isinstance(state_value, str):
        return state_value.lower() == "backlog"
    if isinstance(state_value, dict):
        if state_value.get("fibery/id") == _BACKLOG_STATE_ID:
            return True
        name = state_value.get("enum/name", "")
        if isinstance(name, str) and name.lower() == "backlog":
            return True
    return False


_SPEC_KEYWORDS = frozenset(
    {
        "flesh out",
        "break down",
        "break this down",
        "requirements",
        "spec",
        "acceptance criteria",
        "review the spec",
        "too vague",
        "detail",
        "sub-tasks",
        "subtasks",
        "sub tasks",
        "flesh this out",
        "specify",
        "add criteria",
        "identify gaps",
        "refine the description",
    }
)


def _is_spec_request(comment: str) -> bool:
    """Check if a comment is requesting spec/requirements work (not implementation)."""
    comment_lower = comment.lower()
    return any(kw in comment_lower for kw in _SPEC_KEYWORDS)


async def _thread_has_active_run(thread_id: str) -> bool:
    """Whether the thread has a run currently executing.

    Only consulted by the autonomous Backlog-spec trigger: a state change must
    never interrupt in-flight work the way a human follow-up comment (which
    goes through ``dispatch_agent_run``'s interrupt strategy) deliberately does.
    """
    client = dispatch_client()
    try:
        runs = await client.runs.list(thread_id, limit=5)
    except Exception:  # noqa: BLE001
        return False
    return any(isinstance(run, dict) and run.get("status") == "running" for run in runs)


def _build_fibery_configurable(
    full_entity: dict[str, Any],
    repo_config: dict[str, str] | None,
    user_email: str | None,
) -> dict[str, Any]:
    """Build the run configurable for a Fibery-triggered run."""
    return {
        "repo": repo_config or {},
        "fibery_entity": {
            "id": full_entity["id"],
            "title": full_entity["title"],
            "url": full_entity["url"],
            "github_tag": full_entity["github_tag"],
            "database_type": full_entity["database_type"],
            "desc_secret": full_entity.get("desc_secret", ""),
            "brief_secret": full_entity.get("brief_secret", ""),
        },
        "user_email": user_email,
        "source": "fibery",
    }


async def _dispatch_fibery_run(
    thread_id: str,
    content_blocks: list[dict[str, Any]],
    configurable: dict[str, Any],
    repo_config: dict[str, str] | None,
    user_email: str | None,
) -> None:
    """Persist thread owner metadata and dispatch an agent run."""
    fibery_entity = configurable["fibery_entity"]
    await webapp.upsert_agent_thread_owner_metadata(
        thread_id,
        source="fibery",
        repo_config=repo_config,
        user_email=user_email or "",
        title=fibery_entity.get("title") or "Fibery entity",
        source_context={"fibery_entity": fibery_entity},
    )
    run = await webapp.dispatch_agent_run(
        thread_id,
        content_blocks,
        configurable,
        source="fibery",
        metadata=webapp._AGENT_VERSION_METADATA,
    )
    webapp.logger.info(
        "LangGraph run dispatched for Fibery thread %s (run=%s)",
        thread_id,
        run.get("run_id") if isinstance(run, dict) else None,
    )


async def process_fibery_backlog_spec(
    entity_id: str,
    database_type: str,
    actor_user_id: str = "",
) -> None:
    """Auto-spec a Fibery entity that moved to Backlog.

    Checks readiness (content + repo), skips if already specced (AI Specced = true),
    and routes to spec-specific prompt. Only does requirements work, never implementation.
    """
    webapp.logger.info(
        "Processing Backlog spec for Fibery entity %s (type=%s)", entity_id, database_type
    )

    if not await _is_tech_department(database_type, entity_id):
        webapp.logger.info("Skipping Backlog spec for %s — not in Tech department", entity_id)
        return

    full_entity = await fetch_fibery_entity_details(database_type, entity_id)
    if not full_entity:
        webapp.logger.error("Failed to fetch Fibery entity details for %s", entity_id)
        return

    # 1. Skip if already specced
    if full_entity.get("ai_specced"):
        webapp.logger.info("Skipping Backlog spec for %s — AI Specced is true", entity_id)
        return

    # 2. Readiness check: content AND repo required
    description = full_entity.get("description", "")
    background_brief = full_entity.get("background_brief", "")
    has_content = (
        description.strip() not in ("", "No description") or background_brief.strip() != ""
    )
    repo_configs = full_entity.get("repo_configs", [])

    missing = []
    if not has_content:
        missing.append("a Description or Background & Brief")
    if not repo_configs:
        missing.append("at least one linked Repository")

    if missing:
        webapp.logger.info(
            "Backlog spec readiness check failed for %s: missing %s", entity_id, missing
        )
        await fibery_create_comment(
            database_type,
            entity_id,
            "⏸️ **Auto-spec paused**\n\n"
            "I can't flesh out this task yet. Please add:\n"
            + "\n".join(f"- {m}" for m in missing)
            + "\n\nOnce added, move the task out of Backlog and back in, "
            "or comment `@openswe flesh out the requirements`.",
        )
        return

    # 3. Resolve user email for GitHub auth
    user_email = None
    if actor_user_id:
        user_email = await fibery_fetch_user_email(actor_user_id)
    if not user_email and full_entity.get("lead_id"):
        user_email = await fibery_fetch_user_email(full_entity["lead_id"])

    title = full_entity["title"]
    github_tag = full_entity["github_tag"]
    entity_url = full_entity["url"]

    # 4. Build spec-specific prompt
    prompt = (
        "A task has been moved to Backlog and needs its requirements fleshed out.\n\n"
        f"## Entity\n{title}"
        + (f" ({github_tag})" if github_tag else "")
        + (f"\n{entity_url}" if entity_url else "")
        + f"\n\n## Entity Description\n{description}\n\n"
        + (f"## Background & Brief\n{background_brief}\n\n" if background_brief else "")
        + "Please flesh out the requirements for this task. "
        'Use `fibery_update_description` to write the spec (use `field="background_brief"` for tech tasks), '
        "`fibery_create_entity` to create sub-tasks if appropriate, "
        "and `fibery_comment` to post a summary of what you added. "
        "After completing spec work, use `fibery_update_field` with "
        'field="Tools/AI Specced" and value="true" to mark the task as specced.'
    )

    content_blocks: list[dict[str, Any]] = [create_text_block(prompt)]

    # 5. Use first repo only (spec work = single run)
    repo_config = repo_configs[0] if repo_configs else None

    if repo_config and not webapp._is_repo_allowed(repo_config):
        webapp.logger.warning(
            "Rejecting Backlog spec: repo '%s/%s' not in allowlist",
            repo_config.get("owner"),
            repo_config.get("name"),
        )
        return

    thread_id = generate_thread_id_from_fibery_entity(entity_id)

    # 6. Autonomous trigger: never interrupt an active run
    if await _thread_has_active_run(thread_id):
        webapp.logger.warning(
            "Skipping Backlog spec for %s — thread %s is already active",
            entity_id,
            thread_id,
        )
        return

    configurable = _build_fibery_configurable(full_entity, repo_config, user_email)
    await _dispatch_fibery_run(thread_id, content_blocks, configurable, repo_config, user_email)


async def process_fibery_entity(
    entity_id: str,
    database_type: str,
    triggering_comment: str = "",
    actor_user_id: str = "",
) -> None:
    """Process a Fibery entity by creating LangGraph thread(s) and run(s).

    For multi-repo entities, spawns a separate run per repo.

    Args:
        entity_id: The Fibery entity UUID.
        database_type: The Fibery database type.
        triggering_comment: The comment body that triggered the run (if comment trigger).
        actor_user_id: The Fibery user ID of the person who triggered the action.
    """
    webapp.logger.info("Processing Fibery entity %s (type=%s)", entity_id, database_type)

    if not await _is_tech_department(database_type, entity_id):
        webapp.logger.info("Skipping Fibery entity %s — not in Tech department", entity_id)
        return

    full_entity = await fetch_fibery_entity_details(database_type, entity_id)
    if not full_entity:
        webapp.logger.error("Failed to fetch Fibery entity details for %s", entity_id)
        return

    # Resolve user email for GitHub auth — try actor first, then entity lead
    user_email = None
    if actor_user_id:
        user_email = await fibery_fetch_user_email(actor_user_id)
    if not user_email and full_entity.get("lead_id"):
        user_email = await fibery_fetch_user_email(full_entity["lead_id"])
    if not user_email:
        webapp.logger.warning(
            "Could not resolve email for Fibery user (actor=%s, lead=%s)",
            actor_user_id,
            full_entity.get("lead_id"),
        )

    title = full_entity["title"]
    description = full_entity["description"]
    background_brief = full_entity.get("background_brief", "")
    github_tag = full_entity["github_tag"]
    entity_url = full_entity["url"]

    if triggering_comment:
        # Comment-triggered: Slack-style prompt focused on the mention request,
        # with entity context as background.
        prompt = (
            "You were mentioned in a Fibery comment.\n\n"
            f"## Entity\n{title}"
            + (f" ({github_tag})" if github_tag else "")
            + (f"\n{entity_url}" if entity_url else "")
            + f"\n\n## Entity Description\n{description}\n\n"
            + (f"## Background & Brief\n{background_brief}\n\n" if background_brief else "")
            + f"## Comment\n{triggering_comment}\n\n"
            "Use `fibery_comment` to communicate on this Fibery entity for clarifications, "
            "status updates, and final summaries. "
            "Use `fibery_state` to update the entity workflow state as you progress."
        )
    else:
        # State-change triggered: full issue-style prompt.
        tag_line = f"## Fibery Tag: {github_tag}\n\n" if github_tag else ""
        url_line = f"## Fibery Entity: {entity_url}\n\n" if entity_url else ""
        prompt = (
            f"Please work on the following issue:\n\n"
            f"## Title: {title}\n\n"
            f"{tag_line}"
            f"{url_line}"
            f"## Description:\n{description}\n\n"
            f"Please analyze this issue and implement the necessary changes. "
            f"When you're done, commit and push your changes. "
            f"Use `fibery_comment` to post updates and `fibery_state` to update workflow state."
        )

    content_blocks: list[dict[str, Any]] = [create_text_block(prompt)]

    # Get repos from linked Tech/Repository entities
    repo_configs = full_entity.get("repo_configs", [])

    is_spec = triggering_comment and _is_spec_request(triggering_comment)

    if not repo_configs:
        if is_spec:
            # Spec work can proceed without a repo — run once with no repo
            webapp.logger.info(
                "No repos linked, but spec request — proceeding without repo for entity %s",
                entity_id,
            )
            repo_configs = [None]
        else:
            webapp.logger.error("No repositories linked to Fibery entity %s", entity_id)
            await fibery_create_comment(
                database_type,
                entity_id,
                "❌ **Agent Error**\n\nNo repositories linked to this entity. "
                "Please link one or more repositories in the Repositories field.",
            )
            return

    # For spec requests on multi-repo entities, only run once to avoid
    # concurrent writes to the same description document.
    if is_spec and len(repo_configs) > 1:
        webapp.logger.info(
            "Spec request on multi-repo entity — using first repo only for entity %s", entity_id
        )
        repo_configs = repo_configs[:1]

    for repo_config in repo_configs:
        if repo_config is not None and not webapp._is_repo_allowed(repo_config):
            webapp.logger.warning(
                "Rejecting Fibery entity: repo '%s/%s' not in allowlist",
                repo_config.get("owner"),
                repo_config.get("name"),
            )
            continue

        # Use entity+repo for thread ID in multi-repo scenarios
        if repo_config is not None and len(repo_configs) > 1:
            thread_id = generate_thread_id_from_fibery_entity(
                f"{entity_id}:{repo_config['owner']}/{repo_config['name']}"
            )
        else:
            thread_id = generate_thread_id_from_fibery_entity(entity_id)

        configurable = _build_fibery_configurable(full_entity, repo_config, user_email)
        # dispatch_agent_run uses multitask_strategy="interrupt": a follow-up on
        # an active thread halts the prior run and resumes with the new message,
        # replacing the old busy-check + queue.
        await _dispatch_fibery_run(thread_id, content_blocks, configurable, repo_config, user_email)


async def process_fibery_comment_trigger(
    entity_id: str,
    database_type: str,
    actor_user_id: str,
    comment_id: str = "",
) -> None:
    """Verify a Fibery comment trigger contains @openswe and process if so.

    Fetches only the specific comment by ID (from the webhook payload) rather
    than loading all comments on the entity.
    """
    if not comment_id:
        webapp.logger.info("No comment_id provided for Fibery entity %s, skipping", entity_id)
        return

    # Fetch the single comment's document secret, then its content
    comment_body = ""
    comment_cmd = {
        "command": "fibery.entity/query",
        "args": {
            "query": {
                "q/from": "comments/comment",
                "q/select": {
                    "id": "fibery/id",
                    "secret": "comment/document-secret",
                },
                "q/where": ["=", "fibery/id", "$id"],
                "q/limit": 1,
            },
            "params": {"$id": comment_id},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers={
                    "Authorization": f"Token {FIBERY_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=[comment_cmd],
            )
            response.raise_for_status()
            results = response.json()
            if results and isinstance(results, list) and results[0].get("success"):
                rows = results[0].get("result", [])
                if rows:
                    secret = rows[0].get("secret", "")
                    if secret:
                        comment_body = await fibery_fetch_document(secret)
        except Exception:
            webapp.logger.exception(
                "Failed to fetch comment %s for entity %s", comment_id, entity_id
            )
            return

    if not comment_body:
        webapp.logger.info("Empty comment body for comment %s on entity %s", comment_id, entity_id)
        return

    # Bot loop prevention: skip if the comment looks like our own bot message
    bot_prefixes = (
        "🔐 **GitHub Authentication Required**",
        "✅ **Pull Request Created**",
        "✅ **Pull Request Updated**",
        "🤖 **Agent Response**",
        "❌ **Agent Error**",
    )
    for prefix in bot_prefixes:
        if comment_body.startswith(prefix):
            webapp.logger.debug("Ignoring Fibery comment: matches bot message prefix")
            return

    if "@openswe" not in comment_body.lower():
        webapp.logger.debug("Ignoring Fibery comment: doesn't mention @openswe")
        return

    webapp.logger.info("Fibery comment mentions @openswe on entity %s, processing", entity_id)
    await process_fibery_entity(
        entity_id,
        database_type,
        triggering_comment=comment_body,
        actor_user_id=actor_user_id,
    )
