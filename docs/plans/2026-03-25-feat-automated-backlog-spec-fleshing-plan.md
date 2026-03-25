---
title: "feat: Automated spec fleshing on Backlog state transition"
type: feat
status: completed
date: 2026-03-25
origin: docs/brainstorms/2026-03-25-automated-spec-fleshing-brainstorm.md
---

# feat: Automated Spec Fleshing on Backlog State Transition

## Overview

When a Fibery task moves to **Backlog**, automatically run the spec-fleshing agent — no manual `@openswe` comment needed. The system checks readiness (content + repo), skips if already specced (`AI Specced` boolean), and routes to the spec-work prompt. This builds on the manual spec tools already implemented (`fibery_update_description`, `fibery_create_entity`).

## Problem Statement / Motivation

The manual `@openswe` trigger for spec work requires someone to remember to comment. Tying it to the Backlog workflow state makes it automatic — every task that enters Backlog gets specced if it has enough context. This fits the team's natural flow: Idea → Backlog (auto-spec) → Next Up → In Progress.

## Proposed Solution

Extend the existing state-change webhook handler to detect Backlog transitions, run a readiness check, and route to a spec-specific prompt. Add an `AI Specced` boolean field to prevent double-runs. (see brainstorm: docs/brainstorms/2026-03-25-automated-spec-fleshing-brainstorm.md)

## Technical Approach

### Phase 1: Fibery Schema — Add `AI Specced` Field

Create a boolean field `Tools/AI Specced` on the `Tools/Task` database (default: false). This can be done via the Fibery MCP or manually in the Fibery UI.

### Phase 2: Read AI Specced + New State in Webhook Handler

**2a. Extract the new state name from the webhook payload**

The webhook `values` dict contains the new state as a key like `workflow/state` with a value that is either a state name string or a Fibery entity reference (dict with `fibery/id`). We need to resolve this to a state name.

In `agent/webapp.py`, update the state-change detection block (lines 1896-1903) to also capture the new state value:

```python
# In the state-change detection loop:
new_state_value = None
for key in values:
    if "state" in key.lower() or "workflow" in key.lower():
        old_val = values_before.get(key)
        new_val = values.get(key)
        if old_val != new_val:
            state_changed = True
            new_state_value = new_val  # capture the new state
            break
```

Then route based on the new state:

```python
elif state_changed:
    # Check if the new state is "Backlog" (value may be a string or dict)
    is_backlog = _is_state_backlog(new_state_value)
    if is_backlog:
        background_tasks.add_task(
            process_fibery_backlog_spec,
            entity_id, database_type, author_id,
        )
    else:
        background_tasks.add_task(
            process_fibery_entity,
            entity_id, database_type, "", author_id,
        )
```

The `_is_state_backlog()` helper needs to handle multiple formats:

```python
_BACKLOG_STATE_ID = "9ac0d04f-a6f9-4271-b34f-a4919460d770"  # from Fibery schema

def _is_state_backlog(state_value: Any) -> bool:
    if isinstance(state_value, str):
        return state_value.lower() == "backlog"
    if isinstance(state_value, dict):
        # Could be {"fibery/id": "..."} or {"enum/name": "Backlog"}
        if state_value.get("fibery/id") == _BACKLOG_STATE_ID:
            return True
        name = state_value.get("enum/name", "")
        return name.lower() == "backlog"
    return False
```

**2b. Add AI Specced to `fetch_fibery_entity_details`**

In the entity query's `q/select` dict, add:

```python
"ai_specced": f"{space_prefix}/AI Specced",
```

And pass it through in the returned dict.

### Phase 3: New `process_fibery_backlog_spec` Function

A new async function in `webapp.py` that handles the Backlog spec automation:

```python
async def process_fibery_backlog_spec(
    entity_id: str,
    database_type: str,
    actor_user_id: str = "",
) -> None:
    """Auto-spec a Fibery entity that moved to Backlog."""

    full_entity = await fetch_fibery_entity_details(database_type, entity_id)
    if not full_entity:
        return

    # 1. Skip if already specced
    if full_entity.get("ai_specced"):
        logger.info("Skipping Backlog spec for %s — AI Specced is true", entity_id)
        return

    # 2. Readiness check
    has_content = (
        full_entity.get("description", "").strip() not in ("", "No description")
        or full_entity.get("background_brief", "").strip() != ""
    )
    repo_configs = full_entity.get("repo_configs", [])

    missing = []
    if not has_content:
        missing.append("a Description or Background & Brief")
    if not repo_configs:
        missing.append("at least one linked Repository")

    if missing:
        await fibery_create_comment(
            database_type, entity_id,
            "⏸️ **Auto-spec paused**\n\n"
            f"I can't flesh out this task yet. Please add:\n"
            + "\n".join(f"- {m}" for m in missing)
            + "\n\nOnce added, move the task out of Backlog and back in, "
            "or comment `@openswe flesh out the requirements`.",
        )
        return

    # 3. Resolve user email, build prompt, create run
    # (same pattern as process_fibery_entity, but with spec-specific prompt)
    ...
```

**Spec-specific prompt for Backlog triggers:**

```python
prompt = (
    "A task has been moved to Backlog and needs its requirements fleshed out.\n\n"
    f"## Entity\n{title}"
    + (f" ({github_tag})" if github_tag else "")
    + (f"\n{entity_url}" if entity_url else "")
    + f"\n\n## Entity Description\n{description}\n\n"
    + (f"## Background & Brief\n{background_brief}\n\n" if background_brief else "")
    + "Please flesh out the requirements for this task. "
    "Use `fibery_update_description` to write the spec, "
    "`fibery_create_entity` to create sub-tasks if appropriate, "
    "and `fibery_comment` to post a summary of what you added. "
    "After completing spec work, use `fibery_update_field` to set AI Specced to true."
)
```

**Single-run for multi-repo:** Apply the same `repo_configs[:1]` logic as the manual spec trigger.

### Phase 4: New `fibery_update_field` Tool

A generic tool to update a field value on the triggering Fibery entity. Needed for setting `AI Specced = true`.

**Utility function** in `agent/utils/fibery.py`:

```python
async def update_entity_field(
    database_type: str,
    entity_id: str,
    field: str,
    value: Any,
) -> bool:
    """Update a single field on a Fibery entity."""
    command = {
        "command": "fibery.entity/update",
        "args": {
            "type": database_type,
            "entity": {
                "fibery/id": entity_id,
                field: value,
            },
        },
    }
    # ... execute via _rate_limited_request
```

**Tool** in `agent/tools/fibery_update_field.py`:

```python
def fibery_update_field(field: str, value: Any) -> dict[str, Any]:
    """Update a field on the Fibery entity.

    Use this to set metadata fields after completing work. For example,
    set "AI Specced" to true after fleshing out requirements.

    Args:
        field: The Fibery field name (e.g., "Tools/AI Specced").
        value: The value to set (e.g., true, false, "some string").
    """
    ...
```

Register in `__init__.py` and `server.py`.

### Phase 5: Prompt Update — Instruct Agent to Set AI Specced

Add to `REQUIREMENTS_WORK_SECTION` in `agent/prompt.py`:

```
**After completing spec work triggered by a Backlog state change:**
- Call `fibery_update_field` with field="Tools/AI Specced" and value=true.
- This prevents the agent from re-speccing the task if it moves through Backlog again.
```

## Acceptance Criteria

- [ ] `AI Specced` boolean field exists on `Tools/Task` in Fibery (manual — user will create)
- [x] Moving a task to Backlog with content + repo triggers automatic spec work
- [x] Moving a task to Backlog without content or repo posts a comment listing what's missing
- [x] Moving a task to Backlog with `AI Specced = true` skips silently
- [x] Non-Backlog state changes continue to trigger implementation work as before
- [x] Agent sets `AI Specced = true` after completing spec work (via prompt instruction + tool)
- [x] Multi-repo entities only spawn one spec run
- [x] Active thread for the entity → spec trigger skipped (log warning)
- [x] `fibery_update_field` tool registered and functional
- [x] Readiness check treats `"No description"` as empty

## Dependencies & Risks

**Risk: Webhook payload state format is unknown.** The `values` dict may contain state as a string, a UUID, or a nested object. **Mitigation:** Add logging to capture the raw payload for the first few Backlog transitions. The `_is_state_backlog` helper handles multiple formats. Can also hardcode the Backlog state UUID from the schema (`9ac0d04f-a6f9-4271-b34f-a4919460d770`).

**Risk: Rapid state changes (Backlog → Next Up).** The spec agent runs but the task has already moved forward. **Mitigation:** Acceptable — the agent still produces useful spec work. The AI Specced flag prevents re-runs.

**Risk: Re-spec appends duplicate content.** When AI Specced is unchecked and task re-enters Backlog. **Mitigation:** The `REQUIREMENTS_WORK_SECTION` already instructs the agent to read existing content before appending. The agent should only add what's missing.

**Risk: Concurrent state-change and comment triggers.** Both could fire spec work simultaneously. **Mitigation:** The active-thread check prevents the second trigger from spawning a new run — it queues the message instead.

## Sources & References

### Origin

- **Brainstorm document:** [docs/brainstorms/2026-03-25-automated-spec-fleshing-brainstorm.md](docs/brainstorms/2026-03-25-automated-spec-fleshing-brainstorm.md) — Key decisions: Backlog is the trigger state, readiness requires content AND repo, AI Specced boolean prevents double-runs, Backlog trigger is spec-only.

### Internal References

- Webhook state-change detection: `agent/webapp.py:1896-1903`
- `process_fibery_entity`: `agent/webapp.py:1671-1824`
- `fetch_fibery_entity_details`: `agent/webapp.py:1531-1654`
- Existing spec tools: `agent/tools/fibery_update_description.py`, `agent/tools/fibery_create_entity.py`
- System prompt requirements section: `agent/prompt.py` `REQUIREMENTS_WORK_SECTION`
- Backlog state UUID from Fibery schema: `9ac0d04f-a6f9-4271-b34f-a4919460d770`
