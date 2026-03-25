---
title: "feat: Fibery requirements fleshing via Open SWE"
type: feat
status: completed
date: 2026-03-25
origin: docs/brainstorms/2026-03-25-fibery-requirements-fleshing-brainstorm.md
---

# feat: Fibery Requirements Fleshing via Open SWE

## Overview

Extend Open SWE's Fibery integration so the agent can flesh out task requirements — not just implement tasks. When a user comments `@openswe` with a natural language request (e.g., "flesh out the requirements", "break this into smaller tasks"), the agent writes structured specs, acceptance criteria, and sub-tasks back into Fibery. The agent uses prompt-driven mode switching with new tools, requiring no webhook or endpoint changes.

## Problem Statement / Motivation

Currently, Open SWE can only **implement** tasks triggered from Fibery. But many tasks start as vague ideas or epics that need structured requirements before implementation can begin. Today, a human must manually write specs, acceptance criteria, and sub-tasks. This feature lets the agent do that work — grounding specs in codebase knowledge when appropriate — so tasks are implementation-ready faster.

## Proposed Solution

Add two new agent tools (`fibery_update_description`, `fibery_create_entity`) and a new system prompt section that teaches the agent to interpret requirements-work requests. The agent decides from the comment text whether to expand a sparse task, break down an epic, or review an existing spec. No new webhooks, endpoints, or routing logic — the intelligence lives in the prompt and tools. (see brainstorm: docs/brainstorms/2026-03-25-fibery-requirements-fleshing-brainstorm.md)

## Technical Approach

### Architecture

The feature adds three layers:

1. **Utility functions** in `agent/utils/fibery.py` — `update_document()`, `create_task_entity()`
2. **Agent tools** in `agent/tools/` — `fibery_update_description`, `fibery_create_entity`
3. **System prompt section** in `agent/prompt.py` — requirements-work guidelines
4. **Webhook handler changes** in `agent/webapp.py` — pass document secrets in config, fetch Background & Brief, handle no-repo entities for spec work

### Implementation Phases

#### Phase 1: Utility Functions (`agent/utils/fibery.py`)

**1a. `update_document(document_secret, content, append=True)`**

Writes or appends markdown content to a Fibery document.

```python
# agent/utils/fibery.py

async def update_document(
    document_secret: str,
    content: str,
    append: bool = True,
) -> bool:
    """Write or append content to a Fibery document.

    When append=True, fetches current content first, concatenates,
    then writes the combined content. PUT is a replace operation.
    """
    if append:
        existing = await fetch_document(document_secret)
        if existing.strip():
            content = existing.rstrip() + "\n\n---\n\n" + content
    # PUT /api/documents/{secret}?format=md
    ...
```

- Uses existing `fetch_document()` for the read step
- PUT endpoint is a **replace** operation (same as used in `create_comment` step 3, line 386-396)
- Append = fetch-concatenate-write (read-modify-write, no locking — acceptable given single-run constraint, see Phase 4)

**1b. `create_task_entity(title, description_md, parent_entity_id=None, database_type="Tools/Task")`**

Creates a new Task entity with a title, description document, and optional parent link.

```python
# agent/utils/fibery.py

async def create_task_entity(
    title: str,
    description_md: str = "",
    parent_entity_id: str | None = None,
    database_type: str = "Tools/Task",
) -> dict[str, Any] | None:
    """Create a Fibery Task entity with optional description and parent link.

    Returns dict with id, public_id, url, or None on failure.
    """
    # Step 1: Create entity with pre-generated document secret
    # (same pattern as create_comment, line 318-347)
    doc_secret = str(uuid.uuid4())
    space_prefix = database_type.split("/")[0]
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
    # ... execute, get entity_id

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
        # ... execute

    # Step 3: Write description document content
    if description_md:
        # PUT /api/documents/{doc_secret}?format=md
        ...

    # Step 4: Fetch public_id for URL construction
    ...
    return {"id": entity_id, "public_id": public_id, "url": entity_url}
```

- Follows the 3-step pattern from `create_comment()` (line 291-399)
- Sets `Tools/Parent Task` via entity update (same pattern as `update_entity_state`, line 464)
- Returns public_id and URL so the agent can reference the created entity

**1c. `fetch_entity_document_secret(database_type, entity_id, field)`**

Gets the document secret for a specified document field (Description or Background & Brief).

```python
# agent/utils/fibery.py

async def fetch_entity_document_secret(
    database_type: str,
    entity_id: str,
    field: str,
) -> str | None:
    """Fetch the document secret for an entity's document field.

    Args:
        field: e.g., "Tools/Description" or "Tools/Background & Brief"
    """
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
    # ... return secret string
```

Acceptance criteria:
- [x] `update_document` appends with `---` separator when content exists
- [x] `update_document` replaces when `append=False`
- [x] `create_task_entity` creates entity with title and description
- [x] `create_task_entity` links to parent when `parent_entity_id` provided
- [x] `create_task_entity` returns public_id and URL
- [x] `fetch_entity_document_secret` works for both Description and Background & Brief fields
- [x] All functions use `_rate_limited_request()` for Fibery rate limiting

---

#### Phase 2: Agent Tools (`agent/tools/`)

**2a. `fibery_update_description` tool** (`agent/tools/fibery_update_description.py`)

```python
# agent/tools/fibery_update_description.py

def fibery_update_description(content: str) -> dict[str, Any]:
    """Append content to the Fibery entity's Description field.

    Use this tool to write or update the spec/requirements on the triggering
    Fibery entity. Content is appended after existing description text,
    separated by a horizontal rule.

    **When to use:**
    - After analyzing a task, write the structured spec into the description
    - When reviewing/improving an existing spec, append your additions

    Args:
        content: Markdown-formatted content to append to the description.
    """
    config = get_config()
    fibery_entity = config["configurable"].get("fibery_entity", {})
    entity_id = fibery_entity.get("id")
    database_type = fibery_entity.get("database_type")
    desc_secret = fibery_entity.get("desc_secret")

    if not desc_secret:
        # Fallback: fetch it
        desc_secret = asyncio.run(
            fetch_entity_document_secret(database_type, entity_id, f"{space_prefix}/Description")
        )

    success = asyncio.run(update_document(desc_secret, content, append=True))
    return {"success": success}
```

**2b. `fibery_create_entity` tool** (`agent/tools/fibery_create_entity.py`)

```python
# agent/tools/fibery_create_entity.py

def fibery_create_entity(title: str, description: str = "") -> dict[str, Any]:
    """Create a new Fibery Task entity, optionally linked as a sub-task.

    Use this tool to break down a task into smaller sub-tasks. Each created
    entity is automatically linked to the triggering entity as a sub-task.

    **When to use:**
    - When breaking down an epic or large task into actionable sub-tasks
    - Call this once per sub-task (max ~10 sub-tasks per breakdown)

    Args:
        title: The sub-task title.
        description: Optional markdown description for the sub-task.
    """
    config = get_config()
    fibery_entity = config["configurable"].get("fibery_entity", {})
    parent_entity_id = fibery_entity.get("id")
    database_type = fibery_entity.get("database_type")

    result = asyncio.run(
        create_task_entity(title, description, parent_entity_id, database_type)
    )
    if result:
        return {"success": True, **result}
    return {"success": False, "error": "Failed to create entity"}
```

**2c. Register tools**

- `agent/tools/__init__.py`: Add imports and `__all__` entries for both new tools
- `agent/server.py` line 36-48: Add imports
- `agent/server.py` line 389-401: Add to `tools=[...]` list

Acceptance criteria:
- [x] `fibery_update_description` reads config, fetches secret if needed, appends content
- [x] `fibery_create_entity` creates sub-task linked to parent, returns entity URL
- [x] Both tools follow existing pattern: plain function, docstring as schema, `asyncio.run()`, return `{"success": ...}`
- [x] Both tools registered in `__init__.py` and `server.py`

---

#### Phase 3: System Prompt (`agent/prompt.py`)

Add a new `REQUIREMENTS_WORK_SECTION` constant and include it in `SYSTEM_PROMPT` concatenation.

```python
# agent/prompt.py

REQUIREMENTS_WORK_SECTION = """---

### Requirements & Specification Work

When a user asks you to flesh out requirements, write a spec, break down a task,
or review/improve an existing description, you are doing **requirements work** —
NOT code implementation.

**How to identify requirements work:**
The triggering comment asks you to expand, specify, break down, review, or improve
the task description — rather than implement code. Examples:
- "flesh out the requirements"
- "break this into smaller tasks"
- "add acceptance criteria"
- "review the spec for gaps"
- "this is too vague, can you detail it?"

**Requirements work rules:**
1. Do NOT call `commit_and_open_pr` — you are not writing code.
2. Do NOT call `fibery_state` — suggest state changes in your summary comment instead.
3. Do NOT chain into implementation after writing a spec. Spec and implementation are always separate.
4. Always read the entity's current description before appending to it.
5. If the entity has a "Background & Brief" section in the prompt, use it as context.

**Codebase exploration is optional:**
- For technical tasks: explore relevant code to ground the spec in reality (affected files, patterns, complexity).
- For product/business tasks: work from the description and background alone.
- Use your judgment based on the request.

**Spec structure (adapt based on task):**

## Summary
[1-3 sentences on what this task accomplishes]

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Technical Notes
[Affected areas, existing patterns, dependencies — only if you explored the codebase]

## Edge Cases
[What could go wrong, boundary conditions]

## Open Questions & Assumptions
[Flag anything uncertain. Mark assumptions explicitly.]

**Breaking down tasks:**
- Create sub-tasks using `fibery_create_entity` (max ~10 per breakdown).
- Each sub-task should have a clear title and brief description.
- Include sizing suggestions in your summary comment (not as Fibery fields).

**After requirements work:**
- Call `fibery_update_description` with the structured spec.
- Call `fibery_comment` with a summary of what you added/created.
- In the summary comment, suggest a workflow state if appropriate (e.g., "This task looks ready for Next Up").
- Include sizing estimates for sub-tasks in the summary comment.
"""
```

Then update the `SYSTEM_PROMPT` concatenation (line 279-297) to include `REQUIREMENTS_WORK_SECTION` before `COMMIT_PR_SECTION`.

Acceptance criteria:
- [x] New `REQUIREMENTS_WORK_SECTION` constant added
- [x] Section included in `SYSTEM_PROMPT` concatenation
- [x] Section clearly distinguishes requirements work from implementation
- [x] Spec template is flexible, not rigid
- [x] Explicit rules: no `commit_and_open_pr`, no `fibery_state`, no chaining

---

#### Phase 4: Webhook Handler Changes (`agent/webapp.py`)

**4a. Fetch Background & Brief**

Update `fetch_fibery_entity_details()` (line 1531) to also query the `Tools/Background & Brief` document field.

```python
# In the q/select dict (line 1570-1576), add:
"brief_secret": [f"{space_prefix}/Background & Brief", "Collaboration~Documents/secret"],
```

Then fetch the document content and include it in the returned dict. Update `process_fibery_entity()` to include it in the prompt:

```python
# In the comment-triggered prompt (line 1687-1696), add after description:
+ (f"\n\n## Background & Brief\n{background_brief}\n" if background_brief else "")
```

**4b. Pass document secrets in config**

Update the `fibery_entity` configurable dict (line 1746-1752) to include `desc_secret`:

```python
"fibery_entity": {
    "id": entity_id,
    "title": title,
    "url": entity_url,
    "github_tag": github_tag,
    "database_type": database_type,
    "desc_secret": full_entity.get("desc_secret", ""),  # NEW
},
```

This requires also passing `desc_secret` through from `fetch_fibery_entity_details`.

**4c. Handle no-repo entities for spec work**

Currently, `process_fibery_entity()` hard-fails when no repos are linked (line 1718-1726). For spec work, repos are optional.

Change the no-repo handling to allow spec-only runs:

```python
if not repo_configs:
    # No repos — run without a repo (spec-only mode).
    # Use a dummy repo config so the agent can still start,
    # but it won't have code access.
    repo_configs = [None]  # sentinel for "no repo"
```

Then in the for-loop, when `repo_config is None`:
- Skip the org-allowlist check
- Pass an empty `repo` config — requires `server.py` changes (Phase 4d)

**4d. Support repo-less agent runs in `server.py`**

The hardest change. Currently `get_agent()` (line 366-368) raises `RuntimeError` if no repo is cloned. For spec-only runs, the agent needs to start without a repo.

Options (in order of preference):
1. **Skip sandbox creation entirely** — the agent only needs Fibery API tools for spec work, not shell/file tools. But `create_deep_agent` requires a `backend`. This may not be feasible without deeper changes.
2. **Create sandbox without cloning** — start the sandbox but don't clone a repo. The agent can still run shell commands but `{working_dir}` is empty. Requires removing the `repo_dir` check.
3. **Use a known "spec-only" repo** — clone a lightweight repo (or the same repo) just to satisfy the sandbox requirement, but the prompt tells the agent it's for spec work only.

**Recommendation:** Option 2 is cleanest. Make the `repo_dir` check conditional:

```python
# server.py, around line 366
if not repo_dir and config["configurable"].get("repo", {}).get("owner"):
    # Repo was requested but couldn't be cloned
    raise RuntimeError(...)
# If no repo was requested (spec-only), proceed without repo_dir
```

The `working_dir` in the prompt would be the sandbox's base directory instead of a repo path.

**4e. Single-run for multi-repo spec operations**

When the triggering comment is spec work and the entity has multiple repos, spawning N runs creates race conditions on the same description document.

Add a heuristic in `process_fibery_entity()` to detect spec-intent and run only once:

```python
# Simple heuristic — check if comment contains spec-related terms
SPEC_KEYWORDS = {"flesh out", "break down", "requirements", "spec", "acceptance criteria",
                 "review the spec", "too vague", "detail", "sub-tasks", "subtasks"}

def _is_spec_request(comment: str) -> bool:
    comment_lower = comment.lower()
    return any(kw in comment_lower for kw in SPEC_KEYWORDS)

# In process_fibery_entity, before the repo loop:
if triggering_comment and _is_spec_request(triggering_comment):
    # Spec work: run once, use first repo (or None if no repos)
    repo_configs = repo_configs[:1] if repo_configs else [None]
```

This is imperfect (natural language is ambiguous) but prevents the worst case of N runs writing to the same description. The prompt-level instructions are the primary control.

Acceptance criteria:
- [x] `fetch_fibery_entity_details` fetches Background & Brief document content
- [x] Background & Brief included in agent prompt
- [x] `desc_secret` passed in `fibery_entity` configurable dict
- [x] No-repo entities can trigger spec-only runs (no `RuntimeError`)
- [x] Multi-repo entities only spawn one run for spec requests
- [x] `_is_spec_request` heuristic covers common spec-related phrases

---

## System-Wide Impact

- **Interaction graph**: `@openswe` comment triggers webhook -> `process_fibery_entity()` -> LangGraph run -> agent calls `fibery_update_description` / `fibery_create_entity` / `fibery_comment`. Entity creation via API may fire Fibery webhooks (see risk below).
- **Error propagation**: Tool failures return `{"success": False, "error": "..."}` — the `ToolErrorMiddleware` catches unhandled exceptions. Partial failures (e.g., 3 of 5 sub-tasks created) are logged but not rolled back.
- **State lifecycle risks**: The read-modify-write in `update_document` has no locking. Mitigated by single-run constraint for spec operations.
- **API surface parity**: New tools are only available to the Fibery-triggered agent, same as `fibery_comment` and `fibery_state`.

## Acceptance Criteria

### Functional Requirements

- [x] Agent interprets "flesh out requirements" comments and writes specs to entity Description
- [x] Agent interprets "break this down" comments and creates linked sub-task entities
- [x] Agent interprets "review the spec" comments and appends improvements to existing descriptions
- [x] Agent reads Background & Brief as additional context
- [x] Agent suggests (but does not set) workflow state and sizing in summary comments
- [x] Agent never chains spec work into implementation
- [x] No-repo entities can trigger spec-only runs
- [x] Multi-repo entities only spawn one run for spec requests

### Non-Functional Requirements

- [x] Rate limiting respected (3 req/s to Fibery API)
- [x] Sub-task creation capped at ~10 per run (via prompt instruction)

## Dependencies & Risks

**Risk: Sub-task creation triggers webhook recursion**
Creating entities via API generates Fibery events. If the webhook subscription includes entity-creation events, each new sub-task could trigger a new agent run. **Mitigation:** The current webhook handler only processes comment-add and state-change events (line 1851-1861 of `webapp.py`). Entity creation events are not subscribed, so this should not be an issue. Verify by checking the Fibery webhook subscription configuration.

**Risk: Read-modify-write race on description updates**
Two concurrent writes to the same description document would cause last-writer-wins. **Mitigation:** The `_is_spec_request` heuristic ensures only one run per spec request, even for multi-repo entities.

**Risk: Document secret field path for entity creation**
The exact Fibery API field path for setting a document secret at entity creation time (`Tools/Description` -> `Collaboration~Documents/secret`) needs to be verified. The `create_comment` pattern uses `comment/document-secret`, which is a different field type. **Mitigation:** Test the API call manually first. If nested document-secret setting doesn't work at creation time, use a two-step approach (create entity, then PUT document).

**Risk: Repo-less sandbox support**
Skipping repo cloning may break assumptions in `create_deep_agent` or middleware. **Mitigation:** Test that the agent can start and call tools without a cloned repo. The `open_pr_if_needed` middleware should be a no-op when there's no repo context.

## Sources & References

### Origin

- **Brainstorm document:** [docs/brainstorms/2026-03-25-fibery-requirements-fleshing-brainstorm.md](docs/brainstorms/2026-03-25-fibery-requirements-fleshing-brainstorm.md) — Key decisions carried forward: natural language intent over fixed commands, description as source of truth with append semantics, produce immediately + flag gaps, spec and implementation always separate.

### Internal References

- Existing Fibery tools: `agent/tools/fibery_comment.py`, `agent/tools/fibery_state.py`, `agent/tools/fibery_lookup.py`
- Fibery utilities: `agent/utils/fibery.py` (all Fibery API patterns)
- System prompt: `agent/prompt.py` (section constants, `construct_system_prompt()`)
- Tool registration: `agent/server.py:36-48` (imports), `agent/server.py:389-401` (tool list)
- Webhook handler: `agent/webapp.py:1531-1782` (`fetch_fibery_entity_details`, `process_fibery_entity`)
- Entity config passed to agent: `agent/webapp.py:1744-1755`

### Fibery Schema

- Sub-task relation: `Tools/Sub Tasks` (collection) / `Tools/Parent Task` (single reference)
- Description field: `Tools/Description` (Collaboration~Documents/Document)
- Background field: `Tools/Background & Brief` (Collaboration~Documents/Document)
- Document API: `GET/PUT {workspace}/api/documents/{secret}?format=md`
- Entity API: `POST {workspace}/api/commands` with `fibery.entity/create`, `fibery.entity/update`
