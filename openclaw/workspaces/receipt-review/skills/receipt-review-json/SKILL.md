---
name: receipt-review-json
description: Review uncertain parsed receipt data and return conservative JSON suggestions only.
metadata: {"openclaw":{"emoji":"🧾","os":["linux"]}}
---

# Receipt Review JSON Skill

Use this skill when the request is about reviewing uncertain parsed receipt data.

## Purpose

This skill converts uncertain receipt/header/item data into a compact JSON suggestion payload.

It is for:
- uncertain item-name normalization
- uncertain category suggestions
- cautious header clarification
- cautious paid-price hints only when strongly supported

It is not for:
- portal browsing
- artifact retrieval
- database mutation
- approval decisions
- invented pricing

## Input contract

Typical request shape:

```json
{
  "request_type": "review_receipt_uncertainty",
  "artifact_id": "art_...",
  "receipt_context": {
    "merchant_raw": "Big Y",
    "merchant_canonical": "Big Y",
    "purchase_date": "2026-03-14",
    "receipt_total": 58.03
  },
  "raw_ocr_excerpt": "...",
  "uncertain_header_fields": [],
  "uncertain_items": [
    {
      "line_number": 3,
      "item_text_raw": "PRMLK ND QUESO $.43",
      "item_name_normalized": "Prmlk Queso",
      "category": "Pantry",
      "line_total": 3.49,
      "needs_review": true,
      "review_notes": "Used leading amount only."
    }
  ]
}
```

## Output contract

Return JSON only:

```json
{
  "suggestions": [
    {
      "target_type": "item",
      "line_number": 3,
      "field_name": null,
      "suggested_name": "Primal Kitchen No Dairy Queso",
      "suggested_category": "Pantry",
      "paid_price_hint": null,
      "confidence": "medium",
      "reason": "OCR price evidence is conflicting, but the item name is strongly supported."
    }
  ]
}
```

## Output rules

- Return exactly one JSON object.
- Top-level key must be `suggestions`.
- Never use markdown code fences.
- Never add prose before or after the JSON.
- If nothing useful can be suggested, return:
  `{"suggestions":[]}`

## Invalid outputs

These are invalid and must not be returned:

- any response wrapped in ``` fences
- any response using `review_suggestions`
- any response that omits the top-level `suggestions` key
- any response that invents price values from weak OCR

## Field rules

### `target_type`
Allowed values:
- `item`
- `header`

### For item suggestions
Include:
- `target_type`
- `line_number`
- `field_name` as `null`
- `suggested_name`
- `suggested_category`
- `paid_price_hint`
- `confidence`
- `reason`

### For header suggestions
Include:
- `target_type`
- `line_number` as `null`
- `field_name`
- optional relevant suggestion fields
- `confidence`
- `reason`

## Pricing rules

- Never invent a price.
- If evidence is weak, contradictory, malformed, or missing, set `paid_price_hint` to `null`.
- It is always better to leave pricing blank than to guess wrong.

## Category rules

Use only this taxonomy:
- Produce
- Dairy
- Meat & Seafood
- Bakery
- Frozen
- Pantry
- Snacks
- Beverages
- Household
- Personal Care
- Prepared Foods
- Uncategorized

## Efficiency rules

- Focus only on uncertain fields/items passed into the request.
- Do not restate the entire receipt.
- Keep reasons short and practical.
- Prefer one good suggestion over many weak ones.