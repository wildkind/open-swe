"""Fibery API utilities."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid as _uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FIBERY_API_TOKEN = os.environ.get("FIBERY_API_TOKEN", "")
FIBERY_WORKSPACE_URL = os.environ.get("FIBERY_WORKSPACE_URL", "").rstrip("/")

# Simple rate limiter: track last request time to enforce 3 req/s
_last_request_time: float = 0.0
_MIN_REQUEST_INTERVAL: float = 0.34  # ~3 requests per second


async def _rate_limited_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Make an HTTP request with rate limiting (3 req/s for Fibery)."""
    global _last_request_time  # noqa: PLW0603
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()
    return await client.request(method, url, **kwargs)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Token {FIBERY_API_TOKEN}",
        "Content-Type": "application/json",
    }


async def fetch_entity(
    database_type: str,
    entity_id: str,
    fields: list[str] | None = None,
) -> dict[str, Any] | None:
    """Fetch a Fibery entity by ID using the Command API.

    Args:
        database_type: The Fibery database type (e.g., "App/Task").
        entity_id: The entity UUID.
        fields: Optional list of fields to select. Defaults to common fields.

    Returns:
        The entity dict, or None on failure.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        logger.warning("Fibery credentials not configured")
        return None

    if fields is None:
        fields = [
            "fibery/id",
            "fibery/public-id",
            "fibery/creation-date",
            f"{database_type}/name",
            f"{database_type}/description",
        ]

    command = {
        "command": "fibery.entity/query",
        "args": {
            "query": {
                "q/from": database_type,
                "q/select": {f: f for f in fields},
                "q/where": ["=", "fibery/id", "$id"],
                "q/limit": 1,
            },
            "params": {"$id": entity_id},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[command],
            )
            response.raise_for_status()
            results = response.json()
            if results and isinstance(results, list) and results[0].get("success"):
                rows = results[0].get("result", [])
                return rows[0] if rows else None
            return None
        except Exception:
            logger.exception("Failed to fetch Fibery entity %s", entity_id)
            return None


async def fetch_entity_repositories(
    database_type: str,
    entity_id: str,
    repo_relation: str = "Tools/Repositories",
) -> list[dict[str, str]]:
    """Fetch GitHub repositories linked to a Fibery entity.

    Repositories are stored as a collection relation to Tech/Repository entities.
    Each repository has a "Tech/Full Name" field in "owner/repo" format.

    Args:
        database_type: The Fibery database type (e.g., "Tools/Task").
        entity_id: The entity UUID.
        repo_relation: The relation field name. Defaults to "Tools/Repositories".

    Returns:
        List of repo config dicts with 'owner' and 'name' keys.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return []

    command = {
        "command": "fibery.entity/query",
        "args": {
            "query": {
                "q/from": database_type,
                "q/select": {
                    "repos": {
                        "q/from": repo_relation,
                        "q/select": {
                            "full_name": "Tech/Full Name",
                            "id": "fibery/id",
                        },
                        "q/limit": 20,
                    }
                },
                "q/where": ["=", "fibery/id", "$id"],
                "q/limit": 1,
            },
            "params": {"$id": entity_id},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[command],
            )
            response.raise_for_status()
            results = response.json()
            if not (results and isinstance(results, list) and results[0].get("success")):
                return []

            rows = results[0].get("result", [])
            if not rows:
                return []

            raw_repos = rows[0].get("repos", [])
            configs = []
            for repo in raw_repos:
                full_name = repo.get("full_name", "")
                if full_name and "/" in full_name:
                    owner, name = full_name.split("/", 1)
                    if owner.strip() and name.strip():
                        configs.append({"owner": owner.strip(), "name": name.strip()})
            return configs
        except Exception:
            logger.exception("Failed to fetch repositories for entity %s", entity_id)
            return []


async def fetch_document(document_secret: str) -> str:
    """Fetch rich text content from a Fibery document by its secret.

    Args:
        document_secret: The document secret returned by entity queries.

    Returns:
        The document content as markdown, or empty string on failure.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return ""

    url = f"{FIBERY_WORKSPACE_URL}/api/documents/{document_secret}"

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "GET",
                url,
                headers=_headers(),
                params={"format": "md"},
            )
            response.raise_for_status()
            data = response.json()
            return data.get("content", "")
        except Exception:
            logger.exception("Failed to fetch Fibery document %s", document_secret)
            return ""


async def fetch_entity_comments(
    database_type: str,
    entity_id: str,
) -> list[dict[str, Any]]:
    """Fetch comments on a Fibery entity.

    Comments are stored as related entities in the comments/comments collection.
    Each comment's text is a rich text document that must be fetched separately.

    Args:
        database_type: The Fibery database type (e.g., "App/Task").
        entity_id: The entity UUID.

    Returns:
        List of comment dicts with keys: id, author_name, author_id, body.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return []

    command = {
        "command": "fibery.entity/query",
        "args": {
            "query": {
                "q/from": database_type,
                "q/select": {
                    "comments": {
                        "q/from": "comments/comments",
                        "q/select": {
                            "id": "fibery/id",
                            "secret": "comment/document-secret",
                        },
                        "q/limit": 50,
                    }
                },
                "q/where": ["=", "fibery/id", "$id"],
                "q/limit": 1,
            },
            "params": {"$id": entity_id},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[command],
            )
            response.raise_for_status()
            results = response.json()
            if not (results and isinstance(results, list) and results[0].get("success")):
                return []

            rows = results[0].get("result", [])
            if not rows:
                return []

            raw_comments = rows[0].get("comments", [])
            comments = []
            for raw in raw_comments:
                secret = raw.get("secret")
                body = ""
                if secret:
                    body = await fetch_document(secret)
                comments.append(
                    {
                        "id": raw.get("id", ""),
                        "body": body,
                    }
                )
            return comments
        except Exception:
            logger.exception("Failed to fetch comments for entity %s", entity_id)
            return []


async def create_comment(
    database_type: str,
    entity_id: str,
    comment_body: str,
) -> bool:
    """Create a comment on a Fibery entity.

    This is a 3-step process:
    1. Create the comment entity
    2. Link it to the parent entity
    3. Set the document content

    Args:
        database_type: The Fibery database type (e.g., "App/Task").
        entity_id: The parent entity UUID.
        comment_body: Markdown-formatted comment text.

    Returns:
        True if all steps succeeded, False otherwise.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return False

    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: Create comment entity with a pre-generated document secret
        # Fibery requires comment/document-secret at creation time.
        comment_secret = str(_uuid.uuid4())
        create_cmd = {
            "command": "fibery.entity/create",
            "args": {
                "type": "comments/comment",
                "entity": {
                    "comment/document-secret": comment_secret,
                },
            },
        }

        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[create_cmd],
            )
            response.raise_for_status()
            results = response.json()
            if not (results and isinstance(results, list) and results[0].get("success")):
                logger.error("Failed to create comment entity for %s", entity_id)
                return False
            result = results[0].get("result", {})
            comment_id = result.get("fibery/id", "")
        except Exception:
            logger.exception("Failed to create comment entity (step 1)")
            return False

        if not comment_id:
            logger.error("No comment ID returned for entity %s", entity_id)
            return False

        # Step 2: Link comment to parent entity
        link_cmd = {
            "command": "fibery.entity/add-collection-items",
            "args": {
                "type": database_type,
                "field": "comments/comments",
                "entity": {"fibery/id": entity_id},
                "items": [{"fibery/id": comment_id}],
            },
        }

        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[link_cmd],
            )
            response.raise_for_status()
            results = response.json()
            if not (results and isinstance(results, list) and results[0].get("success")):
                logger.error("Failed to link comment to entity %s (step 2)", entity_id)
                return False
        except Exception:
            logger.exception("Failed to link comment to entity (step 2)")
            return False

        # Step 3: Set document content
        if not comment_secret:
            logger.error("No document secret returned for comment on entity %s", entity_id)
            return False

        try:
            response = await _rate_limited_request(
                client,
                "PUT",
                f"{FIBERY_WORKSPACE_URL}/api/documents/{comment_secret}",
                headers=_headers(),
                params={"format": "md"},
                json={"content": comment_body},
            )
            response.raise_for_status()
            return True
        except Exception:
            logger.exception("Failed to set comment document content (step 3)")
            return False


async def update_entity_state(
    database_type: str,
    entity_id: str,
    state_name: str,
    workflow_field: str = "workflow/state",
) -> bool:
    """Update the workflow state of a Fibery entity.

    Args:
        database_type: The Fibery database type (e.g., "App/Task").
        entity_id: The entity UUID.
        state_name: The target state name (e.g., "In Progress", "PR Ready").
        workflow_field: The workflow field path. Defaults to "workflow/state".

    Returns:
        True if successful, False otherwise.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return False

    # The workflow state enum type follows the pattern "workflow/state_{DatabaseType}"
    # e.g., for "Tools/Task" → "workflow/state_Tools/Task"
    state_type = f"workflow/state_{database_type}"
    lookup_cmd = {
        "command": "fibery.entity/query",
        "args": {
            "query": {
                "q/from": state_type,
                "q/select": {"id": "fibery/id", "name": "enum/name"},
                "q/where": ["=", "enum/name", "$name"],
                "q/limit": 1,
            },
            "params": {"$name": state_name},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[lookup_cmd],
            )
            response.raise_for_status()
            results = response.json()
            if not (results and isinstance(results, list) and results[0].get("success")):
                logger.error("Failed to look up state '%s' in %s", state_name, state_type)
                return False

            rows = results[0].get("result", [])
            if not rows:
                logger.error("State '%s' not found in %s", state_name, state_type)
                return False

            state_id = rows[0].get("id")
        except Exception:
            logger.exception("Failed to look up Fibery state '%s'", state_name)
            return False

        # Update the entity's workflow state
        update_cmd = {
            "command": "fibery.entity/update",
            "args": {
                "type": database_type,
                "entity": {
                    "fibery/id": entity_id,
                    workflow_field: {"fibery/id": state_id},
                },
            },
        }

        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[update_cmd],
            )
            response.raise_for_status()
            results = response.json()
            return bool(results and isinstance(results, list) and results[0].get("success"))
        except Exception:
            logger.exception("Failed to update state for entity %s", entity_id)
            return False


async def lookup_entity_by_public_id(
    database_type: str,
    public_id: str,
) -> dict[str, Any] | None:
    """Look up a Fibery entity by its public ID (e.g., "1104" from TASK-1104).

    Returns a summary dict with title, description, state, tag, and URL.

    Args:
        database_type: The Fibery database type (e.g., "Tools/Task").
        public_id: The numeric public ID (e.g., "1104").

    Returns:
        Dict with entity summary, or None if not found.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return None

    space_prefix = database_type.split("/")[0]
    name_field = f"{space_prefix}/Name"
    desc_field = f"{space_prefix}/Description"
    tag_field = f"{space_prefix}/Github Tag"

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
                    "state": ["workflow/state", "enum/name"],
                },
                "q/where": ["=", "fibery/public-id", "$pid"],
                "q/limit": 1,
            },
            "params": {"$pid": public_id},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[command],
            )
            response.raise_for_status()
            results = response.json()
            if not (results and isinstance(results, list) and results[0].get("success")):
                return None

            rows = results[0].get("result", [])
            if not rows:
                return None

            row = rows[0]
            description = ""
            desc_secret = row.get("desc_secret", "")
            if desc_secret and isinstance(desc_secret, str):
                description = await fetch_document(desc_secret)

            pid = row.get("public_id", public_id)
            entity_url = ""
            if FIBERY_WORKSPACE_URL and pid:
                entity_url = f"{FIBERY_WORKSPACE_URL}/{database_type.replace('/', '-')}/{pid}"

            return {
                "id": row.get("id", ""),
                "public_id": pid,
                "title": row.get("name", ""),
                "description": description or "No description",
                "state": row.get("state", ""),
                "github_tag": row.get("tag", ""),
                "url": entity_url,
            }
        except Exception:
            logger.exception("Failed to look up Fibery entity by public ID %s", public_id)
            return None


async def search_entities(
    database_type: str,
    query_text: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search Fibery entities by name.

    Args:
        database_type: The Fibery database type (e.g., "Tools/Task").
        query_text: Text to search for in entity names.
        limit: Maximum number of results.

    Returns:
        List of entity summary dicts.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return []

    space_prefix = database_type.split("/")[0]
    name_field = f"{space_prefix}/Name"
    tag_field = f"{space_prefix}/Github Tag"

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
                    "state": ["workflow/state", "enum/name"],
                },
                "q/where": ["q/contains", name_field, "$q"],
                "q/limit": limit,
            },
            "params": {"$q": query_text},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[command],
            )
            response.raise_for_status()
            results = response.json()
            if not (results and isinstance(results, list) and results[0].get("success")):
                return []

            rows = results[0].get("result", [])
            entities = []
            for row in rows:
                pid = row.get("public_id", "")
                entity_url = ""
                if FIBERY_WORKSPACE_URL and pid:
                    entity_url = f"{FIBERY_WORKSPACE_URL}/{database_type.replace('/', '-')}/{pid}"
                entities.append({
                    "id": row.get("id", ""),
                    "public_id": pid,
                    "title": row.get("name", ""),
                    "state": row.get("state", ""),
                    "github_tag": row.get("tag", ""),
                    "url": entity_url,
                })
            return entities
        except Exception:
            logger.exception("Failed to search Fibery entities for '%s'", query_text)
            return []


async def fetch_user_email(user_id: str) -> str | None:
    """Fetch a Fibery user's email address.

    Args:
        user_id: The Fibery user UUID.

    Returns:
        The user's email, or None if not found.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return None

    command = {
        "command": "fibery.entity/query",
        "args": {
            "query": {
                "q/from": "fibery/user",
                "q/select": {
                    "email": "user/email",
                    "name": "user/name",
                },
                "q/where": ["=", "fibery/id", "$id"],
                "q/limit": 1,
            },
            "params": {"$id": user_id},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[command],
            )
            response.raise_for_status()
            results = response.json()
            if results and isinstance(results, list) and results[0].get("success"):
                rows = results[0].get("result", [])
                if rows:
                    return rows[0].get("email")
            return None
        except Exception:
            logger.exception("Failed to fetch Fibery user email for %s", user_id)
            return None


async def fetch_entity_document_secret(
    database_type: str,
    entity_id: str,
    field: str,
) -> str | None:
    """Fetch the document secret for an entity's document field.

    Args:
        database_type: The Fibery database type (e.g., "Tools/Task").
        entity_id: The entity UUID.
        field: The document field name (e.g., "Tools/Description").

    Returns:
        The document secret string, or None if not found.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return None

    command = {
        "command": "fibery.entity/query",
        "args": {
            "query": {
                "q/from": database_type,
                "q/select": {
                    "secret": [field, "Collaboration~Documents/secret"],
                },
                "q/where": ["=", "fibery/id", "$id"],
                "q/limit": 1,
            },
            "params": {"$id": entity_id},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[command],
            )
            response.raise_for_status()
            results = response.json()
            if results and isinstance(results, list) and results[0].get("success"):
                rows = results[0].get("result", [])
                if rows:
                    secret = rows[0].get("secret")
                    if secret and isinstance(secret, str):
                        return secret
            return None
        except Exception:
            logger.exception("Failed to fetch document secret for entity %s field %s", entity_id, field)
            return None


async def update_document(
    document_secret: str,
    content: str,
    append: bool = True,
) -> bool:
    """Write or append markdown content to a Fibery document.

    When append=True, fetches current content first, concatenates with a
    separator, then writes the combined content. The PUT endpoint replaces
    the entire document.

    Args:
        document_secret: The document secret.
        content: Markdown content to write or append.
        append: If True, append to existing content. If False, replace.

    Returns:
        True if successful, False otherwise.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return False

    if not document_secret or not content.strip():
        return False

    if append:
        existing = await fetch_document(document_secret)
        if existing.strip():
            content = existing.rstrip() + "\n\n---\n\n" + content

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "PUT",
                f"{FIBERY_WORKSPACE_URL}/api/documents/{document_secret}",
                headers=_headers(),
                params={"format": "md"},
                json={"content": content},
            )
            response.raise_for_status()
            return True
        except Exception:
            logger.exception("Failed to update Fibery document %s", document_secret)
            return False


async def create_task_entity(
    title: str,
    description_md: str = "",
    parent_entity_id: str | None = None,
    database_type: str = "Tools/Task",
) -> dict[str, Any] | None:
    """Create a new Fibery Task entity with optional description and parent link.

    Args:
        title: The task title.
        description_md: Optional markdown description.
        parent_entity_id: Optional parent entity UUID to link as sub-task.
        database_type: The Fibery database type. Defaults to "Tools/Task".

    Returns:
        Dict with id, public_id, url keys, or None on failure.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return None

    space_prefix = database_type.split("/")[0]
    doc_secret = str(_uuid.uuid4())

    # Step 1: Create the entity with a pre-generated document secret
    create_cmd = {
        "command": "fibery.entity/create",
        "args": {
            "type": database_type,
            "entity": {
                f"{space_prefix}/Name": title,
                f"{space_prefix}/Description": {
                    "Collaboration~Documents/secret": doc_secret,
                },
            },
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[create_cmd],
            )
            response.raise_for_status()
            results = response.json()
            if not (results and isinstance(results, list) and results[0].get("success")):
                logger.error("Failed to create Fibery entity: %s", title)
                return None
            result = results[0].get("result", {})
            entity_id = result.get("fibery/id", "")
        except Exception:
            logger.exception("Failed to create Fibery entity: %s", title)
            return None

        if not entity_id:
            logger.error("No entity ID returned for: %s", title)
            return None

        # Step 2: Set parent task if provided
        if parent_entity_id:
            update_cmd = {
                "command": "fibery.entity/update",
                "args": {
                    "type": database_type,
                    "entity": {
                        "fibery/id": entity_id,
                        f"{space_prefix}/Parent Task": {"fibery/id": parent_entity_id},
                    },
                },
            }
            try:
                response = await _rate_limited_request(
                    client,
                    "POST",
                    f"{FIBERY_WORKSPACE_URL}/api/commands",
                    headers=_headers(),
                    json=[update_cmd],
                )
                response.raise_for_status()
            except Exception:
                logger.exception("Failed to set parent for entity %s", entity_id)

        # Step 3: Write description document content
        if description_md.strip():
            try:
                response = await _rate_limited_request(
                    client,
                    "PUT",
                    f"{FIBERY_WORKSPACE_URL}/api/documents/{doc_secret}",
                    headers=_headers(),
                    params={"format": "md"},
                    json={"content": description_md},
                )
                response.raise_for_status()
            except Exception:
                logger.exception("Failed to set description for entity %s", entity_id)

        # Step 4: Fetch public_id for URL construction
        try:
            query_cmd = {
                "command": "fibery.entity/query",
                "args": {
                    "query": {
                        "q/from": database_type,
                        "q/select": {"public_id": "fibery/public-id"},
                        "q/where": ["=", "fibery/id", "$id"],
                        "q/limit": 1,
                    },
                    "params": {"$id": entity_id},
                },
            }
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[query_cmd],
            )
            response.raise_for_status()
            results = response.json()
            public_id = ""
            if results and isinstance(results, list) and results[0].get("success"):
                rows = results[0].get("result", [])
                if rows:
                    public_id = str(rows[0].get("public_id", ""))
        except Exception:
            logger.exception("Failed to fetch public_id for entity %s", entity_id)
            public_id = ""

        entity_url = ""
        if FIBERY_WORKSPACE_URL and public_id:
            entity_url = f"{FIBERY_WORKSPACE_URL}/{database_type.replace('/', '-')}/{public_id}"

        return {
            "id": entity_id,
            "public_id": public_id,
            "url": entity_url,
        }


async def update_entity_field(
    database_type: str,
    entity_id: str,
    field: str,
    value: Any,
) -> bool:
    """Update a single field on a Fibery entity.

    Args:
        database_type: The Fibery database type (e.g., "Tools/Task").
        entity_id: The entity UUID.
        field: The field name (e.g., "Tools/AI Specced").
        value: The value to set.

    Returns:
        True if successful, False otherwise.
    """
    if not FIBERY_API_TOKEN or not FIBERY_WORKSPACE_URL:
        return False

    command = {
        "command": "fibery.entity/update",
        "args": {
            "type": database_type,
            "entity": {
                "fibery/id": entity_id,
                field: value,
            },
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await _rate_limited_request(
                client,
                "POST",
                f"{FIBERY_WORKSPACE_URL}/api/commands",
                headers=_headers(),
                json=[command],
            )
            response.raise_for_status()
            results = response.json()
            return bool(results and isinstance(results, list) and results[0].get("success"))
        except Exception:
            logger.exception("Failed to update field %s for entity %s", field, entity_id)
            return False
