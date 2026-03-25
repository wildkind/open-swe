import asyncio
from typing import Any

from langgraph.config import get_config

from ..utils.fibery import update_entity_field


def fibery_update_field(field: str, value: Any) -> dict[str, Any]:
    """Update a field on the Fibery entity that triggered this task.

    Use this to set metadata fields after completing work. For example,
    set "AI Specced" to true after fleshing out requirements.

    **When to use:**
    - After completing spec/requirements work, call with
      `field="Tools/AI Specced"` and `value=true` to mark the task as specced.

    Args:
        field: The Fibery field name (e.g., "Tools/AI Specced").
        value: The value to set (e.g., true, false, "some string").

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

    if not field.strip():
        return {"success": False, "error": "Field name cannot be empty"}

    success = asyncio.run(update_entity_field(database_type, entity_id, field, value))
    return {"success": success}
