from __future__ import annotations

import os
import re
from typing import Iterable

LANE_ORDER = (
    "structured_item_resolution",
    "category_only_resolution",
    "ocr_item_resolution",
)

LANE_LABELS = {
    "structured_item_resolution": "Structured item resolution",
    "category_only_resolution": "Category-only resolution",
    "ocr_item_resolution": "OCR item resolution",
}

LANE_CONFIG = {
    "structured_item_resolution": {
        "suffix": "STRUCTURED",
        "default_model_label": "structured-reasoner",
        "default_temperature": 0.05,
        "default_max_tokens": 900,
        "system_prompt": (
            "You are receipt-review-structured. Work from structured merchant evidence first. "
            "Return exactly one raw JSON object and nothing else. "
            "Do not use markdown fences. Top-level key must be suggestions. "
            "Use source item codes when present. Be conservative about names and categories. "
            "Never invent prices."
        ),
        "lane_rules": [
            "Prefer structured evidence such as source item codes and stable line totals over OCR guesses.",
            "If a likely product description is weak, leave suggested_name null rather than fabricating a label.",
            "Category suggestions may still be provided when the category is clearer than the exact item name.",
        ],
    },
    "category_only_resolution": {
        "suffix": "CATEGORY",
        "default_model_label": "taxonomy-reasoner",
        "default_temperature": 0.05,
        "default_max_tokens": 800,
        "system_prompt": (
            "You are receipt-review-category. Work conservatively. "
            "Return exactly one raw JSON object and nothing else. "
            "Do not use markdown fences. Top-level key must be suggestions. "
            "Focus on category resolution when the item label is already reasonably stable. "
            "Do not rewrite item names unless the supplied name is clearly unusable. Never invent prices."
        ),
        "lane_rules": [
            "Prioritize suggested_category over suggested_name.",
            "Only replace the name when the source name is clearly broken or placeholder-like.",
            "If category confidence is weak, return suggested_category as null.",
        ],
    },
    "ocr_item_resolution": {
        "suffix": "OCR",
        "default_model_label": "ocr-reasoner",
        "default_temperature": 0.1,
        "default_max_tokens": 1200,
        "system_prompt": (
            "You are receipt-review-ocr. Work from noisy OCR evidence conservatively. "
            "Return exactly one raw JSON object and nothing else. "
            "Do not use markdown fences. Top-level key must be suggestions. "
            "Keep suggestions compact. Never invent prices."
        ),
        "lane_rules": [
            "Use OCR context only for the unresolved items supplied in this request.",
            "When OCR evidence is fragmented or ambiguous, prefer nulls over speculation.",
            "Use the allowed category taxonomy only.",
        ],
    },
}

PLACEHOLDER_NAME_PATTERNS = (
    re.compile(r"^BJ'?S ITEM\s+\d+$", re.IGNORECASE),
    re.compile(r"^ITEM\s+\d+$", re.IGNORECASE),
    re.compile(r"^[A-Z0-9'\-]+\s+ITEM\s+\d+$", re.IGNORECASE),
)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_int(name: str, default: int | None) -> int | None:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _env_float(name: str, default: float | None) -> float | None:
    value = _env(name)
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _optional_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _looks_like_placeholder_name(value: str | None) -> bool:
    text = _optional_text(value)
    if not text:
        return False
    normalized = " ".join(text.split())
    return any(pattern.match(normalized) for pattern in PLACEHOLDER_NAME_PATTERNS)


def classify_openclaw_lane(receipt: dict, item: dict) -> str:
    source_item_code = _optional_text(item.get("source_item_code"))
    if source_item_code:
        return "structured_item_resolution"

    item_name = _optional_text(item.get("item_name_normalized"))
    category = _optional_text(item.get("category"))

    if item_name and not category and not _looks_like_placeholder_name(item_name):
        return "category_only_resolution"

    return "ocr_item_resolution"


def group_items_by_openclaw_lane(receipt: dict, items: Iterable[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        lane = classify_openclaw_lane(receipt, item)
        grouped.setdefault(lane, []).append(item)

    groups: list[dict] = []
    for lane in LANE_ORDER:
        lane_items = grouped.get(lane) or []
        if not lane_items:
            continue
        groups.append(
            {
                "lane": lane,
                "lane_label": LANE_LABELS.get(lane, lane.replace("_", " ").title()),
                "items": lane_items,
            }
        )

    for lane, lane_items in grouped.items():
        if lane in LANE_ORDER or not lane_items:
            continue
        groups.append(
            {
                "lane": lane,
                "lane_label": LANE_LABELS.get(lane, lane.replace("_", " ").title()),
                "items": lane_items,
            }
        )

    return groups


def build_openclaw_strategy(receipt: dict, lane: str, items: list[dict]) -> dict:
    config = LANE_CONFIG.get(lane, LANE_CONFIG["ocr_item_resolution"])
    suffix = config["suffix"]

    default_agent = _env("OPENCLAW_AGENT_ID", "receipt-review")
    agent_id = _env(f"OPENCLAW_AGENT_ID_{suffix}", default_agent)

    model_label = (
        _env(f"OPENCLAW_MODEL_LABEL_{suffix}")
        or _env("OPENCLAW_MODEL_LABEL")
        or config["default_model_label"]
    )
    request_model = (
        _env(f"OPENCLAW_REQUEST_MODEL_{suffix}")
        or _env("OPENCLAW_REQUEST_MODEL")
        or f"openclaw:{agent_id}"
    )

    temperature = _env_float(f"OPENCLAW_TEMPERATURE_{suffix}", None)
    if temperature is None:
        temperature = _env_float("OPENCLAW_TEMPERATURE", config["default_temperature"])

    max_tokens = _env_int(f"OPENCLAW_MAX_TOKENS_{suffix}", None)
    if max_tokens is None:
        max_tokens = _env_int("OPENCLAW_MAX_TOKENS", config["default_max_tokens"])

    return {
        "lane": lane,
        "lane_label": LANE_LABELS.get(lane, lane.replace("_", " ").title()),
        "agent_id": agent_id,
        "model_label": model_label,
        "request_model": request_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "system_prompt": config["system_prompt"],
        "lane_rules": list(config.get("lane_rules") or []),
        "item_count": len(items),
        "line_numbers": [item.get("line_number") for item in items if item.get("line_number") is not None],
    }
