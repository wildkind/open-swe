---
title: "feat: Add Fibery Issue Tracker Integration"
type: feat
status: completed
date: 2026-03-19
origin: docs/brainstorms/2026-03-18-fibery-integration-brainstorm.md
---

# feat: Add Fibery Issue Tracker Integration

## Overview

Add Fibery as an issue tracker integration for Open SWE, enabling agents to be triggered from Fibery entities (via comment mentions or workflow state changes) and communicate results back through comments and state updates. This follows the direct integration pattern established by the existing Linear, GitHub, and Slack integrations.

## Problem Statement / Motivation

The team uses Fibery as their primary issue tracker. Currently, Open SWE can only be triggered from Linear, GitHub, or Slack. Adding Fibery as a trigger source lets developers stay in their existing workflow without switching tools. This mirrors the team's real workspace setup and fills a gap in the project's integration coverage.

## Proposed Solution

Follow the established bespoke integration pattern (webhook + processor + tool) documented in `CUSTOMIZATION.md`. Build a direct Fibery integration that:

1. Receives webhooks from Fibery on comment mentions (`@openswe`) and workflow state changes
2. Fetches full entity context via the Fibery Command API
3. Routes to the correct GitHub repo(s) using a per-entity field
4. Creates agent runs with Fibery-specific metadata
5. Provides agent tools for posting comments and updating workflow state back to Fibery

(see brainstorm: `docs/brainstorms/2026-03-18-fibery-integration-brainstorm.md`)

## Technical Considerations

### Architecture

Follows the same three-layer pattern as Linear (`webapp.py:833`, `webapp.py:481`, `agent/tools/linear_comment.py`, `agent/utils/linear.py`):

- **Layer 1 — Webhook endpoint:** `POST /webhooks/fibery` in `agent/webapp.py`
- **Layer 2 — Processing function:** `process_fibery_entity()` in `agent/webapp.py`
- **Layer 3 — Agent tools:** `fibery_comment` and `fibery_state` in `agent/tools/`
- **Layer 4 — API utilities:** `agent/utils/fibery.py`

### Fibery API Specifics

- **Authentication:** Token-based (`FIBERY_API_TOKEN`), no OAuth
- **Webhook verification:** No HMAC signing — use secret URL token (`/webhooks/fibery?token=SECRET` via `FIBERY_WEBHOOK_URL_TOKEN`)
- **Comment creation:** 3-step process (create entity, link to parent, set document content via `PUT /api/documents/{secret}?format=md`) — can be batched in a single command API call
- **Rich text fields:** Return a document secret; content must be fetched separately via `GET /api/documents/{secret}?format=md`
- **Rate limits:** 3 requests/second per token — the comment 3-step process consumes the full budget for 1 second

### Security Considerations

- **No HMAC replay protection:** Secret URL token is weaker than HMAC. Mitigate by keeping the token out of logs, using HTTPS only, and considering IP allowlisting if Fibery publishes webhook source IPs.
- **Bot comment loop prevention:** Must detect and skip comments posted by the API token to prevent infinite webhook loops. Check the comment author against the API token's associated user, or maintain a short-lived set of recently-posted comment IDs.
- **Org allowlist:** Apply `_is_repo_org_allowed()` check to Fibery-routed repos (same as Linear at `webapp.py:922`).

### Performance Implications

- Rate limit of 3 req/s means comment posting is inherently slow. Implement a simple rate limiter (e.g., `asyncio.Semaphore` or minimum delay between calls) in `agent/utils/fibery.py`.
- Rich text field fetching adds latency to entity detail loading — each rich text field requires an extra API call.

## System-Wide Impact

- **Interaction graph:** Fibery webhook → `process_fibery_entity()` → `langgraph_client.runs.create()` → agent execution → `fibery_comment`/`fibery_state` tools → Fibery API. State updates in Fibery could re-trigger webhooks if the webhook subscription is not scoped correctly.
- **Error propagation:** Auth failures route through `leave_failure_comment()` in `auth.py` — needs a new `source == "fibery"` branch. Tool failures should return `{"success": False, "error": "..."}` following existing patterns.
- **State lifecycle risks:** The 3-step comment creation process can leave orphaned comment entities if step 2 or 3 fails. Implement retry-once logic per step.
- **API surface parity:** All existing integrations (Linear, Slack, GitHub) have: webhook endpoint, processor, comment tool, auth failure routing. Fibery must match this.

## Components & Files

| Component | File | Purpose |
|---|---|---|
| Webhook endpoint | `agent/webapp.py` | `POST /webhooks/fibery` — verify token, parse payload, dispatch to processor |
| Entity fetching | `agent/webapp.py` | `fetch_fibery_entity_details()` — get entity data, fetch rich text via document secrets |
| Thread ID generation | `agent/webapp.py` | `generate_thread_id_from_fibery_entity()` — deterministic UUID with `fibery-entity:` prefix |
| Processing function | `agent/webapp.py` | `process_fibery_entity()` — build prompt from entity context, resolve user, create run |
| Comment tool | `agent/tools/fibery_comment.py` | Agent tool to post comments back to Fibery entity |
| State update tool | `agent/tools/fibery_state.py` | Agent tool to update entity workflow state |
| Utility functions | `agent/utils/fibery.py` | API helpers: create comment (3-step), update state, fetch entity, rate limiting |
| Auth routing | `agent/utils/auth.py` | Add `source == "fibery"` to `leave_failure_comment()` and helper functions (lines 55-77, 203-265) |
| System prompt | `agent/prompt.py` | Add `fibery_comment`/`fibery_state` to tool instructions (line 47-49) and PR title format using Github Tag field (line 228) |
| Server config | `agent/server.py` | Extract `fibery_entity` from configurable, pass Github Tag to prompt, register tools (lines 367-389) |
| Tool registration | `agent/tools/__init__.py` | Export `fibery_comment` and `fibery_state` |

### Environment Variables

| Variable | Purpose |
|---|---|
| `FIBERY_API_TOKEN` | API token for Fibery REST/Command API |
| `FIBERY_WORKSPACE_URL` | Fibery workspace URL (e.g., `https://yourteam.fibery.io`) |
| `FIBERY_WEBHOOK_URL_TOKEN` | Secret token for webhook URL authentication |

## Implementation Phases

### Phase 1: Foundation (Utils + Auth)

1. Create `agent/utils/fibery.py` with:
   - Fibery Command API client (entity query, document fetch)
   - Comment creation (3-step, with retry-once per step)
   - State update function
   - Rate limiter (3 req/s)
2. Update `agent/utils/auth.py`:
   - Add `"fibery"` handling to `leave_failure_comment()`, `_retry_instruction()`, `_source_account_label()`, `_auth_link_text()`, `_work_item_label()`

**Success criteria:** Can call Fibery API, post a comment, update state from a test script.

### Phase 2: Webhook + Processor

1. Add `POST /webhooks/fibery` endpoint to `agent/webapp.py`:
   - Verify `token` query parameter against `FIBERY_WEBHOOK_URL_TOKEN`
   - Parse webhook payload (comment events vs. state change events)
   - Bot loop detection (skip comments from the API token's user)
   - Return 200 immediately, process in background
2. Add `fetch_fibery_entity_details()`:
   - Query entity via Command API
   - Fetch rich text content via document secrets
   - Fetch all existing comments for full context (especially for state-change triggers)
3. Add `generate_thread_id_from_fibery_entity()` with `fibery-entity:` prefix
4. Add `process_fibery_entity()`:
   - Resolve user email from webhook payload (follow-up API call if needed)
   - Parse repo field (comma-separated `owner/repo` format)
   - Apply `_is_repo_org_allowed()` check
   - For multi-repo: spawn a separate run per repo with thread IDs like `fibery-entity:{entity_id}:{owner/repo}`
   - Build configurable dict with `source: "fibery"`, `fibery_entity: {...}`, `user_email`, `repo`
   - Check thread activity, queue or create run

**Success criteria:** Webhook triggers agent run; entity context appears in prompt.

### Phase 3: Agent Tools + Prompt

1. Create `agent/tools/fibery_comment.py`:
   - Thin wrapper calling `agent/utils/fibery.py` comment function
   - Read entity ID from `config["configurable"]["fibery_entity"]`
   - Returns `{"success": True/False}` pattern
2. Create `agent/tools/fibery_state.py`:
   - Thin wrapper calling state update utility
   - Accept target state name as parameter
3. Update `agent/tools/__init__.py` to register both tools
4. Update `agent/server.py`:
   - Import and add tools to tool list (line 381-389)
   - Extract `fibery_entity` from configurable, pass Github Tag to `construct_system_prompt()` (lines 367-380)
5. Update `agent/prompt.py`:
   - Add `fibery_comment` and `fibery_state` to source-aware tool instructions (line 47-49)
   - Add Fibery PR title format using Github Tag field (line 228)

**Success criteria:** Agent uses correct tools for Fibery source; PR titles include Github Tag; comments appear on Fibery entity.

## Acceptance Criteria

### Functional Requirements

- [x] Webhook at `POST /webhooks/fibery?token=SECRET` receives and verifies Fibery webhooks
- [x] `@openswe` comment mention on a Fibery entity triggers an agent run
- [x] Workflow state change to a configured state triggers an agent run
- [x] Agent receives full entity context (title, description, comments) in prompt
- [x] Agent posts progress updates as comments on the Fibery entity via `fibery_comment` tool
- [x] Agent updates entity workflow state via `fibery_state` tool
- [x] PR titles include the `Github Tag` field value (e.g., `feat: Add feature [TASK-1104]`)
- [x] Multi-repo entities spawn separate runs per repo
- [x] Bot comments do not re-trigger webhooks (infinite loop prevention)
- [x] Auth failures post helpful error comments back to the Fibery entity
- [x] `_is_repo_org_allowed()` check applies to Fibery-routed repos
- [x] Missing/empty repo field posts an error comment instead of crashing

### Non-Functional Requirements

- [x] Rate limiting respects Fibery's 3 req/s limit
- [ ] Comment creation retries once per step on transient failures
- [x] Webhook handler returns 200 within 1 second (processing happens in background)

### Testing Requirements

- [ ] Unit tests for webhook payload parsing (comment event, state change event)
- [x] Unit tests for bot loop detection
- [x] Unit tests for repo field parsing (single repo, multi-repo, empty, invalid)
- [x] Unit tests for Fibery utility functions (comment creation, state update)
- [ ] Integration test: end-to-end webhook → agent run creation

## Questions to Resolve Before Implementation

These were identified during SpecFlow analysis and must be answered before or during Phase 2:

1. **Webhook payload structure:** What is the exact JSON shape for comment-created and state-changed events? → Consult Fibery API docs or capture a sample payload.
2. **Bot detection:** How does the webhook payload identify API-created comments vs. human comments? → Check if author ID matches the API token's user, or use a skip-list of recently-posted comment IDs.
3. **User email resolution:** Does the webhook payload include the actor's email, or does it require a follow-up API call? → Likely requires a follow-up call to Fibery's user endpoint.
4. **State change actor:** For workflow state change triggers, use the state changer, entity assignee, or entity creator for GitHub auth? → Default: assignee, fallback to creator.
5. **Repo field name and format:** Exact Fibery field name and expected value format (`owner/repo`). → Confirm with workspace setup.
6. **Workflow state names:** Exact state names for "In Progress" and "PR Ready" equivalents. → Make configurable via env var or document the expected names.
7. **Webhook database scope:** Which 1-2 Fibery database types should the webhook subscribe to initially? → Confirm with team.

## Dependencies & Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Secret URL token is weaker than HMAC | Webhook spoofing if URL leaks | HTTPS only, keep token out of logs, rotate periodically |
| 3 req/s rate limit is very low | Slow comment posting, contention between comment and state tools | Rate limiter in utils, batch where possible |
| 3-step comment creation can partially fail | Orphaned comment entities in Fibery | Retry-once per step, log warnings |
| Multi-repo is architecturally novel | No existing pattern to follow | Spawn separate runs per repo (simplest approach) |
| Fibery webhook payloads may differ from docs | Parsing failures | Capture real payloads early, build from samples |
| Rich text document secrets may expire | Entity description fetch failures | Fetch immediately, don't cache secrets |

## Sources & References

### Origin

- **Brainstorm document:** [docs/brainstorms/2026-03-18-fibery-integration-brainstorm.md](docs/brainstorms/2026-03-18-fibery-integration-brainstorm.md) — Key decisions carried forward: direct integration pattern (no abstraction), dual triggers (comment + state change), per-entity repo field routing, Github Tag for PR titles

### Internal References

- Linear webhook handler: `agent/webapp.py:833`
- Linear processor: `agent/webapp.py:481`
- Linear comment tool: `agent/tools/linear_comment.py`
- Linear utils: `agent/utils/linear.py`
- Auth failure routing: `agent/utils/auth.py:203-265`
- System prompt tool instructions: `agent/prompt.py:47-49`
- PR title format: `agent/prompt.py:228`
- Server tool registration: `agent/server.py:381-389`
- Extension guide: `CUSTOMIZATION.md:254-348`
- Thread ID generation: `agent/webapp.py:239-268`
- Org allowlist check: `agent/webapp.py:297`

### External References

- Fibery Webhooks API: `/api/webhooks/v2`
- Fibery Command API: `/api/commands`
- Fibery Documents API: `/api/documents/{secret}`
