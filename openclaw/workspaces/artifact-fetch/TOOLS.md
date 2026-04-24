# TOOLS.md

This file contains local setup facts specific to the `artifact-fetch` agent.

## Role boundary

This agent is retrieval-only.

It may gather and preserve source artifacts, but it does not perform final receipt interpretation and does not mutate application accounting data.

## Relevant local paths

- Project root: `~/receipt-tracker`
- App main file: `~/receipt-tracker/app/main.py`
- Artifact root: `~/receipt-tracker/data/artifacts/`
- Legacy Big Y manual import inbox: `~/receipt-tracker/imports/bigy-manual/inbox`
- Legacy Big Y processed folder: `~/receipt-tracker/imports/bigy-manual/processed`
- Legacy Big Y manifests folder: `~/receipt-tracker/imports/bigy-manual/manifests`
- Database: `~/receipt-tracker/data/receipts.db`

## Current design direction

The system is moving toward canonical artifact packages under:

`~/receipt-tracker/data/artifacts/<artifact_id>/`

Expected contents:
- `manifest.json`
- `raw/`
- `derived/`

The app already creates these packages for new manual uploads.

This agent should eventually produce the same package shape for external sources.

## Artifact manifest expectations

Current canonical manifest ideas:
- `schema_version`
- `artifact_id`
- `created_at`
- `source.type`
- `source.name`
- `source.connector_skill`
- `source.acquisition_mode`
- `artifacts[]`
- `primary_renderable_path`
- `parser_hints.artifact_kind`
- `parser_hints.needs_ocr`
- `integrity.immutable`
- `workflow.status`

## Source.type examples

- `manual_upload`
- `portal_fetch`
- `discord_attachment`
- `email_attachment`
- `pdf_drop`
- `html_capture`
- `screenshot_capture`

## artifact_kind examples

- `image_receipt`
- `pdf_receipt`
- `html_receipt_page`
- `mixed_bundle`
- `unknown`

## workflow.status examples

- `fetched`
- `normalized`
- `parsed`
- `reviewed`
- `approved`
- `rejected`
- `archived`

## Browser guidance

When browser work is needed:
- default to the managed OpenClaw browser profile
- keep retrieval bounded
- prefer explicit operator-triggered login
- preserve raw artifacts rather than paraphrasing portal content

## Design intent

Keep this agent merchant-agnostic.

Merchant-specific and source-specific logic should live in connector skills under `skills/`.

## Current practical note

The currently working OpenClaw suggestion path was simplified for the `receipt-review` use case.

Do not assume browser-heavy retrieval is ready to be wired into production flow yet.
This workspace setup is primarily for role separation and future connector work.