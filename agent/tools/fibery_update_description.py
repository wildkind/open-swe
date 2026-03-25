import asyncio
from typing import Any, Literal

from langgraph.config import get_config

from ..utils.fibery import fetch_entity_document_secret, update_document

# Map user-facing field names to Fibery field paths and config keys
_FIELD_MAP = {
    "description": {"config_key": "desc_secret", "field_suffix": "Description"},
    "background_brief": {"config_key": "brief_secret", "field_suffix": "Background & Brief"},
}


def fibery_update_description(
    content: str,
    field: Literal["description", "background_brief"] = "description",
) -> dict[str, Any]:
    """Append content to a document field on the Fibery entity.

    Use this tool to write or update the spec/requirements on the triggering
    Fibery entity. Content is appended after existing text, separated by a
    horizontal rule. Existing content is never overwritten.

    **When to use:**
    - After analyzing a task, write the structured spec into the appropriate field.
    - When reviewing/improving an existing spec, append your additions.
    - Do NOT use this for implementation work — use it only for requirements/spec writing.

    **Which field to use:**
    - Use `field="background_brief"` for tech/engineering tasks — these typically use
      Background & Brief as the primary content field.
    - Use `field="description"` for product/business tasks or when the Description field
      is the primary content field.
    - Check which field has existing content in the prompt to determine which is primary.

    Args:
        content: Markdown-formatted content to append.
        field: Which document field to update. Either "description" (default) or
            "background_brief". Use "background_brief" for tech tasks.

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

    if not content.strip():
        return {"success": False, "error": "Content cannot be empty"}

    if field not in _FIELD_MAP:
        return {"success": False, "error": f"Unknown field: {field}. Use 'description' or 'background_brief'."}

    field_info = _FIELD_MAP[field]

    # Try config-provided secret first, then fetch it
    doc_secret = fibery_entity.get(field_info["config_key"])
    if not doc_secret:
        space_prefix = database_type.split("/")[0]
        doc_secret = asyncio.run(
            fetch_entity_document_secret(
                database_type, entity_id, f"{space_prefix}/{field_info['field_suffix']}"
            )
        )

    if not doc_secret:
        return {"success": False, "error": f"Could not resolve document secret for {field}"}

    success = asyncio.run(update_document(doc_secret, content, append=True))
    return {"success": success}
