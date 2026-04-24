---
name: bigy_browser_fetch
description: Fetch Big Y receipt artifacts through the browser with explicit user login and save raw files locally for the receipt-tracker app.
metadata: {"openclaw":{"emoji":"🛒","os":["linux"]}}
---

# Big Y Browser Fetch Skill

Use this skill when the user wants to fetch receipt artifacts from their Big Y account through a browser session.

## Purpose

This skill is for bounded, human-triggered receipt retrieval.

It should:
- open the browser
- help the user reach the Big Y receipt/history area
- let the user log in manually when needed
- fetch one or a small number of receipts
- preserve raw artifacts locally
- hand off to the existing receipt-tracker ingestion/review flow

It should NOT:
- autonomously crawl the account landscape
- store credentials
- silently write to the receipt database
- guess receipt contents without preserving source artifacts

## Tool Choice

For Big Y login or receipt-history pages, prefer the Browser tool.

Do NOT prefer lightweight web fetch/search tools for this workflow because:
- login is involved
- JS-heavy pages may be involved
- the user may need to click or approve steps manually

## Browser Profile Guidance

Default:
- use the isolated OpenClaw-managed browser profile

Only use the user browser profile when:
- the user explicitly wants to reuse an existing logged-in session
- the user is at the computer
- the user can approve any attach/profile prompts

## Output Locations

Save fetched artifacts here:

- inbox: `~/receipt-tracker/imports/bigy-manual/inbox`
- manifests: `~/receipt-tracker/imports/bigy-manual/manifests`
- processed: `~/receipt-tracker/imports/bigy-manual/processed`

## Naming Convention

Use a timestamped base name like:

- `YYYYMMDD_HHMMSS_bigy_receipt.png`
- `YYYYMMDD_HHMMSS_bigy_receipt.html`
- `YYYYMMDD_HHMMSS_bigy_receipt.json`

If multiple receipts are fetched in one user-approved run, append an index:
- `_01`
- `_02`
- `_03`

## Required Behavior

1. Keep the scope bounded.
   - Default to one receipt at a time.
   - Do not fetch more than 3 receipts in one run unless the user explicitly asks.

2. Preserve raw artifacts.
   - Prefer a full-page screenshot or saved image artifact first.
   - If useful, also preserve HTML/snapshot text as a secondary artifact.
   - Never discard the original fetched artifact.

3. Keep the user in the loop.
   - Let the user perform login manually.
   - Let the user choose which receipt to fetch when multiple are visible.

4. Create a manifest.
   - For each fetched receipt, create a JSON manifest in the manifests folder containing:
     - merchant
     - fetch timestamp
     - source URL if available
     - browser profile used
     - artifact paths
     - notes about what was fetched
     - whether the artifact is an image, html page, screenshot, or mixed bundle

5. Do not interpret the receipt as final truth at fetch time.
   - Retrieval is separate from interpretation.
   - The receipt-tracker app remains the system of record.

## Big Y-Specific Notes

Big Y may expose:
- receipt pages
- receipt-like views
- account order/purchase history pages

The connector should focus on retrieval only.

Do not:
- invent missing totals
- infer line items from partial browser views unless source artifacts are preserved
- overwrite existing receipt-tracker records

## Handoff to the App

After a receipt is fetched:
- report the saved paths
- recommend uploading the artifact through the local app:
  - `http://127.0.0.1:8000/upload`

If the fetched artifact is HTML rather than an image:
- preserve it anyway
- tell the user it may need a later HTML-to-artifact conversion step before upload

## Suggested User-Facing Summary

After a successful fetch, summarize briefly:
- what was fetched
- where it was saved
- what the next safe step is

Example:
- “Fetched 1 Big Y receipt artifact and saved it to the inbox and manifests folders. Next safe step: upload the saved artifact through the receipt-tracker app for OCR/parsing/review.”

## Safety Boundary

This skill is retrieval-only.
Interpretation, parsing, OpenClaw review suggestions, and database approval happen later in the normal receipt-tracker workflow.

