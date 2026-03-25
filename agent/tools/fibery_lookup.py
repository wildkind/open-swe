import asyncio
import re
from typing import Any

from ..utils.fibery import lookup_entity_by_public_id, search_entities

# Default database type — can be extended to support multiple databases
_DEFAULT_DATABASE_TYPE = "Tools/Task"


def fibery_lookup(query: str) -> dict[str, Any]:
    """Look up Fibery entities by tag (e.g., TASK-1104) or search by name.

    Use this tool to get context from Fibery when answering questions about tasks,
    checking status, or understanding what work is planned.

    **Examples:**
    - `fibery_lookup("TASK-1104")` — look up a specific task by its tag
    - `fibery_lookup("1104")` — same, just the number
    - `fibery_lookup("website redesign")` — search tasks by name

    Args:
        query: A task tag (e.g., "TASK-1104" or "1104") or search text.

    Returns:
        Dictionary with entity details (lookup) or a list of matching entities (search).
    """
    if not query.strip():
        return {"success": False, "error": "Query cannot be empty"}

    # Check if query looks like a public ID (e.g., "TASK-1104", "1104", "[TASK-1104]")
    match = re.search(r"(?:TASK-)?(\d+)", query.strip().strip("[]"), re.IGNORECASE)
    if match:
        public_id = match.group(1)
        result = asyncio.run(
            lookup_entity_by_public_id(_DEFAULT_DATABASE_TYPE, public_id)
        )
        if result:
            return {"success": True, "entity": result}
        return {"success": False, "error": f"No entity found with public ID {public_id}"}

    # Otherwise, search by name
    results = asyncio.run(search_entities(_DEFAULT_DATABASE_TYPE, query.strip()))
    if results:
        return {"success": True, "entities": results, "count": len(results)}
    return {"success": False, "error": f"No entities found matching '{query}'"}
