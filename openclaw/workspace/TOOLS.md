# TOOLS.md

This file contains setup-specific notes for my local OpenClaw + receipt-tracker lab.

## Receipt Tracker App

- Project root: `~/receipt-tracker`
- Main app entrypoint: `app/main.py`
- Parser files:
  - `app/parsing/ocr.py`
  - `app/parsing/parser.py`
  - `app/parsing/extractors.py`
  - `app/parsing/line_items.py`
- Suggestion layer client:
  - `app/claw_client.py`
- Main review template:
  - `app/templates/receipt_detail.html`

## Run Commands

Start the receipt app:

    cd ~/receipt-tracker
    source .venv/bin/activate
    python -m uvicorn app.main:app --reload

App URL:
- `http://127.0.0.1:8000`

Useful pages:
- `/upload`
- `/receipts`
- `/analytics`

## Data Locations

- SQLite DB: `~/receipt-tracker/data/receipts.db`
- Uploaded images: `~/receipt-tracker/data/images/`

## OpenClaw Gateway

- Gateway URL: `http://127.0.0.1:18789`
- Agent ID: `main`
- Current model target: Gemini via OpenClaw
- OpenClaw suggestions are used as:
  - review-only
  - suggestion-only
  - never auto-authoritative
  - never allowed to invent prices

The receipt app expects these env vars when run from terminal:

    export OPENCLAW_BASE_URL="http://127.0.0.1:18789"
    export OPENCLAW_AGENT_ID="main"
    export OPENCLAW_GATEWAY_TOKEN='SET_IN_CURRENT_SHELL'
    export GEMINI_API_KEY='SET_IN_CURRENT_SHELL_IF_NEEDED'

## Current Review Workflow

For receipt line items:
1. Deterministic parser runs first
2. OCR-derived items are stored in SQLite
3. Items marked `needs_review=1` are eligible for OpenClaw suggestions
4. OpenClaw may suggest:
   - normalized item name
   - category
   - optional paid price hint
   - reason
5. Human review decides final values

OpenClaw suggestions should help with:
- OCR abbreviation expansion
- category correction
- reasoning about ambiguous grocery names

OpenClaw should NOT:
- invent prices
- silently overwrite data
- auto-approve receipt fields
- assume OCR is correct when values conflict

## Merchant Notes

### Big Y
- Grocery receipts may include section headers like:
  - `GROCERY`
  - `MEAT`
  - `PRODUCE`
- These are captured as `category_source_raw`
- The normalized category is stored separately for analytics
- Big Y may use dual-price rows:
  - left side can be non-member / shelf price
  - right side can be paid / member price
- If OCR corrupts both values, do not guess the paid price

### McDonald's
- One priced line may be followed by child lines with no explicit individual price
- Modifier lines like `NO PICKLE` are not products
- Shake/surcharge rows may appear separately
- Do not invent child-line prices without direct evidence

## Known Working Examples

### Big Y suggestion example
OpenClaw successfully suggested:
- `Primal Kitchen No Dairy Queso`
- category: `Pantry`
- no paid price hint because OCR values were contradictory

This is desired behavior.

## Development Preferences

When helping with this project:
- prefer copy-pasteable commands
- prefer replacing whole files over patch fragments
- keep steps small and testable
- preserve the working app
- explain changes briefly
- avoid unnecessary framework complexity

## Future Roadmap Notes

Near-term:
- tighten suggestion UX
- possibly create a workspace skill for receipt-review assistance
- later create a Big Y ingestion helper

Future Big Y ingestion should likely start with:
- manually exported/downloaded receipts
- then a helper skill that routes those files into the app
- only after that consider authenticated portal/app access

Do not jump straight to autonomous login or cron ingestion without an explicit user request.

