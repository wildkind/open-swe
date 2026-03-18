import asyncio
from typing import Any

from langgraph.config import get_config

from ..utils.github_app import get_github_app_installation_token
from ..utils.github_comments import fetch_pr_comments_since_last_tag


def read_pr_comments(pr_number: int) -> dict[str, Any]:
    """Read comments and reviews from a GitHub pull request.

    Args:
        pr_number: The pull request number to read comments from.

    Returns:
        Dictionary with success status and a list of comments, each containing
        author, body, type (pr_comment/review_comment/review), created_at,
        and for inline review comments: path and line number.
    """
    config = get_config()
    configurable = config.get("configurable", {})

    repo_config = configurable.get("repo", {})
    if not pr_number:
        return {"success": False, "error": "Missing pr_number argument"}
    if not repo_config:
        return {"success": False, "error": "No repo config found in config"}

    token = asyncio.run(get_github_app_installation_token())
    if not token:
        return {"success": False, "error": "Failed to get GitHub App installation token"}

    comments = asyncio.run(
        fetch_pr_comments_since_last_tag(repo_config, pr_number, token=token)
    )
    return {"success": True, "comments": comments}
