# TOOLS.md

This file contains local setup facts specific to the `profile-curator` agent.

## Role boundary

This agent is proposal-only.

It does not fetch source artifacts.
It does not perform final receipt review in the UI.
It does not activate rules directly.

## Relevant local paths

- Project root: `~/receipt-tracker`
- App main file: `~/receipt-tracker/app/main.py`
- Artifact root: `~/receipt-tracker/data/artifacts/`
- Database: `~/receipt-tracker/data/receipts.db`
- Parsing package: `~/receipt-tracker/app/parsing/`
- Learning file (present in project): `~/receipt-tracker/app/learning.py`

## Current system direction

The app is moving toward:
- canonical artifact packages
- deterministic parsing first
- selective AI review second
- approved feedback captured over time
- reusable profile fragments proposed for approval
- fewer model calls over time as learning improves

## Current learning intent

This agent should eventually work from:
- approved receipts
- approved item corrections
- repeated merchant patterns
- repeated layout/source patterns
- future learning tables or proposal stores

## Important design rule

Stable agent instructions belong in workspace files.

Evolving learned knowledge should not be stuffed into AGENTS.md or TOOLS.md.
Instead, learned knowledge should live in structured data stores and approved profile layers.

## Proposal targets

Useful proposal types may include:
- merchant_profile_fragment
- source_profile_fragment
- item_mapping_bundle
- layout_hint_bundle
- alias_bundle

## Examples of reusable evidence

- repeated section headers like GROCERY / MEAT / PRODUCE
- repeated right-side paid price behavior
- repeated abbreviation expansions
- repeated merchant alias normalization
- repeated receipt-layout quirks across multiple approved artifacts

## Constraints

- Do not assume a proposal should become active immediately.
- Do not assume the current DB schema already has final learning/proposal tables.
- Keep outputs compact and easy to review manually.