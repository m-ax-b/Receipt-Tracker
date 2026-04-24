---
name: profile-proposal
description: Examine approved receipt evidence and return conservative structured profile proposals for human approval.
metadata: {"openclaw":{"emoji":"🧠","os":["linux"]}}
---

# Profile Proposal Skill

Use this skill when the task is to turn approved receipt evidence into structured, reviewable proposals.

## Purpose

This skill helps the system learn safely over time.

It does this by:
- looking at repeated approved evidence
- identifying reusable patterns
- turning those patterns into compact proposals
- leaving human approval as the trust boundary

## This skill is for

- merchant profile fragment proposals
- source/layout hint proposals
- repeated item normalization proposals
- alias normalization proposals

## This skill is not for

- artifact retrieval
- live receipt review UI suggestions
- direct parser mutation
- direct database activation
- automatic production rule changes

## Input contract

Typical request shape:

```json
{
  "request_type": "propose_profile_updates",
  "scope": {
    "merchant": "Big Y",
    "lookback_days": 180,
    "min_confirmations": 2
  },
  "evidence": {
    "approved_receipt_ids": [12, 14, 19],
    "approved_artifact_ids": [
      "art_20260320_101500_bigy_001",
      "art_20260321_111500_bigy_001"
    ],
    "learned_item_mappings": [
      {
        "merchant_key": "BIG Y",
        "raw_text_key": "PRMLK ND QUESO",
        "approved_name": "Primal Kitchen No Dairy Queso",
        "approved_category": "Pantry",
        "times_confirmed": 3
      }
    ],
    "layout_observations": [
      "section_headers_present",
      "dual_price_possible"
    ]
  }
}