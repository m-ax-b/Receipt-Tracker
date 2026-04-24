import re

from PIL import Image, ImageOps
import pytesseract


def normalize_amount_text(text: str) -> str:
    text = text.replace(",", ".")
    text = text.replace("§", "S")
    text = re.sub(r"(\d)\s*[\.]\s*(\d{2})\b", r"\1.\2", text)
    text = re.sub(r"\$\s+", "$", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _load_image(image_path: str) -> Image.Image:
    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image)
    return image


def _preprocess_for_general_ocr(image: Image.Image) -> Image.Image:
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image)
    return image


def _preprocess_for_structured_lines(image: Image.Image) -> Image.Image:
    image = _preprocess_for_general_ocr(image)
    image = image.resize((image.width * 2, image.height * 2))
    return image


def _extract_amount_from_text(text: str) -> float | None:
    text = normalize_amount_text(text)
    matches = re.findall(r"(?:\$\s*)?([0-9]+\.[0-9]{2})\b", text)
    if not matches:
        return None

    try:
        return float(matches[-1])
    except ValueError:
        return None


def extract_text_from_image(image_path: str) -> str:
    """
    Primary OCR text used by the receipt parser.
    """
    image = _load_image(image_path)
    image = _preprocess_for_general_ocr(image)

    text = pytesseract.image_to_string(image, lang="eng")

    if not text or not text.strip():
        return "OCR produced no readable text."

    return text.strip()


def extract_structured_ocr_lines(image_path: str) -> list[dict]:
    """
    Secondary OCR pass using Tesseract line data.

    Returns ordered OCR rows like:
    [
        {"row_index": 1, "text": "1 S$ Chocolate Shake 4.59", "amount": 4.59},
        ...
    ]

    This is used as a conservative recovery source for line-item prices
    that were missed by the plain text parser.
    """
    image = _load_image(image_path)
    image = _preprocess_for_structured_lines(image)

    data = pytesseract.image_to_data(
        image,
        lang="eng",
        config="--psm 6",
        output_type=pytesseract.Output.DICT,
    )

    rows: dict[tuple[int, int, int], list[dict]] = {}
    count = len(data["text"])

    for i in range(count):
        text = str(data["text"][i] or "").strip()
        if not text:
            continue

        key = (
            int(data["block_num"][i]),
            int(data["par_num"][i]),
            int(data["line_num"][i]),
        )

        rows.setdefault(key, []).append(
            {
                "left": int(data["left"][i]),
                "text": text,
            }
        )

    structured_rows = []

    for key in sorted(rows):
        parts = sorted(rows[key], key=lambda x: x["left"])
        row_text = " ".join(part["text"] for part in parts)
        row_text = normalize_amount_text(row_text)

        if not row_text:
            continue

        structured_rows.append(
            {
                "row_index": len(structured_rows) + 1,
                "text": row_text,
                "amount": _extract_amount_from_text(row_text),
            }
        )

    return structured_rows
