from datetime import datetime

from app.parsing.ocr import extract_structured_ocr_lines, extract_text_from_image
from app.parsing.extractors import (
    extract_merchant,
    extract_merchant_candidates,
    extract_purchase_date,
    extract_total,
)
from app.parsing.line_items import extract_line_items


def build_review_notes(merchant_raw, purchase_date, total, merchant_candidates, items):
    notes = []

    if merchant_raw:
        notes.append(f"Merchant candidate: {merchant_raw}")
    else:
        notes.append("Merchant not confidently extracted.")

    if purchase_date:
        notes.append(f"Purchase date candidate: {purchase_date}")
    else:
        notes.append("Purchase date not confidently extracted.")

    if total is not None:
        notes.append(f"Total candidate: {total:.2f}")
    else:
        notes.append("Total not confidently extracted.")

    if merchant_candidates:
        top_candidates = merchant_candidates[:3]
        candidate_text = "; ".join(
            [f"line {idx} score {score}: {line}" for score, idx, line in top_candidates]
        )
        notes.append(f"Top merchant candidates: {candidate_text}")

    item_count = len(items)
    review_item_count = sum(1 for item in items if item.get("needs_review"))
    notes.append(
        f"Line item extraction v1 found {item_count} item(s); {review_item_count} need review."
    )
    notes.append("Header fields extracted from OCR. Please review before approval.")
    return " ".join(notes)


def parse_receipt_image(image_path: str) -> dict:
    now = datetime.utcnow().isoformat()
    ocr_text = extract_text_from_image(image_path)
    structured_ocr_lines = extract_structured_ocr_lines(image_path)

    merchant_candidates = extract_merchant_candidates(ocr_text)
    merchant_raw, merchant_canonical = extract_merchant(ocr_text)
    purchase_date = extract_purchase_date(ocr_text)
    total = extract_total(ocr_text)

    items = extract_line_items(
        ocr_text,
        merchant_canonical=merchant_canonical,
        structured_ocr_lines=structured_ocr_lines,
        receipt_total=total,
    )

    review_notes = build_review_notes(
        merchant_raw=merchant_raw,
        purchase_date=purchase_date,
        total=total,
        merchant_candidates=merchant_candidates,
        items=items,
    )

    return {
        "receipt": {
            "merchant_raw": merchant_raw,
            "merchant_canonical": merchant_canonical,
            "purchase_date": purchase_date,
            "purchase_time": None,
            "subtotal": None,
            "tax": None,
            "total": total,
            "currency": "USD",
            "ocr_text_raw": ocr_text,
            "receipt_confidence": 0.82,
            "status": "needs_review",
            "review_notes": review_notes,
            "updated_at": now,
        },
        "items": items,
    }
