# Brainstorm: Automated Spec Fleshing via Fibery State Change

**Date:** 2026-03-25
**Status:** Complete

## What We're Building

Automate requirements fleshing so the agent runs automatically when a task moves to **Backlog** — no manual `@openswe` comment needed. The agent checks that the task is ready (has content and a linked repo), fleshes out the spec using the tools we just built (`fibery_update_description`, `fibery_create_entity`), and marks the task as AI-specced via a new boolean field.

### Trigger

- **Workflow state change to Backlog** triggers the agent automatically
- The existing state-change webhook handler already detects state transitions — this extends it to route Backlog transitions to spec work

### Readiness Check

Before running, the agent (or the webhook handler) validates:
1. **Content exists** — Description OR Background & Brief is non-empty
2. **Repo linked** — At least one `Tech/Repository` is linked

If either is missing:
- Post a comment listing what's needed (e.g., "Please add a description and link a repository before I can flesh out this task")
- Leave the task in Backlog (don't move it)
- The human can add the missing info and re-trigger via `@openswe` comment

### Skip Logic

- A new **`AI Specced` boolean field** on `Tools/Task` tracks whether the agent has already specced the task
- If `AI Specced = true` when the task moves to Backlog, the agent skips silently
- The agent sets `AI Specced = true` after successfully fleshing out requirements
- A human can uncheck it to request a re-spec

### Behavior

- The Backlog trigger **only does spec work** — never implementation
- The agent uses the same tools and prompt section as the manual `@openswe` trigger
- The prompt for state-triggered spec work should be different from the existing "Please work on the following issue" prompt — it should instruct the agent to flesh out requirements, not implement

### Fibery Schema Changes

1. **Add `AI Specced` boolean field** to `Tools/Task` (default: false)
   - Agent sets to true after spec work
   - Human can uncheck to re-trigger
   - Used as skip gate on Backlog state transitions

### Open SWE Changes

1. **Webhook handler** — detect Backlog state transitions, run readiness check, route to spec-work prompt
2. **New utility** — `update_entity_field()` to set the `AI Specced` boolean (or use existing entity update pattern)
3. **New tool or prompt instruction** — agent sets `AI Specced = true` after completing spec work
4. **Spec-specific prompt** — for state-triggered runs, use a prompt that explicitly instructs spec work (not implementation)

## Why This Approach

**State-change trigger with readiness gate** — because:

- Backlog is the natural "make this actionable" transition in the team's workflow (Idea → Backlog → Next Up → In Progress)
- The readiness check prevents wasted runs on empty tasks, with graceful feedback when info is missing
- The `AI Specced` boolean is simple, deterministic, and prevents double-runs without complex heuristics
- Spec and implementation stay cleanly separated — Backlog = spec only

**Alternatives considered:**
- **Debounced creation trigger** — rejected because timing is unpredictable and tasks are created incrementally
- **New workflow state ("Needs Spec")** — rejected because it adds AI-specific clutter to everyone's workflow
- **Skip based on content analysis** — rejected in favor of a simple boolean; content analysis is fragile

## Key Decisions

1. **Backlog is the trigger state** — natural fit for "this is real, make it actionable"
2. **Readiness requires content AND repo** — both must be present for the agent to proceed
3. **On failure: comment + stay in Backlog** — agent tells you what's missing, human adds it, can re-trigger via `@openswe`
4. **`AI Specced` boolean prevents double-runs** — simple, human-resettable, deterministic
5. **Backlog trigger is spec-only** — never chains into implementation
6. **Extends existing infrastructure** — uses the state-change webhook handler and spec tools already built

## Resolved Questions

1. **Trigger mechanism** — Workflow state change to Backlog (not creation, not new state)
2. **Readiness requirements** — Non-empty Description/Brief AND linked repo
3. **Failure behavior** — Comment listing missing items, stay in Backlog
4. **Double-run prevention** — `AI Specced` boolean field (not content analysis)
5. **Field type** — Simple boolean, not a multi-state select
6. **Scope** — Backlog trigger does spec work only, never implementation
