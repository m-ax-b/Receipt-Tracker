---
name: bigy-portal-fetch
description: Retrieve one or a few Big Y receipt artifacts from the Big Y portal, preserve raw artifacts locally, and update the canonical artifact package.
metadata: {"openclaw":{"emoji":"🛒","os":["linux"]}}
---

# Big Y Portal Fetch Skill

Use this skill when the user wants to retrieve receipt artifacts from the Big Y portal.

## Purpose

This skill is the first connector skill under the merchant-agnostic `artifact-fetch` agent.

It should help the user retrieve Big Y receipt artifacts in a bounded, review-safe way.

## Scope

Default scope:
- one receipt artifact
- one bounded session
- preserve raw artifacts
- write or update a manifest
- stop

Do not fetch more than a few artifacts unless the user explicitly asks.

## Retrieval principles

1. Retrieval only.
2. Preserve original artifacts.
3. Prefer a clear screenshot, image, PDF, or page capture over paraphrasing.
4. Keep the operator in the loop for login-gated steps.
5. Save enough context to support later parsing and review.

## Allowed source patterns

Big Y may present:
- receipt history pages
- receipt detail pages
- printable receipt-like pages
- image-like receipt captures
- HTML views that need a renderable capture

Any of these are acceptable as long as raw artifacts are preserved.

## Output target

Normalize the fetched result into a canonical artifact package under:

`~/receipt-tracker/data/artifacts/<artifact_id>/`

Expected contents:
- `manifest.json`
- `raw/`
- `derived/`

## Minimum manifest expectations

At minimum, write or update fields that identify:
- source.type = `portal_fetch`
- source.name = `bigy`
- source.connector_skill = `bigy-portal-fetch`
- source.acquisition_mode = `human_triggered`
- source_url when available
- page_title when available
- captured artifacts
- primary_renderable_path
- parser_hints.artifact_kind
- workflow.status = `fetched` or `normalized`

## Preferred artifact capture order

Prefer, in order:
1. original downloadable file if available
2. clean receipt image
3. page screenshot/full-page screenshot
4. HTML capture plus a renderable screenshot

## Do not do these things

Do not:
- interpret totals as final truth
- write to `receipts.db`
- auto-import into the app
- approve or reject anything
- silently widen the fetch scope
- store credentials in files or chat

## Browser profile guidance

Default to the managed OpenClaw browser profile.

Use a user profile only when:
- the user explicitly wants an existing signed-in session
- the user is present
- the user can approve any attach or login actions

## Suggested completion summary

After a successful fetch, summarize briefly:
- what was captured
- where it was saved
- whether the artifact is image, PDF, HTML, or mixed
- the next safe step

Example:
- “Fetched 1 Big Y receipt artifact, saved it as a canonical artifact package, and preserved the primary renderable artifact for later parsing/review.”