import asyncio
from typing import Any

from langgraph.config import get_config

from ..utils.fibery import update_entity_state


def fibery_state(state_name: str) -> dict[str, Any]:
    """Update the workflow state of a Fibery entity.

    Use this tool to update the entity's status as you make progress.

    **When to use:**
    - After starting work, update state to "In Progress".
    - After calling `commit_and_open_pr`, update state to "PR Ready" or equivalent.

    Args:
        state_name: The target workflow state name (e.g., "In Progress", "PR Ready").

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

    if not state_name.strip():
        return {"success": False, "error": "State name cannot be empty"}

    success = asyncio.run(update_entity_state(database_type, entity_id, state_name))
    return {"success": success}
