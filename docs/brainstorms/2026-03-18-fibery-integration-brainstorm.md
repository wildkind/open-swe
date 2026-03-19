# Brainstorm: Fibery Integration for Open SWE

**Date:** 2026-03-18
**Status:** Complete

## What We're Building

Add Fibery as an issue tracker integration for Open SWE, allowing the agent to be triggered from Fibery entities and communicate results back. This mirrors the existing Linear integration pattern but adapts to Fibery's API and the team's specific Fibery workspace setup.

### Triggers

- **Comment mentions:** `@openswe` in a Fibery entity comment (same pattern as Linear)
- **Workflow state changes:** Moving an entity to a specific state (e.g., "Ready for Dev") triggers the agent

### Repo Routing

- A **multi-select or comma-separated text field** on each Fibery entity specifies the target GitHub repo(s) (e.g., `owner/repo1, owner/repo2`)
- For multi-repo issues, the agent creates linked PRs across repos

### Agent Output

- Post progress updates and results as **comments** on the Fibery entity
- **Automatically update workflow state** (e.g., "In Progress" -> "PR Ready")

### PR Title Format

- Use the existing `Github Tag` field on Fibery entities (e.g., `[TASK-1104]`) directly in PR titles
- No need to derive identifiers — the field value is used as-is

### Scope

- Support 1-2 Fibery databases initially (exact databases TBD)
- Design flexibly enough to add more databases without code changes

## Why This Approach

**Direct integration (no shared abstraction)** — following the same bespoke pattern used by Linear, GitHub, and Slack integrations. Reasons:

- The `CUSTOMIZATION.md` documents exactly this extension pattern (webhook + processor + tool)
- Only two issue trackers needed — abstracting is premature
- Each tracker has enough differences (API shape, auth, webhook format) that a shared interface would be leaky
- Fastest path to shipping

## Key Decisions

1. **Direct integration pattern** over abstract interface — YAGNI, matches codebase conventions
2. **Dual trigger support** — both comment mentions and workflow state changes
3. **Entity field for repo mapping** — not a hardcoded dictionary, since repos vary per entity
4. **Multi-repo support** — comma-separated repo field, agent creates linked PRs
5. **Github Tag field for PR titles** — reuse existing `[TASK-1104]` style identifiers
6. **Comments + state updates** for agent output — not just comments

## Components Needed

| Component | File(s) | Purpose |
|---|---|---|
| Webhook endpoint | `agent/webapp.py` | `POST /webhooks/fibery` — receive and verify webhooks |
| Issue fetching | `agent/webapp.py` | `fetch_fibery_entity_details()` — get entity data via Fibery API |
| Repo routing | `agent/webapp.py` | Read repo field from entity, support multi-repo |
| Processing function | `agent/webapp.py` | `process_fibery_entity()` — build prompt, create agent run |
| Comment tool | `agent/tools/fibery_comment.py` | Agent tool to post comments back to Fibery |
| State update tool | `agent/tools/fibery_state.py` | Agent tool to update entity workflow state |
| Utility functions | `agent/utils/fibery.py` | API helpers: comment, state update, reaction |
| Auth routing | `agent/utils/auth.py` | Handle `source == "fibery"` for failure comments |
| System prompt | `agent/prompt.py` | Source-aware PR title format using Github Tag field |
| Server config | `agent/server.py` | Pass `fibery_entity` metadata, include Fibery tools |

## Environment Variables

| Variable | Purpose |
|---|---|
| `FIBERY_API_TOKEN` | API token for Fibery REST/GraphQL API |
| `FIBERY_WORKSPACE_URL` | Fibery workspace URL (e.g., `https://yourteam.fibery.io`) |
| `FIBERY_WEBHOOK_URL_TOKEN` | Secret token included in webhook URL for authentication (Fibery has no HMAC signing) |

## Fibery API Details

### Webhooks
- Subscribe via `POST /api/webhooks/v2` — fires on entity changes per database type
- State changes appear in `values`/`valuesBefore` diff in payload
- Comment creation fires as entity creation event, but **comment text requires separate fetch** (rich text stored as collaborative documents)
- **No HMAC signature verification** — use secret URL token (`/webhooks/fibery?token=SECRET`)

### Comments (3-step process, can be batched)
1. Create comment entity (`fibery.entity/create` with type `comments/comment`)
2. Link to parent entity (`fibery.entity/add-collection-items`)
3. Set content via documents API (`PUT /api/documents/{secret}?format=md`)

### Reading entities
- Command API: `POST /api/commands` with `fibery.entity/query`
- Rich text fields return a document secret — fetch content separately via `GET /api/documents/{secret}?format=md`

### Rate limits
- 3 requests/second per token — relevant for multi-repo scenarios

## Resolved Questions

1. **Fibery webhook capabilities:** Fibery supports outbound webhooks via `/api/webhooks/v2`. They fire on entity updates (including state changes). Comment creation events fire but text content requires a separate API fetch. **No HMAC signature verification** — using a secret URL token instead.
2. **Fibery API for comments:** 3-step process (create entity, link to parent, set document content). Can be batched. Reading requires querying the `comments/comments` collection then fetching each document by secret.
3. **State change trigger details:** When triggered by state change, use entity **title + description + all existing comments** as the prompt, giving the agent full context.
4. **Which databases?** TBD (1-2 databases). Design handles this via the webhook subscription per database type — no code changes needed to add more.
