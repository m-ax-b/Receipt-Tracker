import re
from datetime import datetime


KNOWN_MERCHANTS = {
    "MCDONALD'S": "McDonald's",
    "MCDONALDS": "McDonald's",
    "TRADER JOES": "Trader Joe's",
    "TRADER JOE'S": "Trader Joe's",
    "WALMART": "Walmart",
    "COSTCO": "Costco",
    "TARGET": "Target",
    "WHOLE FOODS": "Whole Foods",
    "ALDI": "ALDI",
    "KROGER": "Kroger",
    "SAFEWAY": "Safeway",
    "PUBLIX": "Publix",
    "WEGMANS": "Wegmans",
    "MEIJER": "Meijer",
    "BIG Y": "Big Y",
    "STOP & SHOP": "Stop & Shop",
    "SHOPRITE": "ShopRite",
    "TRADER JOE": "Trader Joe's",
}

KNOWN_MERCHANT_KEYS = {}
for raw_name, canonical_name in KNOWN_MERCHANTS.items():
    key = re.sub(r"[^A-Za-z0-9& ]+", "", raw_name.upper())
    key = re.sub(r"\s{2,}", " ", key).strip()
    KNOWN_MERCHANT_KEYS[key] = canonical_name


FOOTER_WORDS = [
    "SURVEY",
    "FEEDBACK",
    "VISIT",
    "TELL US",
    "TELLUS",
    "CUSTOMER",
    "SATISFACTION",
    "THANK YOU",
    "THANKYOU",
    "WWW.",
    "HTTP",
    ".COM",
    ".NET",
    "DOWNLOAD OUR APP",
    "RATE YOUR VISIT",
]

OPERATIONAL_WORDS = [
    "ORDER",
    "ORD",
    "LANE",
    "CASHIER",
    "TRANS",
    "TRANSACTION",
    "STORE #",
    "STORE NO",
    "STORE NUMBER",
    "STORE:",
    "CHK",
    "CHECKOUT",
    "REG",
    "REGISTER",
    "TERMINAL",
    "TICKET",
    "INVOICE",
    "AUTH",
    "APPROVAL",
    "TEL#",
    "PHONE",
    "ACCOUNT#",
]

MONEY_WORDS = [
    "TOTAL",
    "SUBTOTAL",
    "TAX",
    "BALANCE",
    "CHANGE",
    "AMOUNT DUE",
    "CASH",
    "CARD",
    "DEBIT",
    "CREDIT",
]

MONTH_NAME_PATTERN = r"(?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"


def normalize_ocr_text_for_matching(text: str) -> str:
    text = text.replace("’", "'").replace("`", "'").replace("‘", "'")
    text = re.sub(r"([A-Za-z])'\s+([A-Za-z])", r"\1'\2", text)
    text = re.sub(r"[^A-Za-z0-9&' ]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text.upper()


def normalize_merchant_lookup(text: str) -> str:
    text = normalize_ocr_text_for_matching(text)
    text = text.replace("'", "")
    text = re.sub(r"[^A-Z0-9& ]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def detect_known_merchant(text: str) -> str | None:
    lookup = normalize_merchant_lookup(text)
    for raw_key, canonical in KNOWN_MERCHANT_KEYS.items():
        if raw_key in lookup:
            return canonical
    return None


def clean_merchant_candidate_text(text: str) -> str:
    text = re.sub(r"\s{2,}", " ", text.strip())
    text = text.replace("’", "'").replace("`", "'").replace("‘", "'")

    canonical = detect_known_merchant(text)
    if canonical:
        return canonical

    text = re.sub(r"\s+#?\d+\s*$", "", text).strip()
    return text.title()


def clean_merchant_name(text: str) -> str:
    canonical = detect_known_merchant(text)
    if canonical:
        return canonical
    return clean_merchant_candidate_text(text)


def normalize_date_string(raw_date: str) -> str | None:
    raw_date = raw_date.strip()

    formats = [
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y-%m-%d",
        "%m-%d-%Y",
        "%m-%d-%y",
        "%Y/%m/%d",
        "%B %d, %Y",
        "%A, %B %d, %Y",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(raw_date, fmt)
            year = dt.year
            if year < 2000 or year > 2100:
                continue
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


def extract_purchase_date(ocr_text: str) -> str | None:
    date_patterns = [
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
        r"\b\d{1,2}/\d{1,2}/\d{2}\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b\d{1,2}-\d{1,2}-\d{4}\b",
        r"\b\d{1,2}-\d{1,2}-\d{2}\b",
        r"\b\d{4}/\d{2}/\d{2}\b",
        rf"\b{MONTH_NAME_PATTERN}\s+\d{{1,2}},\s+\d{{4}}\b",
        rf"\b(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),\s+{MONTH_NAME_PATTERN}\s+\d{{1,2}},\s+\d{{4}}\b",
    ]

    uppercase_text = ocr_text.upper()
    matches = []
    for pattern in date_patterns:
        matches.extend(re.findall(pattern, uppercase_text))

    for value in matches:
        normalized = normalize_date_string(value.title())
        if normalized:
            return normalized

    return None


def normalize_amount_text(text: str) -> str:
    text = text.replace(",", ".")
    text = re.sub(r"(\d)\s*[\.]\s*(\d{2})\b", r"\1.\2", text)
    text = re.sub(r"\$\s+", "$", text)
    return text


def _extract_amounts_from_line(line: str) -> list[float]:
    line = normalize_amount_text(line)
    matches = re.findall(r"(?:\$\s*)?([0-9]+\.[0-9]{2})\b", line)
    amounts = []

    for match in matches:
        try:
            amounts.append(float(match))
        except ValueError:
            pass

    return amounts


def _extract_amount_from_line(line: str) -> float | None:
    amounts = _extract_amounts_from_line(line)
    if not amounts:
        return None
    return amounts[-1]


def extract_total(ocr_text: str) -> float | None:
    lines = [line.strip() for line in ocr_text.splitlines() if line.strip()]

    priority_labels = [
        "GRAND TOTAL",
        "AMOUNT DUE",
        "ORDER TOTAL",
        "BALANCE DUE",
        "TAKE OUT TOTAL",
        "TAKE-OUT TOTAL",
        "TOTAL",
    ]

    best_candidate = None
    best_score = -999

    for idx, line in enumerate(lines):
        normalized_line = normalize_amount_text(line)
        upper = normalize_ocr_text_for_matching(normalized_line)

        if "SUBTOTAL" in upper:
            continue
        if "TAX" in upper and "TOTAL" not in upper:
            continue
        if "BOTTLE DEPOSIT" in upper:
            continue

        amount = _extract_amount_from_line(normalized_line)

        score = 0

        for label in priority_labels:
            if label in upper:
                score += 110 if label != "TOTAL" else 80

        if any(word in upper for word in ["CHANGE", "CASHLESS", "AUTHORIZATION", "ACCOUNT", "CARD ISSUER"]):
            score -= 80

        if amount is None and idx + 1 < len(lines):
            next_amount = _extract_amount_from_line(lines[idx + 1])
            if next_amount is not None and any(label in upper for label in priority_labels):
                amount = next_amount
                score += 70

        if amount is None:
            continue

        if amount <= 0:
            score -= 100

        if score > best_score:
            best_score = score
            best_candidate = amount

    if best_candidate is not None and best_score > 0:
        return best_candidate

    amounts = []
    for line in lines:
        upper = normalize_ocr_text_for_matching(line)
        if any(word in upper for word in ["CHANGE", "BOTTLE DEPOSIT"]):
            continue
        amounts.extend(_extract_amounts_from_line(line))

    if amounts:
        return max(amounts)

    return None


def merchant_score(line: str, line_index: int, total_lines: int) -> int:
    upper = normalize_ocr_text_for_matching(line)
    lookup = normalize_merchant_lookup(line)
    score = 0

    for raw_key in KNOWN_MERCHANT_KEYS:
        if raw_key in lookup:
            score += 150

    if line_index <= 12:
        score += 20
    elif line_index <= 20:
        score += 10

    if total_lines > 0 and line_index >= int(total_lines * 0.75):
        score -= 50

    if 4 <= len(line) <= 48:
        score += 12
    if re.search(r"[A-Z]", line):
        score += 5
    if line.isupper():
        score += 5

    if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", line):
        score -= 50
    if re.search(r"\$?\s*\d+\s*[\.]\s*\d{2}\b", line):
        score -= 35
    if re.fullmatch(r"[#A-Z0-9\- ]{1,18}", upper) and any(ch.isdigit() for ch in upper):
        score -= 20

    if re.search(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", upper):
        score -= 80
    if re.search(r"\b\d+\s+[A-Z0-9 .'-]+\b(?:ST|STREET|RD|ROAD|AVE|AVENUE|BLVD|DR|DRIVE|LN|LANE|HWY)\b", upper):
        score -= 70

    for word in MONEY_WORDS:
        if word in upper:
            score -= 60

    for word in OPERATIONAL_WORDS:
        if word in upper:
            score -= 25

    for word in FOOTER_WORDS:
        if word in upper:
            score -= 90

    if "MEMBERSHIP" in upper or "REWARD" in upper:
        score -= 35

    if any(raw_key in lookup for raw_key in KNOWN_MERCHANT_KEYS) and any(word in upper for word in FOOTER_WORDS):
        score -= 80

    return score


def extract_merchant_candidates(ocr_text: str) -> list[tuple[int, int, str]]:
    lines = [line.strip() for line in ocr_text.splitlines() if line.strip()]
    total_lines = len(lines)
    candidates = []

    for idx, line in enumerate(lines[:30], start=1):
        score = merchant_score(line, idx, total_lines)
        candidates.append((score, idx, line))

    candidates.sort(reverse=True, key=lambda x: (x[0], -x[1]))
    return candidates


def extract_merchant(ocr_text: str) -> tuple[str | None, str | None]:
    candidates = extract_merchant_candidates(ocr_text)
    if not candidates:
        return None, None

    best_score, _, best_line = candidates[0]
    if best_score < 0:
        return None, None

    return clean_merchant_candidate_text(best_line), clean_merchant_name(best_line)
