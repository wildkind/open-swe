import asyncio
from typing import Any

from langgraph.config import get_config

from ..utils.fibery import create_comment


def fibery_comment(comment_body: str) -> dict[str, Any]:
    """Post a comment to a Fibery entity.

    Use this tool to communicate progress and completion to stakeholders on Fibery.

    **When to use:**
    - After calling `commit_and_open_pr`, post a comment on the Fibery entity to let
      stakeholders know the task is complete and include the PR link. For example:
      "I've completed the implementation and opened a PR: <pr_url>"
    - When answering a question or sharing an update (no code changes needed).

    Args:
        comment_body: Markdown-formatted comment text to post to the Fibery entity.

    Returns:
        Dictionary with 'success' (bool) key.
    """
    config = get_config()
    configurable = config.get("configurable", {})
    fibery_entity = configurable.get("fibery_entity", {})

    entity_id = fibery_entity.get("id")
    database_type = fibery_entity.get("database_type")
    if not entity_id or not database_type:
        return {
            "success": False,
            "error": "Missing fibery_entity.id or fibery_entity.database_type in config",
        }

    if not comment_body.strip():
        return {"success": False, "error": "Comment body cannot be empty"}

    success = asyncio.run(create_comment(database_type, entity_id, comment_body))
    return {"success": success}
