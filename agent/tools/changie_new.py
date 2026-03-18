import logging
import shlex
from typing import Any

from langgraph.config import get_config

from ..utils.sandbox_paths import resolve_repo_dir
from ..utils.sandbox_state import get_sandbox_backend_sync

logger = logging.getLogger(__name__)


def changie_new(
    kind: str,
    body: str,
    component: str | None = None,
    custom: list[str] | None = None,
    projects: list[str] | None = None,
) -> dict[str, Any]:
    """Create a changelog entry using changie.

    This runs `npx changie new` in the repository to create a new change fragment file.
    The repository must have a .changie.yaml configuration file.

    Args:
        kind: The kind of change (e.g. "Added", "Changed", "Fixed", "Removed").
            Must match a kind defined in the repo's .changie.yaml.
        body: The changelog entry description.
        component: Optional component name if the repo uses components.
        custom: Optional list of custom values in "key=value" format
            (e.g. ["Author=alice", "Issue=123"]).
        projects: Optional list of project names if the repo uses projects.

    Returns:
        Dictionary with success status and output or error message.
    """
    # Strip quotes from body to avoid shell escaping issues
    body = body.replace("'", "").replace('"', "")

    try:
        config = get_config()
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id")

        if not thread_id:
            return {"success": False, "error": "Missing thread_id in config"}

        repo_config = configurable.get("repo", {})
        repo_name = repo_config.get("name")
        if not repo_name:
            return {"success": False, "error": "Missing repo name in config"}

        sandbox_backend = get_sandbox_backend_sync(thread_id)
        if not sandbox_backend:
            return {"success": False, "error": "No sandbox found for thread"}

        repo_dir = resolve_repo_dir(sandbox_backend, repo_name)

        cmd_parts = [
            "npx", "changie", "new",
            "--kind", shlex.quote(kind),
            "--body", shlex.quote(body),
        ]

        if component:
            cmd_parts.extend(["--component", shlex.quote(component)])

        if custom:
            for entry in custom:
                cmd_parts.extend(["--custom", shlex.quote(entry)])

        if projects:
            for project in projects:
                cmd_parts.extend(["--projects", shlex.quote(project)])

        command = f"cd {shlex.quote(repo_dir)} && {' '.join(cmd_parts)}"
        result = sandbox_backend.execute(command)

        if result.exit_code != 0:
            return {
                "success": False,
                "error": f"changie new failed (exit {result.exit_code}): {result.output.strip()}",
            }

        return {
            "success": True,
            "output": result.output.strip(),
        }

    except Exception as e:
        logger.exception("changie_new failed")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
