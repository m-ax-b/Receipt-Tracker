from __future__ import annotations

from datetime import datetime
from html import unescape
from urllib.parse import urljoin
import re

from app.parsing.extractors import normalize_date_string


BJS_MERCHANT_NAME = "BJ's Wholesale Club"


HTML_ITEM_BLOCK_PATTERN = re.compile(
    r"<app-order-details-item-level\b.*?</app-order-details-item-level>",
    flags=re.I | re.S,
)


HTML_LINK_PATTERNS = [
    re.compile(r'<a[^>]+href="([^"]+)"[^>]*>', flags=re.I | re.S),
    re.compile(r'routerlink="([^"]+)"', flags=re.I | re.S),
    re.compile(r'\[routerLink\]="([^"]+)"', flags=re.I | re.S),
]


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s{2,}", " ", (text or "").strip())



def _clean_html_text(html_text: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", html_text or "", flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return _collapse_ws(text)



def looks_like_bjs_saved_html(html_text: str) -> bool:
    sample = (html_text or "").lower()
    signals = 0

    if "bjs wholesale club" in sample or "bj&#39;s wholesale club" in sample:
        signals += 1
    if "managecluborders" in sample:
        signals += 1
    if "order summary" in sample or "order details" in sample:
        signals += 1
    if "app-order-details-item-level" in sample:
        signals += 1
    if "club location" in sample:
        signals += 1
    if "item total" in sample and "order total" in sample:
        signals += 1

    return signals >= 2



def _extract_saved_url(html_text: str) -> str | None:
    match = re.search(r"saved from url=.*?(https?://[^\s>]+)", html_text or "", flags=re.I)
    if match:
        return match.group(1).strip()
    return None



def _extract_order_number(saved_url: str | None) -> str | None:
    if not saved_url:
        return None

    match = re.search(r"manageClubOrders/(\d+)/(\d{8})", saved_url, flags=re.I)
    if match:
        return match.group(1)
    return None



def _extract_url_date(saved_url: str | None) -> str | None:
    if not saved_url:
        return None

    match = re.search(r"manageClubOrders/\d+/(\d{8})", saved_url, flags=re.I)
    if not match:
        return None

    raw = match.group(1)
    return normalize_date_string(f"{raw[4:6]}/{raw[6:8]}/{raw[0:4]}")



def _extract_amount(label: str, cleaned_text: str) -> float | None:
    pattern = rf"{re.escape(label)}\s*\$\s*([0-9]+(?:\.[0-9]{{2}})?)"
    match = re.search(pattern, cleaned_text, flags=re.I)
    if not match:
        return None

    try:
        return float(match.group(1))
    except ValueError:
        return None



def _extract_text_between(cleaned_text: str, start_label: str, end_label: str | None = None) -> str | None:
    if end_label:
        pattern = rf"{re.escape(start_label)}\s+(.+?)\s+{re.escape(end_label)}"
    else:
        pattern = rf"{re.escape(start_label)}\s+(.+)$"

    match = re.search(pattern, cleaned_text, flags=re.I)
    if not match:
        return None

    value = _collapse_ws(match.group(1))
    return value or None





def _extract_first_match(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I | re.S)
        if not match:
            continue
        value = _collapse_ws(unescape(match.group(1)))
        if value:
            return value
    return None

def _extract_description_from_block(block_html: str) -> str | None:
    match = re.search(
        r'<p[^>]*class="[^"]*description[^"]*"[^>]*>\s*<a[^>]*>(.*?)</a>\s*</p>',
        block_html,
        flags=re.I | re.S,
    )
    if not match:
        return None

    description = _clean_html_text(match.group(1))
    return description or None



def _extract_item_id(block_html: str, block_text: str) -> str | None:
    match = re.search(r"Item:\s*(\d{3,})\b", block_text, flags=re.I)
    if match:
        return match.group(1)

    match = re.search(r'itemImg[^>]+src="[^\"]*/(\d{3,})(?:[^\"]*)"', block_html, flags=re.I)
    if match:
        return match.group(1)

    return None



def _extract_item_detail_hint(block_html: str, saved_url: str | None) -> str | None:
    for pattern in HTML_LINK_PATTERNS:
        match = pattern.search(block_html)
        if not match:
            continue

        href = _collapse_ws(unescape(match.group(1)))
        if not href or href in {"#", "/", "."}:
            continue
        if href.lower().startswith(("javascript:", "mailto:")):
            continue

        if saved_url and href.startswith(("/", "./", "../")):
            return urljoin(saved_url, href)
        return href

    return None



def _extract_quantity_and_unit(block_text: str) -> tuple[float | None, str | None, str | None]:
    match = re.search(
        r"Qty\s*(?:\(Weight\))?\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z/]+)?",
        block_text,
        flags=re.I,
    )
    if not match:
        return None, None, None

    raw_quantity = match.group(1)
    raw_unit = (match.group(2) or "").strip().upper() or None

    try:
        quantity = float(raw_quantity)
    except ValueError:
        quantity = None

    if quantity is not None and raw_unit is None:
        raw_unit = "QTY"

    return quantity, raw_unit, raw_quantity



def _extract_line_total(block_text: str) -> float | None:
    match = re.search(r"Total Price\s*:\s*\$\s*([0-9]+(?:\.[0-9]{2})?)", block_text, flags=re.I)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None

    amounts = re.findall(r"\$\s*([0-9]+(?:\.[0-9]{2})?)", block_text)
    if not amounts:
        return None

    try:
        return float(amounts[-1])
    except ValueError:
        return None



def _build_item_record(line_number: int, block_html: str, saved_url: str | None) -> dict | None:
    block_text = _clean_html_text(block_html)
    item_id = _extract_item_id(block_html, block_text)
    if not item_id:
        return None

    description = _extract_description_from_block(block_html)
    detail_hint = _extract_item_detail_hint(block_html, saved_url)
    quantity, unit, raw_quantity = _extract_quantity_and_unit(block_text)
    line_total = _extract_line_total(block_text)

    stable_key = f"BJ'S ITEM {item_id}"
    display_name = description or stable_key

    review_notes = [
        "Imported from BJ's saved HTML.",
        f"Stable item key: {stable_key}.",
        f"Source item code preserved: {item_id}.",
    ]
    if description:
        review_notes.append("Human-readable description was present in the saved HTML.")
    else:
        review_notes.append(
            "Saved HTML did not include a human-readable item description, so a code-based placeholder name was used."
        )
    if detail_hint:
        review_notes.append("A future detail-enrichment step can reuse the captured item detail hint for richer descriptions.")
    if raw_quantity is not None:
        review_notes.append(f"Imported quantity: {raw_quantity}{(' ' + unit) if unit else ''}.")
    if line_total is not None:
        review_notes.append(f"Imported line total: {line_total:.2f}.")

    now = datetime.utcnow().isoformat()
    return {
        "line_number": line_number,
        "item_text_raw": stable_key,
        "item_name_normalized": display_name,
        "quantity": quantity,
        "unit": unit,
        "unit_price": None,
        "line_total": line_total,
        "category": "Uncategorized",
        "category_source_raw": "bjs_html_import",
        "item_confidence": 0.98 if description else 0.9,
        "needs_review": 0,
        "review_notes": " ".join(review_notes),
        "source_item_code": item_id,
        "source_item_detail_hint": detail_hint,
        "created_at": now,
        "updated_at": now,
    }



def parse_bjs_saved_html(html_text: str) -> dict:
    if not looks_like_bjs_saved_html(html_text):
        raise ValueError("HTML does not look like a BJ's saved order-history receipt page.")

    cleaned_text = _clean_html_text(html_text)
    saved_url = _extract_saved_url(html_text)
    order_number = _extract_order_number(saved_url)
    purchase_date = normalize_date_string(
        _extract_first_match(cleaned_text, [
            r"Order Date\s+(.+?)\s+Club Location",
        ]) or ""
    ) or _extract_url_date(saved_url)
    club_location = _extract_first_match(
        cleaned_text,
        [r"Club Location\s+(.+?)\s+Total Items"],
    )
    total_items_text = _extract_first_match(
        cleaned_text,
        [r"Total Items\s+(.+?)\s+Register\s*#"],
    )
    register_number = _extract_first_match(
        cleaned_text,
        [
            r"Register\s*#\s+(.+?)\s+Item Description",
            r"Register\s*#\s+(.+?)\s+Return Policy:",
        ],
    )
    payment_method = _extract_first_match(
        cleaned_text,
        [r"Payment Method\s+(.+?)\s+Item Total"],
    )

    subtotal = _extract_amount("Item Total", cleaned_text)
    tax = _extract_amount("Tax", cleaned_text)
    fees = _extract_amount("Fees", cleaned_text)
    total = _extract_amount("Order Total", cleaned_text)
    savings = _extract_amount("You Saved", cleaned_text)
    if savings is None:
        savings = _extract_amount("Savings", cleaned_text)

    item_blocks = HTML_ITEM_BLOCK_PATTERN.findall(html_text)

    items = []
    for index, block_html in enumerate(item_blocks, start=1):
        item = _build_item_record(index, block_html, saved_url)
        if item is not None:
            items.append(item)

    if not items:
        raise ValueError("No BJ's item rows were found in the saved HTML.")

    description_count = sum(1 for item in items if item.get("item_name_normalized") != item.get("item_text_raw"))
    detail_hint_count = sum(1 for item in items if item.get("source_item_detail_hint"))

    review_notes = [
        f"BJ's structured HTML import v1 parsed {len(items)} item(s) deterministically from saved order-history HTML.",
        "Item codes, quantities, and totals were imported locally without OCR.",
        "This keeps the evidence-governance loop intact while reducing OCR noise and AI dependence for structured sources.",
    ]
    if order_number:
        review_notes.append(f"Order number: {order_number}.")
    if club_location:
        review_notes.append(f"Club location: {club_location}.")
    if register_number:
        review_notes.append(f"Register: {register_number}.")
    if payment_method:
        review_notes.append(f"Payment method: {payment_method}.")
    if total_items_text:
        review_notes.append(f"Reported total items: {total_items_text}.")
    if savings is not None:
        review_notes.append(f"Reported savings: {savings:.2f}.")
    if fees is not None and fees > 0:
        review_notes.append(f"Reported fees: {fees:.2f}.")
    if description_count:
        review_notes.append(f"Descriptions recovered directly from saved HTML: {description_count}.")
    else:
        review_notes.append(
            "Saved HTML did not expose product descriptions in this sample, so stable item-code placeholder names were used."
        )
    review_notes.append(f"Stable source item codes preserved: {len(items)}.")
    if detail_hint_count:
        review_notes.append(f"Future detail-enrichment hints detected: {detail_hint_count}.")
    else:
        review_notes.append(
            "No explicit item-detail links were present in this saved page, so later enrichment will rely on separately captured detail pages or merchant exports."
        )

    receipt_text_lines = [
        BJS_MERCHANT_NAME,
        f"Order Number: {order_number or '-'}",
        f"Order Date: {purchase_date or '-'}",
        f"Club Location: {club_location or '-'}",
        f"Register: {register_number or '-'}",
        f"Item Total: {subtotal:.2f}" if subtotal is not None else "Item Total: -",
        f"Tax: {tax:.2f}" if tax is not None else "Tax: -",
        f"Fees: {fees:.2f}" if fees is not None else "Fees: -",
        f"Order Total: {total:.2f}" if total is not None else "Order Total: -",
        f"Savings: {savings:.2f}" if savings is not None else "Savings: -",
        "Line Items:",
    ]
    for item in items:
        line_parts = [item["item_text_raw"]]
        if item.get("source_item_code"):
            line_parts.append(f"code={item['source_item_code']}")
        if item.get("item_name_normalized") and item["item_name_normalized"] != item["item_text_raw"]:
            line_parts.append(f"desc={item['item_name_normalized']}")
        if item.get("quantity") is not None:
            if item.get("unit"):
                line_parts.append(f"qty={item['quantity']} {item['unit']}")
            else:
                line_parts.append(f"qty={item['quantity']}")
        if item.get("line_total") is not None:
            line_parts.append(f"line_total={item['line_total']:.2f}")
        if item.get("source_item_detail_hint"):
            line_parts.append("detail_hint=captured")
        receipt_text_lines.append(" | ".join(line_parts))

    now = datetime.utcnow().isoformat()
    return {
        "receipt": {
            "merchant_raw": BJS_MERCHANT_NAME,
            "merchant_canonical": BJS_MERCHANT_NAME,
            "purchase_date": purchase_date,
            "purchase_time": None,
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
            "currency": "USD",
            "ocr_text_raw": "\n".join(receipt_text_lines),
            "receipt_confidence": 0.99,
            "status": "needs_review",
            "review_notes": " ".join(review_notes),
            "updated_at": now,
        },
        "items": items,
        "source_metadata": {
            "order_number": order_number,
            "club_location": club_location,
            "register_number": register_number,
            "payment_method": payment_method,
            "reported_item_count": total_items_text,
            "savings": savings,
            "fees": fees,
            "saved_url": saved_url,
            "description_count": description_count,
            "detail_hint_count": detail_hint_count,
        },
    }
