import asyncio
from typing import Any

from langgraph.config import get_config

from ..utils.fibery import create_task_entity


def fibery_create_entity(title: str, description: str = "") -> dict[str, Any]:
    """Create a new Fibery Task entity linked as a sub-task of the current entity.

    Use this tool to break down a task into smaller, actionable sub-tasks.
    Each created entity is automatically linked to the triggering entity as
    a child via the Parent Task relation.

    **When to use:**
    - When breaking down an epic or large task into actionable sub-tasks.
    - Call once per sub-task. Aim for no more than ~10 sub-tasks per breakdown.
    - Each sub-task should have a clear, specific title.

    **What NOT to set:**
    - Size, Impact, Lead, and workflow state are left for humans.

    Args:
        title: The sub-task title (clear, actionable).
        description: Optional markdown description for the sub-task.

    Returns:
        Dictionary with 'success' (bool), and on success: 'id', 'public_id', 'url'.
    """
    config = get_config()
    configurable = config.get("configurable", {})
    fibery_entity = configurable.get("fibery_entity", {})

    parent_entity_id = fibery_entity.get("id")
    database_type = fibery_entity.get("database_type")
    if not parent_entity_id or not database_type:
        return {
            "success": False,
            "error": "Missing fibery_entity.id or fibery_entity.database_type in config",
        }

    if not title.strip():
        return {"success": False, "error": "Title cannot be empty"}

    result = asyncio.run(
        create_task_entity(title, description, parent_entity_id, database_type)
    )
    if result:
        return {"success": True, **result}
    return {"success": False, "error": "Failed to create entity in Fibery"}
