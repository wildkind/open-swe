# Brainstorm: Fibery Requirements Fleshing via Open SWE

**Date:** 2026-03-25
**Status:** Complete

## What We're Building

Extend Open SWE's Fibery integration so the agent can **flesh out task requirements** — not just implement tasks. When a user comments `@openswe` with a natural language request like "flesh out the requirements" or "break this into smaller tasks," the agent analyzes the task (and optionally the codebase) and writes structured specs, acceptance criteria, and sub-tasks back into Fibery.

### Capabilities

Three modes of requirements work, all triggered via natural language comments:

1. **Expand sparse tasks** — A task has a brief title/description. The agent researches the codebase and writes a detailed spec (acceptance criteria, affected areas, edge cases) into the entity's Description field.

2. **Break down epics** — A high-level task gets decomposed into smaller, actionable sub-tasks. The agent creates new Fibery entities linked to the parent.

3. **Review & improve specs** — A task already has a description. The agent reviews it for gaps and suggests improvements, appending to (not overwriting) the existing description.

The agent decides which mode(s) to apply based on the natural language request and the task's current state.

### Trigger

Same as existing: `@openswe` comment mentions. The agent interprets intent from the comment text. No new commands or webhook changes needed.

Examples:
- `@openswe please flesh out the requirements for this task`
- `@openswe break this into smaller tasks`
- `@openswe review the spec and identify any gaps`
- `@openswe this is too vague, can you add acceptance criteria?`

### Output

1. **Entity description updated** — The spec is written into the Fibery entity's Description document (appended when content already exists). This is the source of truth.

2. **Summary comment posted** — A comment summarizing what was added/changed, so the team gets notified via Fibery's activity feed.

3. **Sub-entities created and linked** — When breaking down tasks, new Task entities are created in Fibery and linked back to the parent via a relation, so they appear in the entity's sub-task view.

### Interaction Model

- **Produce + flag gaps** — The agent always produces its best output immediately. If something is ambiguous, it flags open questions and assumptions in the spec rather than blocking.
- A human can then comment to trigger a refinement pass, reusing the existing `@openswe` trigger.
- Spec and implementation are **always separate operations**. The agent never chains into code implementation after writing requirements.

### Codebase Access

Optional per-task. The agent decides based on the request:
- A high-level product idea may not need code exploration
- A technical task benefits from the agent cloning the repo and exploring relevant code to identify affected files, existing patterns, and complexity

## Why This Approach

**Prompt-driven mode switching with new tools** — rather than a separate endpoint or processing mode. Reasons:

- The existing `@openswe` comment trigger already passes the full comment text to the agent. The LLM can interpret intent naturally without keyword parsing or routing logic.
- New Fibery API tools (`fibery_update_description`, `fibery_create_entity`) are generally useful and reusable beyond just requirements work.
- Minimal infrastructure change — no new webhooks, endpoints, or processing paths. The intelligence lives in the prompt and tools.
- Follows the established pattern: the agent's behavior is shaped by its system prompt and available tools, not by external routing.

**Alternative considered:** Separate "requirements mode" with a dedicated system prompt and explicit routing. Rejected because it over-engineers what is fundamentally the same agent doing a different kind of work on the same entities.

## Key Decisions

1. **Natural language intent** over fixed commands — the agent interprets what the user wants from the comment text, no `@openswe spec` vs `@openswe breakdown` distinction.

2. **Description is the source of truth** — specs go into the entity's Description document, not just comments. Comments are for notification/traceability.

3. **Append, don't overwrite** — when the description already has content, the agent appends its additions rather than replacing what a human wrote.

4. **Sub-tasks are real entities** — breakdowns create new Fibery Task entities linked to the parent, not just checklists in the description.

5. **Produce immediately, flag gaps** — the agent doesn't block waiting for clarification. It does its best and explicitly marks assumptions and open questions for human review.

6. **Spec and implementation are always separate** — the agent never chains into code implementation after writing requirements.

7. **Codebase access is optional** — the agent decides per-task whether to clone the repo based on the nature of the request.

## New Tools Needed

### `fibery_update_description`
- Appends spec content to the `Tools/Description` document on the triggering entity
- Uses the Documents API (`PUT /api/documents/{secret}?format=md`) — same mechanism already used for comment creation step 3
- Fetches current description content first, then appends (never overwrites)
- The document secret is available from entity queries via `[Tools/Description, Collaboration~Documents/secret]`
- Note: Fibery MCP has `append_document_content` which does this — but the agent runs in a sandbox without MCP access, so we need a standalone tool using the REST API

### `fibery_create_entity`
- Creates a new `Tools/Task` entity with a title and description
- Sets `Tools/Parent Task` to link it to the parent entity
- Uses `fibery.entity/create` (already used for comment creation) + document secret setup for the description
- Returns the new entity's ID, public ID, and URL
- Does NOT set Size, Impact, or Lead — those are left for humans

### Utility Additions (`agent/utils/fibery.py`)
- `update_document(document_secret, content, append=True)` — write/append to a Fibery document
- `create_task_entity(title, description_md, parent_entity_id=None)` — create a Task and optionally link to parent
- `fetch_entity_document_secret(database_type, entity_id, field)` — get the document secret for Description or Background & Brief

### Prompt Changes
- New section in the system prompt teaching the agent about requirements work
- Guidelines for when to explore the codebase vs. work from context alone
- Spec structure template: Summary, Acceptance Criteria, Technical Notes, Edge Cases, Open Questions
- Instructions to always separate spec work from implementation
- Instruct agent to read `Tools/Background & Brief` as additional context
- Instruct agent to suggest workflow state and sizing in the summary comment (not set them directly)

## Fibery Schema Notes

From the `Tools/Task` schema:

- **Sub-task relation:** `Tools/Sub Tasks` (collection of `Tools/Task`) with inverse `Tools/Parent Task`
- **Description field:** `Tools/Description` (Collaboration~Documents/Document)
- **Background field:** `Tools/Background & Brief` (separate document field — may contain context the agent should read)
- **Workflow states:** Idea, Backlog, Next Up, In Progress, For Review, Blocked, Measuring, Done, Abandoned
- **Size field:** x-small, small, medium, large, x-large
- **Impact field:** Low, Medium, High
- **Fibery MCP has `append_document_content`** — can append markdown to entity documents directly, which simplifies the "append to description" tool

## Resolved Questions

1. **Sub-task relation field** — `Tools/Sub Tasks` (collection) / `Tools/Parent Task` (single reference). Confirmed via Fibery schema.

2. **Workflow state after speccing** — Agent suggests a state change in its summary comment but does NOT update the state. State management remains manual.

3. **Spec structure** — Summary, Acceptance Criteria, Technical Notes (affected areas, patterns), Edge Cases, Open Questions. Agent adapts structure based on task type.

4. **Background & Brief field** — Agent reads `Tools/Background & Brief` as additional input context alongside `Tools/Description`.

5. **Sub-task fields** — Agent only sets title, description, and parent link. Size/Impact/Lead are left for humans. Agent includes sizing suggestions in the summary comment instead.
