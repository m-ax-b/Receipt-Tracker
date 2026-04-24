# Receipt Learning Demo

A receipt-domain prototype for a governed AI learning loop.

This project demonstrates a pattern for turning approved human corrections into reusable knowledge. Deterministic logic handles the obvious cases, OpenClaw orchestrates fit-for-purpose AI where ambiguity remains, and only approved patterns become active runtime knowledge.

The current domain is grocery receipts because it is easy to understand and safe to demo. The larger architectural goal is a reusable evidence-learning framework that can later be adapted to more complex workflows.

## Why this project exists

Most AI demos stop at one-off prediction. This project is focused on something more operationally useful:

- ingest raw evidence
- preserve canonical artifacts
- extract structure deterministically where possible
- invoke AI selectively for unresolved ambiguity
- keep human approval as the trust boundary
- convert approved outcomes into reusable knowledge

That learning loop is the main product idea.

## Current architecture

The app is intentionally built around a governed flow:

1. **Observe** – manual uploads and review actions create evidence
2. **Reason** – deterministic logic runs first, OpenClaw is used selectively for ambiguous cases
3. **Propose** – repeated evidence is turned into conservative reusable proposals
4. **Approve** – human approval remains the activation boundary
5. **Reuse** – approved patterns participate in future review and reduce repeat work

## Current demo capabilities

- Manual receipt upload
- Canonical artifact package creation under `data/artifacts/<artifact_id>/`
- Review UI for header fields and line items
- Suggestion provenance in the UI
- Exact learned suggestion reuse
- Alias proposal generation from repeated approved evidence
- Line-item repair actions:
  - add missing line
  - merge selected lines
  - suppress selected lines
  - delete bogus line
- Learning observations captured from:
  - approved item observations
  - manual add actions
  - merge actions
  - suppression actions
- Proposal lifecycle for reusable knowledge:
  - observed
  - proposed
  - approved
  - active

## Why OpenClaw is in the loop

OpenClaw is used here as the selective AI orchestration layer. The point is not to send every problem to a frontier model. The point is to:

- keep deterministic logic in front
- preserve flexibility in model choice
- avoid locking the workflow to one provider
- use AI where it creates the most leverage
- feed approved results back into reusable knowledge

In the current prototype, OpenClaw is used selectively for receipt ambiguity that local logic and existing learned patterns cannot yet resolve.


## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Then open the app in your browser and go to:

- `/upload`
- `/receipts`
- `/learning/proposals`
- `/analytics`

## Current maturity

This is a prototype, not a finished product. The UI and learning model are intentionally conservative. Human review is still required. The value is in the governed architecture and in the ability to convert repeated cleanup work into reusable runtime knowledge.

## What is next

The most immediate next slice is:

- suppression-pattern proposals
- runtime repair hints
- better measurement of what is handled locally vs what is sent to OpenClaw

Longer term, the same framework is intended to support more complex evidence workflows, including communications incident root-cause analysis.


