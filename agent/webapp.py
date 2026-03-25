"""Custom FastAPI routes for LangGraph server."""

import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from langchain_core.messages.content import create_text_block
from langgraph_sdk import get_client
from langgraph_sdk.client import LangGraphClient

from .utils.auth import (
    is_bot_token_only_mode,
    persist_encrypted_github_token,
    resolve_github_token_from_email,
)
from .utils.fibery import (
    create_comment as fibery_create_comment,
    fetch_document as fibery_fetch_document,
    fetch_entity as fibery_fetch_entity,
    fetch_entity_comments as fibery_fetch_entity_comments,
    fetch_entity_repositories as fibery_fetch_entity_repositories,
    fetch_user_email as fibery_fetch_user_email,
)
from .utils.comments import get_recent_comments
from .utils.github_app import get_github_app_installation_token
from .utils.github_comments import (
    OPEN_SWE_TAGS,
    build_pr_prompt,
    extract_pr_context,
    fetch_issue_comments,
    fetch_pr_comments_since_last_tag,
    format_github_comment_body_for_prompt,
    get_thread_id_from_branch,
    react_to_github_comment,
    sanitize_github_comment_body,
    verify_github_signature,
)
from .utils.github_token import get_github_token_from_thread
from .utils.github_user_email_map import GITHUB_USER_EMAIL_MAP
from .utils.linear_team_repo_map import LINEAR_TEAM_TO_REPO
from .utils.multimodal import dedupe_urls, extract_image_urls, fetch_image_block
from .utils.slack import (
    add_slack_reaction,
    fetch_slack_thread_messages,
    format_slack_messages_for_prompt,
    get_slack_user_info,
    get_slack_user_names,
    post_slack_thread_reply,
    select_slack_context_messages,
    strip_bot_mention,
    verify_slack_signature,
)

logger = logging.getLogger(__name__)

app = FastAPI()

LINEAR_WEBHOOK_SECRET = os.environ.get("LINEAR_WEBHOOK_SECRET", "")
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID", "")
SLACK_BOT_USERNAME = os.environ.get("SLACK_BOT_USERNAME", "")
SLACK_REPO_OWNER = os.environ.get("SLACK_REPO_OWNER", "langchain-ai")
SLACK_REPO_NAME = os.environ.get("SLACK_REPO_NAME", "open-swe")

LANGGRAPH_URL = os.environ.get("LANGGRAPH_URL") or os.environ.get(
    "LANGGRAPH_URL_PROD", "http://localhost:2024"
)

_AGENT_VERSION_METADATA: dict[str, str] = (
    {"LANGSMITH_AGENT_VERSION": os.environ["LANGCHAIN_REVISION_ID"]}
    if os.environ.get("LANGCHAIN_REVISION_ID")
    else {}
)

ALLOWED_GITHUB_ORGS: frozenset[str] = frozenset(
    org.strip().lower()
    for org in os.environ.get("ALLOWED_GITHUB_ORGS", "").split(",")
    if org.strip()
)

LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY", "")

FIBERY_API_TOKEN = os.environ.get("FIBERY_API_TOKEN", "")
FIBERY_WORKSPACE_URL = os.environ.get("FIBERY_WORKSPACE_URL", "").rstrip("/")
FIBERY_WEBHOOK_URL_TOKEN = os.environ.get("FIBERY_WEBHOOK_URL_TOKEN", "")

_GITHUB_BOT_MESSAGE_PREFIXES = (
    "🔐 **GitHub Authentication Required**",
    "✅ **Pull Request Created**",
    "✅ **Pull Request Updated**",
    "**Pull Request Created**",
    "**Pull Request Updated**",
    "🤖 **Agent Response**",
    "❌ **Agent Error**",
)


def get_repo_config_from_team_mapping(
    team_identifier: str, project_name: str = ""
) -> dict[str, str]:
    """
    Look up repository configuration from LINEAR_TEAM_TO_REPO mapping.

    Supports both legacy flat mapping (team -> repo) and new nested mapping (team -> project -> repo).

    Args:
        team_identifier: Team name or ID to look up (e.g., "LangChain OSS")
        project_name: Name of the project (e.g., "deepagents")

    Returns:
        Repository config dict with 'owner' and 'name' keys. Defaults to langchainplus if not found.
    """
    if not team_identifier or team_identifier not in LINEAR_TEAM_TO_REPO:
        return {"owner": "langchain-ai", "name": "langchainplus"}

    config = LINEAR_TEAM_TO_REPO[team_identifier]

    if "owner" in config and "name" in config:
        return config

    if "projects" in config and project_name:
        project_config = config["projects"].get(project_name)
        if project_config:
            return project_config

    if "default" in config:
        return config["default"]

    return {"owner": "langchain-ai", "name": "langchainplus"}


async def react_to_linear_comment(comment_id: str, emoji: str = "👀") -> bool:
    """Add an emoji reaction to a Linear comment.

    Args:
        comment_id: The Linear comment ID
        emoji: The emoji to react with (default: eyes 👀)

    Returns:
        True if successful, False otherwise
    """
    if not LINEAR_API_KEY:
        return False

    url = "https://api.linear.app/graphql"

    mutation = """
    mutation ReactionCreate($commentId: String!, $emoji: String!) {
        reactionCreate(input: { commentId: $commentId, emoji: $emoji }) {
            success
        }
    }
    """

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                url,
                headers={
                    "Authorization": LINEAR_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "query": mutation,
                    "variables": {"commentId": comment_id, "emoji": emoji},
                },
            )
            response.raise_for_status()
            result = response.json()
            return bool(result.get("data", {}).get("reactionCreate", {}).get("success"))
        except Exception:  # noqa: BLE001
            return False


async def fetch_linear_issue_details(issue_id: str) -> dict[str, Any] | None:
    """Fetch full issue details from Linear API including description and comments.

    Args:
        issue_id: The Linear issue ID

    Returns:
        Full issue data dict, or None if fetch failed
    """
    if not LINEAR_API_KEY:
        return None

    url = "https://api.linear.app/graphql"

    query = """
    query GetIssue($issueId: String!) {
        issue(id: $issueId) {
            id
            identifier
            title
            description
            url
            project {
                id
                name
            }
            team {
                id
                name
                key
            }
            comments {
                nodes {
                    id
                    body
                    createdAt
                    user {
                        id
                        name
                        email
                    }
                }
            }
        }
    }
    """

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                url,
                headers={
                    "Authorization": LINEAR_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "variables": {"issueId": issue_id},
                },
            )
            response.raise_for_status()
            result = response.json()

            return result.get("data", {}).get("issue")
        except httpx.HTTPError:
            return None


def generate_thread_id_from_issue(issue_id: str) -> str:
    """Generate a deterministic thread ID from a Linear issue ID.

    Args:
        issue_id: The Linear issue ID

    Returns:
        A UUID-formatted thread ID derived from the issue ID
    """
    hash_bytes = hashlib.sha256(f"linear-issue:{issue_id}".encode()).hexdigest()
    return (
        f"{hash_bytes[:8]}-{hash_bytes[8:12]}-{hash_bytes[12:16]}-"
        f"{hash_bytes[16:20]}-{hash_bytes[20:32]}"
    )


def generate_thread_id_from_github_issue(issue_id: str) -> str:
    """Generate a deterministic thread ID from a GitHub issue ID."""
    hash_bytes = hashlib.sha256(f"github-issue:{issue_id}".encode()).hexdigest()
    return (
        f"{hash_bytes[:8]}-{hash_bytes[8:12]}-{hash_bytes[12:16]}-"
        f"{hash_bytes[16:20]}-{hash_bytes[20:32]}"
    )


def generate_thread_id_from_slack_thread(channel_id: str, thread_id: str) -> str:
    """Generate a deterministic thread ID from a Slack thread identifier."""
    composite = f"{channel_id}:{thread_id}"
    md5_hex = hashlib.md5(composite.encode("utf-8")).hexdigest()
    return str(uuid.UUID(hex=md5_hex))


def _extract_repo_config_from_thread(thread: dict[str, Any]) -> dict[str, str] | None:
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


def _is_repo_org_allowed(repo_config: dict[str, str]) -> bool:
    """Check if the repo owner/org is in the allowlist.

    Returns True if no allowlist is configured (empty ALLOWED_GITHUB_ORGS),
    or if the repo owner is in the allowlist.
    """
    if not ALLOWED_GITHUB_ORGS:
        return True
    owner = repo_config.get("owner", "").lower()
    return owner in ALLOWED_GITHUB_ORGS


async def _upsert_slack_thread_repo_metadata(
    thread_id: str, repo_config: dict[str, str], langgraph_client: LangGraphClient
) -> None:
    """Persist the selected repo config on the thread metadata."""
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
                    "Failed to create Slack thread %s while persisting repo metadata",
                    thread_id,
                )
            return
        logger.exception(
            "Failed to persist Slack thread repo metadata for thread %s",
            thread_id,
        )


async def check_if_using_repo_msg_sent(
    channel_id: str, thread_ts: str, using_repo_str: str
) -> bool:
    thread_messages = await fetch_slack_thread_messages(channel_id, thread_ts)
    for message in thread_messages:
        if using_repo_str in message.get("text", ""):
            return True
    return False


async def get_slack_repo_config(message: str, channel_id: str, thread_ts: str) -> dict[str, str]:
    """Resolve repository configuration for Slack-triggered runs."""
    default_owner = SLACK_REPO_OWNER.strip() or "langchain-ai"
    default_name = SLACK_REPO_NAME.strip() or "langchainplus"
    thread_id = generate_thread_id_from_slack_thread(channel_id, thread_ts)
    langgraph_client = get_client(url=LANGGRAPH_URL)

    owner: str | None = None
    name: str | None = None

    if "repo:" in message or "repo " in message:
        match = re.search(r"repo[: ]([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", message)
        if match:
            owner, name = match.group(1).split("/", 1)

    if not owner or not name:
        github_match = re.search(r"github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", message)
        if github_match:
            owner, name = github_match.group(1).split("/", 1)

    if not owner or not name:
        try:
            thread = await langgraph_client.threads.get(thread_id)
            thread_repo_config = _extract_repo_config_from_thread(thread)
            if thread_repo_config:
                owner = thread_repo_config["owner"]
                name = thread_repo_config["name"]
        except Exception as exc:  # noqa: BLE001
            if not _is_not_found_error(exc):
                logger.exception(
                    "Failed to fetch Slack thread %s for repo resolution",
                    thread_id,
                )

    if not owner or not name:
        owner = default_owner
        name = default_name

    using_repo_str = f"Using repository: `{owner}/{name}`"
    if not await check_if_using_repo_msg_sent(channel_id, thread_ts, using_repo_str):
        await post_slack_thread_reply(channel_id, thread_ts, using_repo_str)

    return {"owner": owner, "name": name}


async def is_thread_active(thread_id: str) -> bool:
    """Check if a thread is currently active (has a running run).

    Args:
        thread_id: The LangGraph thread ID

    Returns:
        True if the thread status is "busy", False otherwise
    """
    langgraph_client = get_client(url=LANGGRAPH_URL)
    try:
        logger.debug("Fetching thread status for %s from %s", thread_id, LANGGRAPH_URL)
        thread = await langgraph_client.threads.get(thread_id)
        status = thread.get("status", "idle")
        logger.info(
            "Thread %s status check: status=%s, is_busy=%s",
            thread_id,
            status,
            status == "busy",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Failed to get thread status for %s: %s (type: %s) - assuming not active",
            thread_id,
            e,
            type(e).__name__,
        )
        status = "idle"
    return status == "busy"


async def _thread_exists(thread_id: str) -> bool:
    """Return whether a LangGraph thread already exists."""
    langgraph_client = get_client(url=LANGGRAPH_URL)
    try:
        await langgraph_client.threads.get(thread_id)
        return True
    except Exception as exc:  # noqa: BLE001
        if _is_not_found_error(exc):
            return False
        logger.warning("Failed to fetch thread %s, assuming it exists", thread_id)
        return True


async def queue_message_for_thread(
    thread_id: str, message_content: str | list[dict[str, Any]] | dict[str, Any]
) -> bool:
    """Queue a message for a thread that is currently active.

    Stores the message in the langgraph store, namespaced to the thread.
    Supports multiple queued messages by storing them as a list (FIFO order).
    The before_model middleware will pick them up and inject them into state.

    Args:
        thread_id: The LangGraph thread ID
        message_content: The message content to queue (text or content blocks)

    Returns:
        True if successfully queued, False otherwise
    """
    langgraph_client = get_client(url=LANGGRAPH_URL)
    try:
        namespace = ("queue", thread_id)
        key = "pending_messages"

        new_message = {"content": message_content}

        existing_messages: list[dict[str, Any]] = []
        try:
            existing_item = await langgraph_client.store.get_item(namespace, key)
            if existing_item and existing_item.get("value"):
                existing_messages = existing_item["value"].get("messages", [])
        except Exception:  # noqa: BLE001
            logger.debug("No existing queued messages for thread %s", thread_id)

        existing_messages.append(new_message)
        value = {"messages": existing_messages}

        logger.info(
            "Attempting to queue message for thread %s (total queued: %d)",
            thread_id,
            len(existing_messages),
        )
        await langgraph_client.store.put_item(namespace, key, value)
        logger.info("Successfully queued message for thread %s", thread_id)
        return True  # noqa: TRY300
    except Exception:
        logger.exception("Failed to queue message for thread %s", thread_id)
        return False


async def process_linear_issue(  # noqa: PLR0912, PLR0915
    issue_data: dict[str, Any], repo_config: dict[str, str]
) -> None:
    """Process a Linear issue by creating a new LangGraph thread and run.

    Args:
        issue_data: The Linear issue data from webhook (basic info only).
        repo_config: The repo configuration with owner and name.
    """
    issue_id = issue_data.get("id", "")
    logger.info(
        "Processing Linear issue %s for repo %s/%s",
        issue_id,
        repo_config.get("owner"),
        repo_config.get("name"),
    )

    triggering_comment_id = issue_data.get("triggering_comment_id", "")
    if triggering_comment_id:
        await react_to_linear_comment(triggering_comment_id, "👀")

    thread_id = generate_thread_id_from_issue(issue_id)

    full_issue = await fetch_linear_issue_details(issue_id)
    if not full_issue:
        full_issue = issue_data

    user_email = None
    user_name = None
    comment_author = issue_data.get("comment_author", {})
    if comment_author:
        user_email = comment_author.get("email")
        user_name = comment_author.get("name")
    if not user_email:
        creator = full_issue.get("creator", {})
        if creator:
            user_email = creator.get("email")
            user_name = user_name or creator.get("name")
    if not user_email:
        assignee = full_issue.get("assignee", {})
        if assignee:
            user_email = assignee.get("email")
            user_name = user_name or assignee.get("name")

    logger.info("User email for issue %s: %s", issue_id, user_email)

    title = full_issue.get("title", "No title")
    description = full_issue.get("description") or "No description"
    image_urls: list[str] = []
    description_image_urls = extract_image_urls(description)
    if description_image_urls:
        image_urls.extend(description_image_urls)
        logger.debug(
            "Found %d image URL(s) in issue description",
            len(description_image_urls),
        )

    comments = full_issue.get("comments", {}).get("nodes", [])
    comments_text = ""
    triggering_comment = issue_data.get("triggering_comment", "")
    triggering_comment_id = issue_data.get("triggering_comment_id", "")

    bot_message_prefixes = (
        "🔐 **GitHub Authentication Required**",
        "✅ **Pull Request Created**",
        "✅ **Pull Request Updated**",
        "**Pull Request Created**",
        "**Pull Request Updated**",
        "🤖 **Agent Response**",
        "❌ **Agent Error**",
    )

    comment_ids: set[str] = set()
    comment_id_to_index: dict[str, int] = {}
    if comments:
        for i, comment in enumerate(comments):
            comment_id = comment.get("id", "")
            if comment_id:
                comment_ids.add(comment_id)
                comment_id_to_index[comment_id] = i

        relevant_comments = []
        trigger_index = None
        if triggering_comment_id:
            trigger_index = comment_id_to_index.get(triggering_comment_id)
        if trigger_index is not None:
            relevant_comments = comments[trigger_index:]
            logger.debug(
                "Using triggering comment index %d to build relevant comments",
                trigger_index,
            )
        else:
            relevant_comments = get_recent_comments(comments, bot_message_prefixes)

        if relevant_comments:
            comments_text = "\n\n## Comments:\n"
            for comment in relevant_comments:
                user = comment.get("user") or {}
                author = user.get("name", "User")
                body = comment.get("body", "")
                body_image_urls = extract_image_urls(body)
                if body_image_urls:
                    image_urls.extend(body_image_urls)
                    logger.debug(
                        "Found %d image URL(s) in comment by %s",
                        len(body_image_urls),
                        author,
                    )
                if any(body.startswith(prefix) for prefix in bot_message_prefixes):
                    continue
                comments_text += f"\n**{author}:** {body}\n"

    if triggering_comment and triggering_comment_id not in comment_ids:
        if not comments_text:
            comments_text = "\n\n## Comments:\n"
        trigger_author = comment_author.get("name", "Unknown")
        trigger_body = triggering_comment
        trigger_image_urls = extract_image_urls(trigger_body)
        if trigger_image_urls:
            image_urls.extend(trigger_image_urls)
            logger.debug(
                "Found %d image URL(s) in triggering comment by %s",
                len(trigger_image_urls),
                trigger_author,
            )
        comments_text += f"\n**{trigger_author}:** {trigger_body}\n"
        logger.debug(
            "Appended triggering comment %s not present in issue comments list",
            triggering_comment_id or "<missing-id>",
        )

    identifier = full_issue.get("identifier", "") or issue_data.get("identifier", "")

    triggered_by_line = f"## Triggered by: {user_name}\n\n" if user_name else ""
    tag_instruction = (
        f"When calling linear_comment, tag @{user_name} if you are asking them a question, need their input, or are notifying them of something important (e.g. a completed PR). For simple answers, tagging is not required."
        if user_name
        else ""
    )
    prompt = (
        f"Please work on the following issue:\n\n"
        f"## Title: {title}\n\n"
        f"{triggered_by_line}"
        f"## Linear Ticket: {identifier} - Ticket ID: {issue_id}\n\n"
        f"## Description:\n{description}\n"
        f"{comments_text}\n\n"
        f"Please analyze this issue and implement the necessary changes. "
        f"When you're done, commit and push your changes. {tag_instruction}"
    )
    content_blocks: list[dict[str, Any]] = [create_text_block(prompt)]
    if image_urls:
        image_urls = dedupe_urls(image_urls)
        logger.info("Preparing %d image(s) for multimodal content", len(image_urls))
        logger.debug("Image URLs: %s", image_urls)

        async with httpx.AsyncClient() as client:
            for image_url in image_urls:
                image_block = await fetch_image_block(image_url, client)
                if image_block:
                    content_blocks.append(image_block)
        logger.info("Built %d content block(s) for prompt", len(content_blocks))

    linear_project_id = ""
    linear_issue_number = ""
    if identifier and "-" in identifier:
        parts = identifier.split("-", 1)
        linear_project_id = parts[0]
        linear_issue_number = parts[1]

    configurable: dict[str, Any] = {
        "repo": repo_config,
        "linear_issue": {
            "id": issue_id,
            "title": title,
            "url": full_issue.get("url", "") or issue_data.get("url", ""),
            "identifier": identifier,
            "linear_project_id": linear_project_id,
            "linear_issue_number": linear_issue_number,
            "triggering_user_name": user_name or "",
        },
        "user_email": user_email,
        "source": "linear",
    }

    logger.info("Checking if thread %s is active before creating run", thread_id)
    thread_active = await is_thread_active(thread_id)
    logger.info("Thread %s active status: %s", thread_id, thread_active)

    if thread_active:
        logger.info(
            "Thread %s is active (busy), will queue message instead of creating run",
            thread_id,
        )

        queued_payload = {"text": prompt, "image_urls": image_urls}
        queued = await queue_message_for_thread(
            thread_id=thread_id,
            message_content=queued_payload,
        )

        if queued:
            logger.info("Message queued for thread %s, will be processed by middleware", thread_id)
        else:
            logger.error("Failed to queue message for thread %s", thread_id)
    else:
        logger.info("Creating LangGraph run for thread %s", thread_id)
        langgraph_client = get_client(url=LANGGRAPH_URL)
        await langgraph_client.runs.create(
            thread_id,
            "agent",
            input={"messages": [{"role": "user", "content": content_blocks}]},
            config={"configurable": configurable, "metadata": _AGENT_VERSION_METADATA},
            if_not_exists="create",
        )
        logger.info("LangGraph run created successfully for thread %s", thread_id)


async def process_slack_mention(event_data: dict[str, Any], repo_config: dict[str, str]) -> None:
    """Process a Slack app mention by creating or interrupting a thread run."""
    channel_id = event_data.get("channel_id", "")
    thread_ts = event_data.get("thread_ts", "")
    event_ts = event_data.get("event_ts", "")
    user_id = event_data.get("user_id", "")
    text = event_data.get("text", "")
    bot_user_id = event_data.get("bot_user_id", "")

    if not channel_id or not thread_ts or not event_ts:
        logger.warning(
            "Missing Slack event fields (channel_id=%s, thread_ts=%s, event_ts=%s)",
            channel_id,
            thread_ts,
            event_ts,
        )
        return

    reacted = await add_slack_reaction(channel_id, event_ts, "eyes")
    if not reacted:
        logger.debug(
            "Unable to add eyes reaction for Slack message ts=%s in channel=%s",
            event_ts,
            channel_id,
        )

    thread_id = generate_thread_id_from_slack_thread(channel_id, thread_ts)

    user_email = None
    user_name = ""
    if user_id:
        slack_user = await get_slack_user_info(user_id)
        if slack_user:
            profile = slack_user.get("profile", {})
            if isinstance(profile, dict):
                user_email = profile.get("email")
                user_name = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or slack_user.get("real_name")
                    or slack_user.get("name")
                    or ""
                )

    thread_messages = await fetch_slack_thread_messages(channel_id, thread_ts)
    if not any(str(message.get("ts")) == str(event_ts) for message in thread_messages):
        thread_messages.append({"ts": event_ts, "text": text, "user": user_id})

    context_messages, context_mode = select_slack_context_messages(
        thread_messages, event_ts, bot_user_id, SLACK_BOT_USERNAME
    )
    context_user_ids = [
        value
        for value in (message.get("user") for message in context_messages)
        if isinstance(value, str) and value
    ]
    user_names_by_id = await get_slack_user_names(context_user_ids)
    if user_id and user_name and user_id not in user_names_by_id:
        user_names_by_id[user_id] = user_name
    context_text = format_slack_messages_for_prompt(
        context_messages,
        user_names_by_id,
        bot_user_id=bot_user_id,
        bot_username=SLACK_BOT_USERNAME,
    )
    context_source = (
        "the previous message where I was tagged"
        if context_mode == "last_mention"
        else "the beginning of the thread"
    )
    clean_text = (
        strip_bot_mention(text, bot_user_id, bot_username=SLACK_BOT_USERNAME)
        or "(no text in mention)"
    )
    trigger_user = user_name or (f"<@{user_id}>" if user_id else "Unknown user")

    prompt = (
        "You were mentioned in Slack.\n\n"
        f"## Repository\n{repo_config.get('owner')}/{repo_config.get('name')}\n\n"
        f"## Triggered by\n{trigger_user}\n\n"
        f"## Slack Thread\n- Channel: {channel_id}\n- Thread TS: {thread_ts}\n"
        f"- Context starts at: {context_source}\n\n"
        f"## Conversation Context\n{context_text}\n\n"
        f"## Latest Mention Request\n{clean_text}\n\n"
        "Use `slack_thread_reply` to communicate in this Slack thread for clarifications, "
        "status updates, and final summaries."
    )
    content_blocks: list[dict[str, Any]] = [create_text_block(prompt)]

    configurable: dict[str, Any] = {
        "repo": repo_config,
        "slack_thread": {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "triggering_user_id": user_id,
            "triggering_user_name": user_name,
            "triggering_user_email": user_email,
            "triggering_event_ts": event_ts,
        },
        "user_email": user_email,
        "source": "slack",
    }

    langgraph_client = get_client(url=LANGGRAPH_URL)
    await _upsert_slack_thread_repo_metadata(thread_id, repo_config, langgraph_client)
    await langgraph_client.runs.create(
        thread_id,
        "agent",
        input={"messages": [{"role": "user", "content": content_blocks}]},
        config={"configurable": configurable, "metadata": _AGENT_VERSION_METADATA},
        if_not_exists="create",
        multitask_strategy="interrupt",
    )


def verify_linear_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify the Linear webhook signature.

    Args:
        body: Raw request body bytes
        signature: The Linear-Signature header value
        secret: The webhook signing secret

    Returns:
        True if signature is valid, False otherwise
    """
    if not secret:
        logger.warning("LINEAR_WEBHOOK_SECRET is not configured — rejecting webhook request")
        return False

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected, signature)


@app.post("/webhooks/linear")
async def linear_webhook(  # noqa: PLR0911, PLR0912, PLR0915
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Handle Linear webhooks.

    Triggers a new LangGraph run when an issue gets the 'open-swe' label added.
    """
    logger.info("Received Linear webhook")
    body = await request.body()

    signature = request.headers.get("Linear-Signature", "")
    if not verify_linear_signature(body, signature, LINEAR_WEBHOOK_SECRET):
        logger.warning("Invalid webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.exception("Failed to parse webhook JSON")
        return {"status": "error", "message": "Invalid JSON"}

    if payload.get("type") != "Comment":
        logger.debug("Ignoring webhook: not a Comment event")
        return {"status": "ignored", "reason": "Not a Comment event"}

    action = payload.get("action")
    if action != "create":
        logger.debug("Ignoring webhook: action is %s, not create", action)
        return {
            "status": "ignored",
            "reason": f"Comment action is '{action}', only processing 'create'",
        }

    data = payload.get("data", {})

    if data.get("botActor"):
        logger.debug("Ignoring webhook: comment is from a bot")
        return {"status": "ignored", "reason": "Comment is from a bot"}

    comment_body = data.get("body", "")
    bot_message_prefixes = [
        "🔐 **GitHub Authentication Required**",
        "✅ **Pull Request Created**",
        "✅ **Pull Request Updated**",
        "**Pull Request Created**",
        "**Pull Request Updated**",
        "🤖 **Agent Response**",
        "❌ **Agent Error**",
    ]
    for prefix in bot_message_prefixes:
        if comment_body.startswith(prefix):
            logger.debug("Ignoring webhook: comment is our own bot message")
            return {"status": "ignored", "reason": "Comment is our own bot message"}
    if "@openswe" not in comment_body.lower():
        logger.debug("Ignoring webhook: comment doesn't mention @openswe")
        return {"status": "ignored", "reason": "Comment doesn't mention @openswe"}

    issue = data.get("issue", {})
    if not issue:
        logger.debug("Ignoring webhook: no issue data in comment")
        return {"status": "ignored", "reason": "No issue data in comment"}

    # Fetch full issue details to get project info (webhook doesn't include it)
    issue_id = issue.get("id", "")
    full_issue = await fetch_linear_issue_details(issue_id)
    if not full_issue:
        logger.warning("Failed to fetch full issue details, using webhook data")
        full_issue = issue

    team = full_issue.get("team", {})
    team_name = team.get("name", "") if team else ""
    project = full_issue.get("project")
    project_name = project.get("name", "") if project else ""

    team_identifier = team_name.strip() if team_name else ""
    project_key = project_name.strip() if project_name else ""

    repo_config = get_repo_config_from_team_mapping(team_identifier, project_key)

    logger.debug(
        "Team/project lookup result",
        extra={
            "team_name": team_identifier,
            "project_name": project_key,
            "repo_config": repo_config,
        },
    )

    if not _is_repo_org_allowed(repo_config):
        logger.warning(
            "Rejecting Linear webhook: org '%s' not in ALLOWED_GITHUB_ORGS",
            repo_config.get("owner"),
        )
        return {"status": "ignored", "reason": "Repository org not in allowlist"}

    repo_owner = repo_config["owner"]
    repo_name = repo_config["name"]

    issue["triggering_comment"] = comment_body
    issue["triggering_comment_id"] = data.get("id", "")
    comment_user = data.get("user", {})
    if comment_user:
        issue["comment_author"] = comment_user

    logger.info(
        "Accepted webhook for issue '%s' (%s), scheduling background task",
        issue.get("title"),
        issue.get("id"),
    )
    background_tasks.add_task(process_linear_issue, issue, repo_config)

    return {
        "status": "accepted",
        "message": f"Processing issue '{issue.get('title')}' for repo {repo_owner}/{repo_name}",
    }


@app.get("/webhooks/linear")
async def linear_webhook_verify() -> dict[str, str]:
    """Verify endpoint for Linear webhook setup."""
    return {"status": "ok", "message": "Linear webhook endpoint is active"}


@app.post("/webhooks/slack")
async def slack_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Handle Slack Event API webhooks for app mentions."""
    body = await request.body()

    signature = request.headers.get("X-Slack-Signature", "")
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    if not verify_slack_signature(
        body=body,
        timestamp=timestamp,
        signature=signature,
        secret=SLACK_SIGNING_SECRET,
    ):
        logger.warning("Invalid Slack signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.exception("Failed to parse Slack webhook JSON")
        return {"status": "error", "message": "Invalid JSON"}

    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        return {"challenge": challenge}

    if payload.get("type") != "event_callback":
        return {"status": "ignored", "reason": "Not an event callback"}

    event = payload.get("event", {})
    if event.get("type") != "app_mention":
        message_text = event.get("text", "")
        has_username_mention = bool(
            event.get("type") == "message"
            and SLACK_BOT_USERNAME
            and f"@{SLACK_BOT_USERNAME}" in message_text
        )
        has_id_mention = bool(
            event.get("type") == "message"
            and SLACK_BOT_USER_ID
            and f"<@{SLACK_BOT_USER_ID}>" in message_text
        )
        if not (has_username_mention or has_id_mention):
            return {"status": "ignored", "reason": "Not an app_mention event"}

    if event.get("subtype") == "bot_message" or event.get("bot_id"):
        return {"status": "ignored", "reason": "Event from a bot"}

    channel_id = event.get("channel", "")
    event_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or event_ts
    user_id = event.get("user", "")
    text = event.get("text", "")
    if not channel_id or not event_ts or not thread_ts:
        return {"status": "ignored", "reason": "Missing channel/thread timestamp"}

    bot_user_id = SLACK_BOT_USER_ID
    if not bot_user_id:
        authorizations = payload.get("authorizations", [])
        if isinstance(authorizations, list) and authorizations:
            auth_user_id = authorizations[0].get("user_id")
            if isinstance(auth_user_id, str):
                bot_user_id = auth_user_id
    if not bot_user_id:
        authed_users = payload.get("authed_users", [])
        if isinstance(authed_users, list) and authed_users:
            first_user = authed_users[0]
            if isinstance(first_user, str):
                bot_user_id = first_user

    if bot_user_id and user_id == bot_user_id:
        return {"status": "ignored", "reason": "Event from this bot user"}

    event_data = {
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "event_ts": event_ts,
        "user_id": user_id,
        "text": text,
        "bot_user_id": bot_user_id,
    }
    repo_config = await get_slack_repo_config(text, channel_id, thread_ts)

    if not _is_repo_org_allowed(repo_config):
        logger.warning(
            "Rejecting Slack webhook: org '%s' not in ALLOWED_GITHUB_ORGS",
            repo_config.get("owner"),
        )
        return {"status": "ignored", "reason": "Repository org not in allowlist"}

    background_tasks.add_task(process_slack_mention, event_data, repo_config)

    return {"status": "accepted", "message": "Slack mention queued"}


@app.get("/webhooks/slack")
async def slack_webhook_verify() -> dict[str, str]:
    """Verify endpoint for Slack webhook setup."""
    return {"status": "ok", "message": "Slack webhook endpoint is active"}


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


_SUPPORTED_GH_EVENTS = frozenset(
    ["issue_comment", "issues", "pull_request_review_comment", "pull_request_review"]
)
_SUPPORTED_GH_ISSUE_ACTIONS = frozenset(["edited", "opened", "reopened"])


def _build_github_issue_comments_text(comments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for comment in comments:
        body = comment.get("body", "")
        if not body or any(body.startswith(prefix) for prefix in _GITHUB_BOT_MESSAGE_PREFIXES):
            continue
        author = comment.get("author", "unknown")
        formatted_body = format_github_comment_body_for_prompt(author, body)
        lines.append(f"\n**{author}:**\n{formatted_body}\n")

    if not lines:
        return ""
    return "\n\n## Comments:\n" + "".join(lines)


def build_github_issue_prompt(
    repo_config: dict[str, str],
    issue_number: int,
    issue_id: str,
    title: str,
    body: str,
    comments: list[dict[str, Any]],
    *,
    github_login: str,
    issue_author: str = "",
) -> str:
    """Build the user prompt for a GitHub issue-triggered run."""
    triggered_by_line = f"## Triggered by: {github_login}\n\n" if github_login else ""
    comments_text = _build_github_issue_comments_text(comments)
    sanitized_title = sanitize_github_comment_body(title)
    formatted_body = format_github_comment_body_for_prompt(issue_author or github_login, body)
    return (
        "Please work on the following GitHub issue:\n\n"
        f"## Repository: {repo_config.get('owner')}/{repo_config.get('name')}\n\n"
        f"{triggered_by_line}"
        f"## GitHub Issue: #{issue_number} - Issue ID: {issue_id}\n\n"
        f"## Title: {sanitized_title}\n\n"
        f"## Description:\n{formatted_body}\n"
        f"{comments_text}\n\n"
        "Please analyze this issue and implement the necessary changes. "
        "When you need to communicate on GitHub, use `github_comment` with the issue number."
    )


def build_github_issue_followup_prompt(github_login: str, comment_body: str) -> str:
    """Build the prompt for a follow-up GitHub issue comment."""
    return (
        f"**{github_login}:**\n{format_github_comment_body_for_prompt(github_login, comment_body)}"
    )


def build_github_issue_update_prompt(github_login: str, title: str, body: str) -> str:
    """Build the prompt for a follow-up GitHub issue title/body update."""
    sanitized_title = sanitize_github_comment_body(title)
    formatted_body = format_github_comment_body_for_prompt(github_login, body)
    return (
        f"**{github_login}:** updated the GitHub issue title/body.\n\n"
        f"Title: {sanitized_title}\n\n"
        f"Description:\n{formatted_body}"
    )


async def _trigger_or_queue_run(
    thread_id: str,
    prompt: str,
    *,
    github_login: str,
    repo_config: dict[str, str],
    pr_number: int,
) -> None:
    """Create a new agent run or queue the message if the thread is busy."""
    thread_active = await is_thread_active(thread_id)
    if thread_active:
        logger.info("Thread %s is busy, queuing GitHub PR comment message", thread_id)
        await queue_message_for_thread(thread_id, prompt)
        return

    logger.info("Creating LangGraph run for thread %s from GitHub PR comment", thread_id)
    langgraph_client = get_client(url=LANGGRAPH_URL)
    await langgraph_client.runs.create(
        thread_id,
        "agent",
        input={"messages": [{"role": "user", "content": prompt}]},
        config={
            "configurable": {
                "source": "github",
                "github_login": github_login,
                "repo": repo_config,
                "pr_number": pr_number,
            },
            "metadata": _AGENT_VERSION_METADATA,
        },
        if_not_exists="create",
    )
    logger.info("LangGraph run created for thread %s from GitHub PR comment", thread_id)


async def _get_or_resolve_thread_github_token(thread_id: str, email: str) -> str | None:
    """Resolve and persist a GitHub token for a thread when available.

    In bot-token-only mode, returns a fresh GitHub App installation token
    instead of resolving per-user OAuth tokens.
    """
    if is_bot_token_only_mode():
        bot_token = await get_github_app_installation_token()
        if bot_token:
            try:
                await persist_encrypted_github_token(thread_id, bot_token)
            except Exception:
                logger.warning("Could not persist bot token for thread %s", thread_id)
            return bot_token
        logger.warning("Bot-token-only mode but GitHub App token unavailable")
        return None

    github_token, _encrypted_token = await get_github_token_from_thread(thread_id)
    if github_token:
        return github_token

    auth_result = await resolve_github_token_from_email(email)
    github_token = auth_result.get("token")
    if not github_token:
        return None

    try:
        await persist_encrypted_github_token(thread_id, github_token)
    except Exception:
        logger.warning("Could not persist GitHub token for thread %s", thread_id)
    return github_token


async def process_github_pr_comment(payload: dict[str, Any], event_type: str) -> None:
    """Process a GitHub PR comment that tagged @open-swe.

    Retrieves the existing thread token, reacts with 👀, fetches all comments
    since the last @open-swe tag, then creates or queues a new run.

    Args:
        payload: The parsed GitHub webhook payload.
        event_type: One of 'issue_comment', 'pull_request_review_comment',
                    'pull_request_review'.
    """
    (
        repo_config,
        pr_number,
        branch_name,
        github_login,
        pr_url,
        comment_id,
        node_id,
    ) = await extract_pr_context(payload, event_type)

    logger.info(
        "Processing GitHub PR comment: event=%s, pr=%s, branch=%s",
        event_type,
        pr_number,
        branch_name,
    )

    thread_id = get_thread_id_from_branch(branch_name) if branch_name else None
    if not thread_id:
        logger.warning("Could not extract thread_id from branch '%s', skipping", branch_name)
        return

    email = GITHUB_USER_EMAIL_MAP.get(github_login, "")
    if not email:
        logger.warning("No email mapping for GitHub user '%s', skipping", github_login)
        return

    github_token = await _get_or_resolve_thread_github_token(thread_id, email)
    if not github_token:
        logger.warning("No GitHub token for thread %s, skipping", thread_id)
        return

    if comment_id:
        await react_to_github_comment(
            repo_config,
            comment_id,
            event_type=event_type,
            token=github_token,
            pull_number=pr_number,
            node_id=node_id,
        )

    if not pr_number:
        logger.warning("No PR number found in payload, skipping")
        return

    comments = await fetch_pr_comments_since_last_tag(repo_config, pr_number, token=github_token)
    if not comments:
        logger.info("No comments found since last @open-swe tag for PR %s", pr_number)
        return

    prompt = build_pr_prompt(comments, pr_url)
    await _trigger_or_queue_run(
        thread_id,
        prompt,
        github_login=github_login,
        repo_config=repo_config,
        pr_number=pr_number,
    )


async def process_github_issue(payload: dict[str, Any], event_type: str) -> None:
    """Process a GitHub issue or issue comment that tagged @open-swe."""
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    repo_config = {
        "owner": repo.get("owner", {}).get("login", ""),
        "name": repo.get("name", ""),
    }

    issue_id = str(issue.get("id", ""))
    issue_number = issue.get("number")
    github_login = payload.get("sender", {}).get("login", "")
    issue_url = issue.get("html_url", "") or issue.get("url", "")
    title = issue.get("title", "No title")
    description = issue.get("body") or "No description"
    issue_author = issue.get("user", {}).get("login", "")

    logger.info(
        "Processing GitHub issue: event=%s, issue=%s, repo=%s/%s",
        event_type,
        issue_number,
        repo_config.get("owner"),
        repo_config.get("name"),
    )

    if not issue_id or not issue_number:
        logger.warning("Missing GitHub issue id/number, skipping")
        return

    email = GITHUB_USER_EMAIL_MAP.get(github_login, "")
    if not email:
        logger.warning("No email mapping for GitHub user '%s', skipping", github_login)
        return

    thread_id = generate_thread_id_from_github_issue(issue_id)
    existing_thread = await _thread_exists(thread_id)
    github_token = await _get_or_resolve_thread_github_token(thread_id, email)
    app_token = await get_github_app_installation_token()
    reaction_token = github_token or app_token
    comment = payload.get("comment", {})
    comment_id = comment.get("id")
    if event_type == "issue_comment" and comment_id:
        if not reaction_token:
            logger.warning("No GitHub token available to react to issue comment %s", comment_id)
        else:
            reacted = await react_to_github_comment(
                repo_config,
                comment_id,
                event_type="issue_comment",
                token=reaction_token,
            )
            if not reacted:
                logger.warning("Failed to react to GitHub issue comment %s", comment_id)

    if existing_thread:
        if event_type == "issue_comment":
            prompt = build_github_issue_followup_prompt(
                comment.get("user", {}).get("login", github_login) or github_login,
                comment.get("body", ""),
            )
        else:
            prompt = build_github_issue_update_prompt(github_login, title, description)
    else:
        comments = await fetch_issue_comments(
            repo_config, issue_number, token=github_token or app_token
        )
        if comment_id and not any(item.get("comment_id") == comment_id for item in comments):
            comments.append(
                {
                    "body": comment.get("body", ""),
                    "author": comment.get("user", {}).get("login", "unknown"),
                    "created_at": comment.get("created_at", ""),
                    "comment_id": comment_id,
                }
            )
            comments.sort(key=lambda item: item.get("created_at", ""))

        prompt = build_github_issue_prompt(
            repo_config,
            issue_number,
            issue_id,
            title,
            description,
            comments,
            github_login=github_login,
            issue_author=issue_author,
        )
    configurable: dict[str, Any] = {
        "source": "github",
        "github_login": github_login,
        "repo": repo_config,
        "github_issue": {
            "id": issue_id,
            "number": issue_number,
            "title": title,
            "url": issue_url,
        },
    }

    thread_active = await is_thread_active(thread_id)
    if thread_active:
        logger.info("Thread %s is busy, queuing GitHub issue message", thread_id)
        await queue_message_for_thread(thread_id, prompt)
        return

    logger.info("Creating LangGraph run for thread %s from GitHub issue", thread_id)
    langgraph_client = get_client(url=LANGGRAPH_URL)
    await langgraph_client.runs.create(
        thread_id,
        "agent",
        input={"messages": [{"role": "user", "content": prompt}]},
        config={"configurable": configurable, "metadata": _AGENT_VERSION_METADATA},
        if_not_exists="create",
    )
    logger.info("LangGraph run created for thread %s from GitHub issue", thread_id)


@app.post("/webhooks/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Handle GitHub webhooks for issue and PR events that tag @open-swe."""
    body = await request.body()

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_github_signature(body, signature, secret=GITHUB_WEBHOOK_SECRET):
        logger.warning("Invalid GitHub webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type not in _SUPPORTED_GH_EVENTS:
        logger.info("Ignoring unsupported GitHub event type: %s", event_type)
        return {"status": "ignored", "reason": f"Unsupported event type: {event_type}"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.exception("Failed to parse GitHub webhook JSON")
        return {"status": "error", "message": "Invalid JSON"}

    # Check org allowlist
    webhook_repo = payload.get("repository", {})
    webhook_repo_config = {
        "owner": webhook_repo.get("owner", {}).get("login", ""),
        "name": webhook_repo.get("name", ""),
    }
    if not _is_repo_org_allowed(webhook_repo_config):
        logger.warning(
            "Rejecting GitHub webhook: org '%s' not in ALLOWED_GITHUB_ORGS",
            webhook_repo_config.get("owner"),
        )
        return {"status": "ignored", "reason": "Repository org not in allowlist"}

    issue = payload.get("issue", {})
    is_pull_request_comment = bool(event_type == "issue_comment" and issue.get("pull_request"))
    is_issue_comment = bool(event_type == "issue_comment" and not issue.get("pull_request"))
    is_issue_event = event_type == "issues"

    if is_issue_event:
        action = payload.get("action", "")
        if action not in _SUPPORTED_GH_ISSUE_ACTIONS:
            logger.info("Ignoring unsupported GitHub issue action: %s", action)
            return {"status": "ignored", "reason": f"Unsupported GitHub issue action: {action}"}
        if action == "edited":
            changes = payload.get("changes", {})
            if not any(field in changes for field in ("body", "title")):
                logger.info("Ignoring GitHub issue edit without title/body changes")
                return {"status": "ignored", "reason": "Issue edit did not change title or body"}

        issue_text = f"{issue.get('title', '')}\n\n{issue.get('body', '')}".lower()
        if not any(tag in issue_text for tag in OPEN_SWE_TAGS):
            logger.info("Ignoring issue that does not mention @openswe or @open-swe")
            return {"status": "ignored", "reason": "Issue does not mention @openswe or @open-swe"}

        logger.info("Accepted GitHub issue webhook, scheduling background task")
        background_tasks.add_task(process_github_issue, payload, event_type)
        return {"status": "accepted", "message": "Processing GitHub issue event"}

    comment = payload.get("comment") or payload.get("review", {})
    comment_body = (comment.get("body") or "") if comment else ""
    if not any(tag in comment_body.lower() for tag in OPEN_SWE_TAGS):
        logger.info("Ignoring comment that does not mention @openswe or @open-swe")
        return {"status": "ignored", "reason": "Comment does not mention @openswe or @open-swe"}

    logger.info("Accepted GitHub webhook: event=%s, scheduling background task", event_type)
    if is_pull_request_comment or event_type in {
        "pull_request_review_comment",
        "pull_request_review",
    }:
        background_tasks.add_task(process_github_pr_comment, payload, event_type)
        return {"status": "accepted", "message": f"Processing {event_type} event"}

    if is_issue_comment:
        background_tasks.add_task(process_github_issue, payload, event_type)
        return {"status": "accepted", "message": "Processing GitHub issue comment event"}

    logger.info("Ignoring unsupported GitHub payload shape for event=%s", event_type)
    return {"status": "ignored", "reason": f"Unsupported payload for event type: {event_type}"}


# ---------------------------------------------------------------------------
# Fibery webhook
# ---------------------------------------------------------------------------


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
            logger.exception("Failed to fetch Fibery entity %s", entity_id)
            return None

    if not (results and isinstance(results, list) and results[0].get("success")):
        logger.error("Fibery entity query failed for %s", entity_id)
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


# Backlog state UUID from Fibery schema (workflow/state_Tools/Task)
_BACKLOG_STATE_ID = "9ac0d04f-a6f9-4271-b34f-a4919460d770"


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


_SPEC_KEYWORDS = frozenset({
    "flesh out", "break down", "break this down", "requirements", "spec",
    "acceptance criteria", "review the spec", "too vague", "detail",
    "sub-tasks", "subtasks", "sub tasks", "flesh this out", "specify",
    "add criteria", "identify gaps", "refine the description",
})


def _is_spec_request(comment: str) -> bool:
    """Check if a comment is requesting spec/requirements work (not implementation)."""
    comment_lower = comment.lower()
    return any(kw in comment_lower for kw in _SPEC_KEYWORDS)


async def process_fibery_backlog_spec(
    entity_id: str,
    database_type: str,
    actor_user_id: str = "",
) -> None:
    """Auto-spec a Fibery entity that moved to Backlog.

    Checks readiness (content + repo), skips if already specced (AI Specced = true),
    and routes to spec-specific prompt. Only does requirements work, never implementation.
    """
    logger.info("Processing Backlog spec for Fibery entity %s (type=%s)", entity_id, database_type)

    full_entity = await fetch_fibery_entity_details(database_type, entity_id)
    if not full_entity:
        logger.error("Failed to fetch Fibery entity details for %s", entity_id)
        return

    # 1. Skip if already specced
    if full_entity.get("ai_specced"):
        logger.info("Skipping Backlog spec for %s — AI Specced is true", entity_id)
        return

    # 2. Readiness check: content AND repo required
    description = full_entity.get("description", "")
    background_brief = full_entity.get("background_brief", "")
    has_content = (
        description.strip() not in ("", "No description")
        or background_brief.strip() != ""
    )
    repo_configs = full_entity.get("repo_configs", [])

    missing = []
    if not has_content:
        missing.append("a Description or Background & Brief")
    if not repo_configs:
        missing.append("at least one linked Repository")

    if missing:
        logger.info("Backlog spec readiness check failed for %s: missing %s", entity_id, missing)
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
        "Use `fibery_update_description` to write the spec (use `field=\"background_brief\"` for tech tasks), "
        "`fibery_create_entity` to create sub-tasks if appropriate, "
        "and `fibery_comment` to post a summary of what you added. "
        "After completing spec work, use `fibery_update_field` with "
        "field=\"Tools/AI Specced\" and value=\"true\" to mark the task as specced."
    )

    content_blocks: list[dict[str, Any]] = [create_text_block(prompt)]

    # 5. Use first repo only (spec work = single run)
    repo_config = repo_configs[0] if repo_configs else None

    if repo_config and not _is_repo_org_allowed(repo_config):
        logger.warning(
            "Rejecting Backlog spec: org '%s' not in ALLOWED_GITHUB_ORGS",
            repo_config.get("owner"),
        )
        return

    thread_id = generate_thread_id_from_fibery_entity(entity_id)

    configurable: dict[str, Any] = {
        "repo": repo_config or {},
        "fibery_entity": {
            "id": entity_id,
            "title": title,
            "url": entity_url,
            "github_tag": github_tag,
            "database_type": database_type,
            "desc_secret": full_entity.get("desc_secret", ""),
            "brief_secret": full_entity.get("brief_secret", ""),
        },
        "user_email": user_email,
        "source": "fibery",
    }

    # 6. Check for active thread — skip if busy
    thread_active = await is_thread_active(thread_id)
    if thread_active:
        logger.warning(
            "Skipping Backlog spec for %s — thread %s is already active",
            entity_id, thread_id,
        )
        return

    logger.info("Creating LangGraph run for Backlog spec, thread %s", thread_id)
    langgraph_client = get_client(url=LANGGRAPH_URL)
    await langgraph_client.runs.create(
        thread_id,
        "agent",
        input={"messages": [{"role": "user", "content": content_blocks}]},
        config={"configurable": configurable, "metadata": _AGENT_VERSION_METADATA},
        if_not_exists="create",
    )
    logger.info("Backlog spec run created for thread %s", thread_id)


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
    logger.info("Processing Fibery entity %s (type=%s)", entity_id, database_type)

    full_entity = await fetch_fibery_entity_details(database_type, entity_id)
    if not full_entity:
        logger.error("Failed to fetch Fibery entity details for %s", entity_id)
        return

    # Resolve user email for GitHub auth — try actor first, then entity lead
    user_email = None
    if actor_user_id:
        user_email = await fibery_fetch_user_email(actor_user_id)
    if not user_email and full_entity.get("lead_id"):
        user_email = await fibery_fetch_user_email(full_entity["lead_id"])
    if not user_email:
        logger.warning("Could not resolve email for Fibery user (actor=%s, lead=%s)",
                        actor_user_id, full_entity.get("lead_id"))

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
            logger.info("No repos linked, but spec request — proceeding without repo for entity %s", entity_id)
            repo_configs = [None]
        else:
            logger.error("No repositories linked to Fibery entity %s", entity_id)
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
        logger.info("Spec request on multi-repo entity — using first repo only for entity %s", entity_id)
        repo_configs = repo_configs[:1]

    for repo_config in repo_configs:
        if repo_config is not None and not _is_repo_org_allowed(repo_config):
            logger.warning(
                "Rejecting Fibery entity: org '%s' not in ALLOWED_GITHUB_ORGS",
                repo_config.get("owner"),
            )
            continue

        # Use entity+repo for thread ID in multi-repo scenarios
        if repo_config is not None and len(repo_configs) > 1:
            thread_id = generate_thread_id_from_fibery_entity(
                f"{entity_id}:{repo_config['owner']}/{repo_config['name']}"
            )
        else:
            thread_id = generate_thread_id_from_fibery_entity(entity_id)

        configurable: dict[str, Any] = {
            "repo": repo_config or {},
            "fibery_entity": {
                "id": entity_id,
                "title": title,
                "url": entity_url,
                "github_tag": github_tag,
                "database_type": database_type,
                "desc_secret": full_entity.get("desc_secret", ""),
                "brief_secret": full_entity.get("brief_secret", ""),
            },
            "user_email": user_email,
            "source": "fibery",
        }

        logger.info("Checking if thread %s is active before creating run", thread_id)
        thread_active = await is_thread_active(thread_id)

        if thread_active:
            logger.info("Thread %s is active, queuing message", thread_id)
            queued = await queue_message_for_thread(
                thread_id=thread_id,
                message_content={"text": prompt},
            )
            if queued:
                logger.info("Message queued for thread %s", thread_id)
            else:
                logger.error("Failed to queue message for thread %s", thread_id)
        else:
            logger.info("Creating LangGraph run for thread %s", thread_id)
            langgraph_client = get_client(url=LANGGRAPH_URL)
            await langgraph_client.runs.create(
                thread_id,
                "agent",
                input={"messages": [{"role": "user", "content": content_blocks}]},
                config={"configurable": configurable, "metadata": _AGENT_VERSION_METADATA},
                if_not_exists="create",
            )
            logger.info("LangGraph run created for thread %s (repo: %s/%s)",
                        thread_id, repo_config["owner"], repo_config["name"])


@app.get("/webhooks/fibery")
async def fibery_webhook_verify() -> dict[str, str]:
    """Verify endpoint for Fibery webhook setup."""
    return {"status": "ok", "message": "Fibery webhook endpoint is active"}


@app.post("/webhooks/fibery")
async def fibery_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Handle Fibery webhooks.

    Triggers a new LangGraph run when:
    - A comment mentioning @openswe is created on an entity
    - An entity's workflow state changes to a configured trigger state

    Authentication is via a secret URL token (Fibery does not support HMAC signing).
    """
    logger.info("Received Fibery webhook")

    # Verify webhook token
    token = request.query_params.get("token", "")
    if not FIBERY_WEBHOOK_URL_TOKEN or not hmac.compare_digest(token, FIBERY_WEBHOOK_URL_TOKEN):
        logger.warning("Invalid or missing Fibery webhook token")
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        payload = await request.json()
    except Exception:
        logger.exception("Failed to parse Fibery webhook JSON")
        return {"status": "error", "message": "Invalid JSON"}

    # Fibery webhooks v2 send an "effects" array, each with entity changes
    effects = payload.get("effects", [])
    if not effects:
        logger.info("No effects in Fibery webhook payload (keys: %s)", list(payload.keys()))
        return {"status": "ignored", "reason": "No effects"}

    author_id = payload.get("authorId", "")

    # Deduplicate: multiple effects can reference the same entity (e.g., add-comment + update-modification-date).
    # We only need to process each entity once.
    seen_entity_ids: set[str] = set()

    for effect in effects:
        entity_id = effect.get("id", "")
        database_type = effect.get("type", "")

        if not entity_id or not database_type:
            logger.debug("Skipping Fibery effect with missing id or type: %s", effect.get("effect"))
            continue

        if entity_id in seen_entity_ids:
            continue
        seen_entity_ids.add(entity_id)

        effect_type = effect.get("effect", "")

        # Detect the trigger type from the effect
        values = effect.get("values", {})
        values_before = effect.get("valuesBefore", {})

        # Detect trigger type from the effect
        comment_trigger = False
        state_changed = False
        new_state_value = None

        # Comment added: effect is "fibery.entity/add-collection-items" on "comments/comments"
        if effect_type == "fibery.entity/add-collection-items" and effect.get("field") == "comments/comments":
            comment_trigger = True
        # State change: look for workflow/state in values vs valuesBefore
        elif values and values_before:
            for key in values:
                if "state" in key.lower() or "workflow" in key.lower():
                    old_val = values_before.get(key)
                    new_val = values.get(key)
                    if old_val != new_val:
                        state_changed = True
                        new_state_value = new_val
                        break

        if comment_trigger:
            # Extract the new comment ID from the effect's items array
            items = effect.get("items", [])
            comment_id = items[0].get("fibery/id", "") if items else ""
            logger.info(
                "Comment trigger for Fibery entity %s, comment_id=%s, scheduling verification",
                entity_id,
                comment_id,
            )
            background_tasks.add_task(
                _process_fibery_comment_trigger,
                entity_id,
                database_type,
                author_id,
                comment_id,
            )
        elif state_changed:
            if _is_state_backlog(new_state_value):
                logger.info(
                    "Backlog state change for Fibery entity %s, scheduling spec work",
                    entity_id,
                )
                background_tasks.add_task(
                    process_fibery_backlog_spec,
                    entity_id,
                    database_type,
                    author_id,
                )
            else:
                logger.info(
                    "State change trigger for Fibery entity %s, scheduling processing",
                    entity_id,
                )
                background_tasks.add_task(
                    process_fibery_entity,
                    entity_id,
                    database_type,
                    "",  # no triggering comment for state changes
                    author_id,
                )

    return {"status": "accepted", "message": "Processing Fibery webhook effects"}


async def _process_fibery_comment_trigger(
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
        logger.info("No comment_id provided for Fibery entity %s, skipping", entity_id)
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
            logger.exception("Failed to fetch comment %s for entity %s", comment_id, entity_id)
            return

    if not comment_body:
        logger.info("Empty comment body for comment %s on entity %s", comment_id, entity_id)
        return

    logger.info("Fibery comment on entity %s mentions @openswe, processing", entity_id)

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
            logger.debug("Ignoring Fibery comment: matches bot message prefix")
            return

    if "@openswe" not in comment_body.lower():
        logger.debug("Ignoring Fibery comment: doesn't mention @openswe")
        return

    logger.info("Fibery comment mentions @openswe on entity %s, processing", entity_id)
    await process_fibery_entity(
        entity_id,
        database_type,
        triggering_comment=comment_body,
        actor_user_id=actor_user_id,
    )
