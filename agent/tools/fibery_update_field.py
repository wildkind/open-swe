import asyncio
from typing import Any

from langgraph.config import get_config

from ..utils.fibery import update_entity_field


def _parse_value(raw: str) -> Any:
    """Convert string value to appropriate Python type for Fibery API."""
    lower = raw.strip().lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def fibery_update_field(field: str, value: str) -> dict[str, Any]:
    """Update a field on the Fibery entity that triggered this task.

    Use this to set metadata fields after completing work. For example,
    set "AI Specced" to true after fleshing out requirements.

    **When to use:**
    - After completing spec/requirements work, call with
      `field="Tools/AI Specced"` and `value="true"` to mark the task as specced.

    Args:
        field: The Fibery field name (e.g., "Tools/AI Specced").
        value: The value to set as a string. Use "true"/"false" for booleans,
            numbers as strings (e.g., "42"), or plain text for string fields.

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

    parsed = _parse_value(value)
    success = asyncio.run(update_entity_field(database_type, entity_id, field, parsed))
    return {"success": success}
