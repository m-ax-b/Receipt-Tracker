from __future__ import annotations

from html import unescape
from pathlib import Path
import mimetypes
import re

from app.parsing.bjs_html import looks_like_bjs_saved_html

SUPPORTED_IMAGE_MEDIA_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/tiff",
}

MEDIA_TYPE_TO_PREFERRED_EXTENSION = {
    "image/jpeg": ".jpeg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "application/pdf": ".pdf",
    "text/html": ".html",
}


def _decode_probe_text(file_bytes: bytes, limit: int = 250000) -> str:
    if not file_bytes:
        return ""
    return file_bytes[:limit].decode("utf-8", errors="ignore")


def _sniff_media_type(file_bytes: bytes, filename: str | None) -> str | None:
    header = file_bytes[:64]

    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if header.startswith(b"RIFF") and len(file_bytes) >= 12 and file_bytes[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"%PDF-"):
        return "application/pdf"

    probe_text = _decode_probe_text(file_bytes).lower()
    if any(token in probe_text for token in ("<!doctype html", "<html", "<head", "<body", "saved from url")):
        return "text/html"

    guessed, _ = mimetypes.guess_type((filename or "").strip())
    return guessed or None


def _extract_html_title(html_text: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text or "", flags=re.I | re.S)
    if not match:
        return None
    title = re.sub(r"\s+", " ", unescape(match.group(1))).strip()
    return title or None


def _extract_saved_source_url(html_text: str) -> str | None:
    match = re.search(r"saved from url=.*?(https?://[^\s>]+)", html_text or "", flags=re.I)
    if not match:
        return None
    return match.group(1).strip()


def _normalized_filename(filename: str | None, media_type: str) -> str:
    original = Path((filename or "upload").strip() or "upload")
    suffix = original.suffix.lower()
    preferred_suffix = MEDIA_TYPE_TO_PREFERRED_EXTENSION.get(media_type, "")

    if suffix in {".jpg", ".jpeg", ".jpe", ".jfif"} and media_type == "image/jpeg":
        return original.name or f"upload{suffix}"

    if suffix and mimetypes.guess_type(original.name)[0] == media_type:
        return original.name

    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", original.stem or "upload").strip("._-") or "upload"
    if not preferred_suffix:
        preferred_suffix = suffix or ".bin"
    return f"{stem}{preferred_suffix}"


def _base_manual_source(*, page_title: str | None = None, source_url: str | None = None) -> dict:
    return {
        "type": "manual_upload",
        "name": "manual",
        "connector_skill": None,
        "acquisition_mode": "human_triggered",
        "operator_present": True,
        "account_label": None,
        "source_url": source_url,
        "page_title": page_title,
    }


def detect_upload_artifact(file_bytes: bytes, filename: str | None) -> dict:
    media_type = _sniff_media_type(file_bytes, filename)
    original_filename = (filename or "").strip() or "upload"

    if not media_type:
        return {
            "is_supported": False,
            "media_type": None,
            "original_filename": original_filename,
            "normalized_filename": original_filename,
            "source": _base_manual_source(),
            "parser_hints": {
                "merchant_hint": None,
                "artifact_kind": "unknown",
                "needs_ocr": False,
                "likely_language": "en",
                "expected_layout": "unknown",
            },
            "detection_label": "Unsupported artifact",
            "detection_detail": "The file could not be recognized as an image, PDF, or saved HTML receipt page.",
        }

    normalized_filename = _normalized_filename(original_filename, media_type)

    if media_type.startswith("image/"):
        return {
            "is_supported": media_type in SUPPORTED_IMAGE_MEDIA_TYPES,
            "media_type": media_type,
            "original_filename": original_filename,
            "normalized_filename": normalized_filename,
            "source": _base_manual_source(),
            "parser_hints": {
                "merchant_hint": None,
                "artifact_kind": "image_receipt",
                "needs_ocr": True,
                "likely_language": "en",
                "expected_layout": "ocr_receipt_image",
            },
            "detection_label": "Receipt image",
            "detection_detail": "Manual upload routed to the image OCR intake path.",
        }

    if media_type == "application/pdf":
        return {
            "is_supported": True,
            "media_type": media_type,
            "original_filename": original_filename,
            "normalized_filename": normalized_filename,
            "source": _base_manual_source(),
            "parser_hints": {
                "merchant_hint": None,
                "artifact_kind": "pdf_receipt",
                "needs_ocr": True,
                "likely_language": "en",
                "expected_layout": "pdf_receipt",
            },
            "detection_label": "PDF receipt artifact",
            "detection_detail": "Manual upload routed to the PDF artifact path.",
        }

    if media_type == "text/html":
        html_text = _decode_probe_text(file_bytes)
        page_title = _extract_html_title(html_text)
        source_url = _extract_saved_source_url(html_text)

        if looks_like_bjs_saved_html(html_text):
            return {
                "is_supported": True,
                "media_type": media_type,
                "original_filename": original_filename,
                "normalized_filename": normalized_filename,
                "source": _base_manual_source(
                    page_title=page_title or "BJ's saved order-history receipt",
                    source_url=source_url,
                ),
                "parser_hints": {
                    "merchant_hint": "BJ's Wholesale Club",
                    "artifact_kind": "html_receipt_page",
                    "needs_ocr": False,
                    "likely_language": "en",
                    "expected_layout": "bjs_saved_html",
                },
                "detection_label": "BJ's saved HTML receipt page",
                "detection_detail": "Manual upload routed to the BJ's structured HTML adapter.",
            }

        return {
            "is_supported": True,
            "media_type": media_type,
            "original_filename": original_filename,
            "normalized_filename": normalized_filename,
            "source": _base_manual_source(page_title=page_title, source_url=source_url),
            "parser_hints": {
                "merchant_hint": None,
                "artifact_kind": "html_receipt_page",
                "needs_ocr": False,
                "likely_language": "en",
                "expected_layout": "generic_saved_html",
            },
            "detection_label": "Saved HTML receipt page",
            "detection_detail": "Manual upload routed to the structured HTML intake path. No merchant-specific adapter was identified yet.",
        }

    return {
        "is_supported": False,
        "media_type": media_type,
        "original_filename": original_filename,
        "normalized_filename": normalized_filename,
        "source": _base_manual_source(),
        "parser_hints": {
            "merchant_hint": None,
            "artifact_kind": "unknown",
            "needs_ocr": False,
            "likely_language": "en",
            "expected_layout": "unknown",
        },
        "detection_label": "Unsupported artifact",
        "detection_detail": f"The detected media type '{media_type}' is not supported by the current intake router.",
    }
