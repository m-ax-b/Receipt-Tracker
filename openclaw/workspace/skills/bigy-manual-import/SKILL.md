---
name: bigy_manual_import
description: Help manually ingest downloaded Big Y receipt files into the local receipt-tracker workflow.
metadata: {"openclaw":{"emoji":"🧾","os":["linux"]}}
---

# Big Y Manual Import Skill

Use this skill when the user wants help importing Big Y receipt files that were manually downloaded, exported, screenshotted, or saved locally.

## Scope

This skill is for:
- manual receipt screenshots
- exported receipt images
- PDFs
- copied OCR text that belongs to a Big Y receipt

This skill is NOT for:
- live login automation
- credentialed portal access
- scheduled background ingestion
- autonomous syncing

Stay manual and human-controlled.

## Local Paths

Use these paths for the manual import flow:

- Inbox: `~/receipt-tracker/imports/bigy-manual/inbox`
- Processed: `~/receipt-tracker/imports/bigy-manual/processed`
- App root: `~/receipt-tracker`
- App upload page: `http://127.0.0.1:8000/upload`

## Core Behavior

When helping with Big Y manual imports:

1. Preserve original files.
- Never delete the user’s original receipt files.
- Prefer copying into the inbox, not moving, unless the user explicitly asks.

2. Keep the workflow simple.
- One receipt at a time.
- Prefer copy-paste terminal commands.
- Prefer safe, inspectable steps.

3. Use the existing receipt-tracker app as the system of record.
- The app upload flow remains primary.
- Do not bypass the app unless explicitly asked.

4. Stay conservative on OCR-derived pricing.
- Big Y may contain dual-price rows.
- The right-side price is the likely paid/member price only when clearly visible.
- If OCR evidence is contradictory, do not guess the paid price.

5. Respect the current human-review workflow.
- Deterministic parser first.
- OpenClaw suggestions second.
- Human review decides final values.

## Big Y-Specific Notes

Big Y receipts may include:
- section headers like `GROCERY`, `MEAT`, `PRODUCE`
- dual-price rows where left and right prices differ
- OCR abbreviation-heavy item names

Helpful behavior:
- preserve `category_source_raw` evidence when relevant
- expand item names conservatively
- do not force a paid price when the OCR is inconsistent

## Recommended Manual Import Flow

When the user asks to import a Big Y receipt manually:

1. Ask or help them place the file into:
`~/receipt-tracker/imports/bigy-manual/inbox`

2. Confirm which file should be processed.

3. Recommend uploading it through:
`http://127.0.0.1:8000/upload`

4. After upload, help review:
- merchant
- date
- total
- line items
- OpenClaw suggestions for `needs_review` items

5. Optionally suggest moving the handled file into:
`~/receipt-tracker/imports/bigy-manual/processed`

## Output Style

When helping with this workflow:
- prefer short copy-paste commands
- prefer explicit filenames and paths
- do not invent receipt facts
- do not assume login automation exists
- do not recommend autonomous syncing unless explicitly requested later

