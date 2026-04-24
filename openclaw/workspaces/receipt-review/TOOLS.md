# TOOLS.md

This file contains local setup facts specific to the `receipt-review` agent.

## Role boundary

This agent is review-only.
It does not fetch artifacts and does not mutate application data.

## Relevant local paths

- Project root: `~/receipt-tracker`
- App main file: `~/receipt-tracker/app/main.py`
- Review template: `~/receipt-tracker/app/templates/receipt_detail.html`
- Database: `~/receipt-tracker/data/receipts.db`
- Uploaded images: `~/receipt-tracker/data/images/`
- Canonical artifact root (planned): `~/receipt-tracker/data/artifacts/`

## Expected working pattern

This agent is expected to receive compact review payloads from the app or operator, not broad project dumps.

Typical inputs include:
- receipt context
- OCR excerpt
- uncertain header fields
- uncertain items

## Local constraints

- Do not assume browser access is relevant.
- Do not assume portal credentials are relevant.
- Do not assume direct file writes are allowed or needed.
- Do not assume any suggestion should be auto-applied.

## Design intent

This agent should remain small and efficient.
Stable instructions belong here.
Merchant-specific and evolving learned knowledge should live in data stores and approved profile layers, not in this file.