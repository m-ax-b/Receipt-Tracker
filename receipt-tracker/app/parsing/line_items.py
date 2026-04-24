import re
from datetime import datetime


CATEGORY_BY_SECTION = {
    "GROCERY": "Pantry",
    "MEAT": "Meat & Seafood",
    "PRODUCE": "Produce",
    "DAIRY": "Dairy",
    "FROZEN": "Frozen",
    "BAKERY": "Bakery",
    "DELI": "Prepared Foods",
    "SEAFOOD": "Meat & Seafood",
    "HOUSEHOLD": "Household",
    "PERSONAL CARE": "Personal Care",
    "BEVERAGES": "Beverages",
}

STOP_WORDS = [
    "SUBTOTAL",
    "SUMMARY",
    "TOTAL BEFORE TAX",
    "BALANCE DUE",
    "TAKE-OUT TOTAL",
    "TAKE OUT TOTAL",
    "CHANGE DUE",
    "CASHLESS",
    "AUTHORIZATION",
    "ACCOUNT#",
    "CONTACTLESS",
    "MER#",
    "AID:",
    "SEQ#",
    "TRANSACTION AMOUNT",
    "CARD ISSUER",
    "WIN A $",
    "SHOPPING SPREE",
]

SKIP_WORDS = [
    "CREDIT CARD PAYMENT INFORMATION",
    "CARD:",
    "EXP DATE:",
    "REF #",
    "AUTH #",
    "THANK YOU",
    "TEL#",
    "VALIDATION CODE",
    "SURVEY CODE",
    "EXPIRES",
    "VISITING WWW",
    "HTTP",
    "VISIT",
    "PLEASE CALL",
    "REWARD MEMBER",
    "POINTS EARNED",
    "CASHIER:",
    "STORE:",
    "TOTAL NUMBER OF ITEMS PURCHASED",
    "BOTTLE DEPOSIT",
    "TOTAL BOTTLE DEPOSITS",
    "COMPLETE OUR SURVEY",
    "TELL US ABOUT THIS VISIT",
    "TO ENTER A MONTHLY DRAWING",
    "HTTPS:",
    ".COM",
    "PTS",
]

CATEGORY_HINTS = {
    "COKE": "Beverages",
    "COLA": "Beverages",
    "SODA": "Beverages",
    "WATER": "Beverages",
    "JUICE": "Beverages",
    "SHAKE": "Beverages",
    "MILK": "Dairy",
    "CHEESE": "Dairy",
    "YOGURT": "Dairy",
    "BANANA": "Produce",
    "APPLE": "Produce",
    "LETTUCE": "Produce",
    "MUSH": "Produce",
    "RIB EYE": "Meat & Seafood",
    "STEAK": "Meat & Seafood",
    "CHICKEN": "Meat & Seafood",
    "SAUCE": "Pantry",
    "NUGGET": "Prepared Foods",
    "MCDOUBLE": "Prepared Foods",
    "FRIES": "Prepared Foods",
}

MODIFIER_PREFIXES = [
    "NO ",
    "ADD ",
    "EXTRA ",
    "LIGHT ",
    "EASY ",
    "LESS ",
    "ONLY ",
    "W/O ",
    "WITH ",
]

MODIFIER_EXACT = {
    "NO PICKLE",
    "NO ONION",
    "NO LETTUCE",
    "NO TOMATO",
    "NO CHEESE",
}

RECOVERY_SKIP_WORDS = [
    "SUBTOTAL",
    "TOTAL",
    "TAX",
    "CHANGE",
    "BALANCE",
    "CARD",
    "CASH",
    "AUTHORIZATION",
    "ACCOUNT",
    "TRANSACTION",
    "BOTTLE DEPOSIT",
    "MER#",
    "AID:",
    "SEQ#",
    "TEL#",
    "SURVEY",
]


def normalize_amount_text(text: str) -> str:
    text = text.replace(",", ".")
    text = text.replace("§", "S")
    text = re.sub(r"(\d)\s*[\.]\s*(\d{2})\b", r"\1.\2", text)
    text = re.sub(r"\$\s+", "$", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def normalize_line_for_matching(text: str) -> str:
    text = normalize_amount_text(text)
    text = text.replace("’", "'").replace("`", "'").replace("‘", "'")
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip().upper()


def normalize_match_text(text: str) -> str:
    text = normalize_line_for_matching(text)
    text = re.sub(r"(?:\$\s*)?\b\d+\.\d{2}\b", " ", text)
    text = re.sub(r"^\s*\d+\s+", " ", text)
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def token_set(text: str) -> set[str]:
    drop_tokens = {"S", "M", "L", "XL"}
    return {
        token
        for token in normalize_match_text(text).split()
        if token not in drop_tokens and len(token) >= 2
    }


def should_stop_parsing(upper_line: str) -> bool:
    return any(word in upper_line for word in STOP_WORDS)


def should_skip_line(upper_line: str) -> bool:
    return any(word in upper_line for word in SKIP_WORDS)


def looks_like_header_row(upper_line: str) -> bool:
    return upper_line in {
        "PRICE",
        "WITHOUT YOU",
        "CARD ITEM DESCRIPTION PAY",
        "ITEM DESCRIPTION",
        "YOU PAY",
    }


def looks_like_modifier_line(upper_line: str) -> bool:
    if upper_line in MODIFIER_EXACT:
        return True

    if any(upper_line.startswith(prefix) for prefix in MODIFIER_PREFIXES):
        return True

    return False


def looks_like_item_section_start(upper_line: str) -> bool:
    if "ORDER" in upper_line and re.search(r"\b\d+\b", upper_line):
        return True
    if "ITEM DESCRIPTION" in upper_line:
        return True
    return False


def looks_like_section_header(upper_line: str) -> bool:
    if upper_line in CATEGORY_BY_SECTION:
        return True

    if looks_like_header_row(upper_line):
        return False

    if looks_like_modifier_line(upper_line):
        return False

    if should_skip_line(upper_line) or should_stop_parsing(upper_line):
        return False

    if re.search(r"[\d$:/.#-]", upper_line):
        return False

    if not re.fullmatch(r"[A-Z& ]{3,30}", upper_line):
        return False

    word_count = len(upper_line.split())
    if word_count < 1 or word_count > 3:
        return False

    return False


def choose_category(current_category: str, description: str, merchant_canonical: str | None) -> str:
    upper_desc = normalize_line_for_matching(description)

    for hint, category in CATEGORY_HINTS.items():
        if hint in upper_desc:
            return category

    if current_category != "Uncategorized":
        return current_category

    if merchant_canonical and "MCDONALD" in merchant_canonical.upper():
        if any(word in upper_desc for word in ["COKE", "SHAKE", "COFFEE", "TEA"]):
            return "Beverages"
        return "Prepared Foods"

    return "Uncategorized"


def clean_item_text_raw(text: str) -> str:
    text = normalize_amount_text(text)
    text = text.replace("S$", "S ")
    text = text.replace("’", "'").replace("`", "'").replace("‘", "'")
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -:")


def strip_trailing_ocr_price_fragment(text: str) -> str:
    text = re.sub(r"\s+\$?\.\d{2}\b", "", text)
    text = re.sub(r"\s+\$?\d+\.\d{2}\b$", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def apply_name_cleanup_rules(text: str) -> str:
    text = re.sub(r"\bM1\]\b", "M1", text)
    text = re.sub(r"\bND\b", "", text)
    text = re.sub(r"\bWHL\b", "Whole", text, flags=re.IGNORECASE)
    text = re.sub(r"\bBBY\b", "Baby", text, flags=re.IGNORECASE)
    text = re.sub(r"\bBLLA\b", "Bella", text, flags=re.IGNORECASE)
    text = re.sub(r"\bMSH\b", "Mushrooms", text, flags=re.IGNORECASE)
    text = re.sub(r"\bBNLS\b", "Boneless", text, flags=re.IGNORECASE)
    text = re.sub(r"\bYLWBRD\b", "Yellow Bird", text, flags=re.IGNORECASE)
    text = re.sub(r"\bAUJUS\b", "Au Jus", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def apply_title_case_corrections(text: str) -> str:
    replacements = {
        "Mcdouble": "McDouble",
        "Mcnuggets": "McNuggets",
        "Coke Zero": "Coke Zero",
        "Au Jus": "Au Jus",
        "S&S Sauce": "S&S Sauce",
    }

    for source, target in replacements.items():
        text = re.sub(rf"\b{re.escape(source)}\b", target, text)

    return text


def normalize_item_name(text: str) -> str:
    text = clean_item_text_raw(text)
    text = re.sub(r"^\$?\d+(?:\.\d{2})?\s+", "", text)
    text = strip_trailing_ocr_price_fragment(text)
    text = apply_name_cleanup_rules(text)
    text = text.title()
    text = apply_title_case_corrections(text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def score_row_match(item_text: str, row_text: str) -> int:
    item_norm = normalize_match_text(item_text)
    row_norm = normalize_match_text(row_text)

    if not item_norm or not row_norm:
        return -999

    score = 0

    if item_norm in row_norm or row_norm in item_norm:
        score += 120

    item_tokens = token_set(item_text)
    row_tokens = token_set(row_text)
    common_tokens = item_tokens & row_tokens

    score += len(common_tokens) * 20

    if item_tokens and item_tokens.issubset(row_tokens):
        score += 80

    if not common_tokens:
        score -= 100

    return score


def usable_recovery_row(row: dict, receipt_total: float | None) -> bool:
    amount = row.get("amount")
    text = row.get("text", "")

    if amount is None or amount <= 0:
        return False

    if receipt_total is not None and amount > receipt_total + 0.01:
        return False

    upper_text = normalize_line_for_matching(text)

    if any(word in upper_text for word in RECOVERY_SKIP_WORDS):
        return False

    return True


def clean_recovery_row_text(text: str) -> str:
    text = normalize_amount_text(text)
    text = text.strip()
    text = re.sub(r"^[|:;,\- ]+", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text


def append_note(existing: str | None, extra: str) -> str:
    if not existing:
        return extra

    if extra in existing:
        return existing

    return f"{existing} {extra}"


def recover_missing_line_totals(
    items: list[dict],
    structured_ocr_lines: list[dict],
    receipt_total: float | None = None,
) -> list[dict]:
    usable_rows = [
        row for row in structured_ocr_lines if usable_recovery_row(row, receipt_total)
    ]

    used_row_indexes = set()

    for item in items:
        if item.get("line_total") is None:
            continue

        best_idx = None
        best_score = -999

        for idx, row in enumerate(usable_rows):
            if idx in used_row_indexes:
                continue

            if abs(float(row["amount"]) - float(item["line_total"])) > 0.01:
                continue

            score = score_row_match(item.get("item_text_raw", ""), row.get("text", ""))
            if score > best_score:
                best_idx = idx
                best_score = score

        if best_idx is not None and best_score >= 80:
            used_row_indexes.add(best_idx)

    for item in items:
        if item.get("line_total") is not None:
            continue

        best_idx = None
        best_score = -999

        for idx, row in enumerate(usable_rows):
            if idx in used_row_indexes:
                continue

            score = score_row_match(item.get("item_text_raw", ""), row.get("text", ""))
            if score > best_score:
                best_idx = idx
                best_score = score

        if best_idx is None or best_score < 120:
            continue

        matched_row = usable_rows[best_idx]
        cleaned_row_text = clean_recovery_row_text(matched_row["text"])

        item["line_total"] = float(matched_row["amount"])
        item["needs_review"] = 1
        item["item_confidence"] = max(float(item.get("item_confidence") or 0), 0.74)
        item["review_notes"] = append_note(
            item.get("review_notes"),
            f"Recovered line total from structured OCR row: {cleaned_row_text}",
        )

        used_row_indexes.add(best_idx)

    return items


def build_item(
    line_number: int,
    raw_text: str,
    line_total: float | None,
    category: str,
    confidence: float,
    needs_review: int,
    review_notes: str | None,
    now: str,
    quantity: float | None = None,
    category_source_raw: str | None = None,
) -> dict:
    return {
        "line_number": line_number,
        "item_text_raw": clean_item_text_raw(raw_text),
        "item_name_normalized": normalize_item_name(raw_text),
        "quantity": quantity,
        "unit": None,
        "unit_price": None,
        "line_total": line_total,
        "category": category,
        "category_source_raw": category_source_raw,
        "item_confidence": confidence,
        "needs_review": needs_review,
        "review_notes": review_notes,
        "created_at": now,
        "updated_at": now,
    }


def extract_line_items(
    ocr_text: str,
    merchant_canonical: str | None = None,
    structured_ocr_lines: list[dict] | None = None,
    receipt_total: float | None = None,
) -> list[dict]:
    now = datetime.utcnow().isoformat()
    lines = [line.strip() for line in ocr_text.splitlines() if line.strip()]

    items: list[dict] = []
    current_category = "Uncategorized"
    current_section_raw = None
    in_item_section = False
    line_number = 1

    for original_line in lines:
        line = normalize_amount_text(original_line)
        upper_line = normalize_line_for_matching(line)

        if should_stop_parsing(upper_line):
            if in_item_section:
                break
            continue

        if looks_like_header_row(upper_line):
            continue

        if looks_like_section_header(upper_line):
            current_section_raw = original_line.strip()
            current_category = CATEGORY_BY_SECTION.get(upper_line, "Uncategorized")
            in_item_section = True
            continue

        if looks_like_item_section_start(upper_line):
            in_item_section = True
            continue

        if should_skip_line(upper_line):
            continue

        if not in_item_section:
            continue

        if re.fullmatch(r"[0-9.:/ AMP-]+", upper_line):
            continue

        grocery_match = re.match(
            r"^(?:\$?([0-9]+\.[0-9]{2})|([0-9]{3,4}))\s+(.+?)(?:\s+\$?([0-9]+\.[0-9]{2}))?$",
            line,
        )
        if grocery_match:
            leading_amount = grocery_match.group(1)
            description = grocery_match.group(3).strip()
            trailing_amount = grocery_match.group(4)

            if "BOTTLE DEPOSIT" in normalize_line_for_matching(description):
                continue

            if trailing_amount:
                line_total = float(trailing_amount)
                confidence = 0.90
                needs_review = 0
                review_notes = None
            elif leading_amount:
                line_total = float(leading_amount)
                confidence = 0.78
                needs_review = 1
                review_notes = "Used leading amount only. Review this OCR-derived item."
            else:
                line_total = None
                confidence = 0.60
                needs_review = 1
                review_notes = "Damaged leading price detected. Review this OCR-derived item."

            category = choose_category(current_category, description, merchant_canonical)
            items.append(
                build_item(
                    line_number=line_number,
                    raw_text=description,
                    line_total=line_total,
                    category=category,
                    category_source_raw=current_section_raw,
                    confidence=confidence,
                    needs_review=needs_review,
                    review_notes=review_notes,
                    now=now,
                )
            )
            line_number += 1
            continue

        restaurant_match = re.match(
            r"^(\d+)\s+(?:\$?([0-9]+(?:\.[0-9]{2})?)\s+)?(.+?)(?:\s+\$?([0-9]+\.[0-9]{2}))?$",
            line,
        )
        if restaurant_match:
            quantity = float(restaurant_match.group(1))
            leading_amount = restaurant_match.group(2)
            description = restaurant_match.group(3).strip()
            trailing_amount = restaurant_match.group(4)

            if quantity > 20:
                continue

            upper_description = normalize_line_for_matching(description)
            if upper_description.startswith("NO "):
                continue
            if should_skip_line(upper_description):
                continue

            if trailing_amount:
                line_total = float(trailing_amount)
                confidence = 0.90
                needs_review = 0
                review_notes = None
            elif leading_amount and quantity == 1 and ("." in leading_amount or f"${leading_amount}" in line):
                line_total = float(leading_amount)
                confidence = 0.77
                needs_review = 1
                review_notes = "Used leading amount only. Review this OCR-derived item."
            else:
                if leading_amount and quantity == 1 and not ("." in leading_amount or f"${leading_amount}" in line):
                    description = f"{leading_amount} {description}".strip()
                line_total = None
                confidence = 0.66
                needs_review = 1
                review_notes = "Price not found on item line. Review this OCR-derived item."

            category = choose_category(current_category, description, merchant_canonical)
            items.append(
                build_item(
                    line_number=line_number,
                    raw_text=description,
                    line_total=line_total,
                    category=category,
                    category_source_raw=current_section_raw,
                    confidence=confidence,
                    needs_review=needs_review,
                    review_notes=review_notes,
                    now=now,
                    quantity=quantity,
                )
            )
            line_number += 1
            continue

    if structured_ocr_lines:
        items = recover_missing_line_totals(
            items,
            structured_ocr_lines,
            receipt_total=receipt_total,
        )

    return items
