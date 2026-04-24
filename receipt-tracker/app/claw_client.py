import json
import os
import urllib.request
import urllib.error
from time import perf_counter

from app.learning import get_learned_item_suggestions
from app.openclaw_strategy import build_openclaw_strategy, group_items_by_openclaw_lane


CATEGORY_CHOICES = [
    "Produce",
    "Dairy",
    "Meat & Seafood",
    "Bakery",
    "Frozen",
    "Pantry",
    "Snacks",
    "Beverages",
    "Household",
    "Personal Care",
    "Prepared Foods",
    "Uncategorized",
]


class ClawConfigError(Exception):
    pass


class ClawRequestError(Exception):
    def __init__(self, message: str, telemetry: list[dict] | None = None):
        super().__init__(message)
        self.telemetry = list(telemetry or [])



def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None



def _get_gateway_settings() -> tuple[str, str]:
    base_url = _env("OPENCLAW_BASE_URL", "http://127.0.0.1:18789")
    gateway_token = _env("OPENCLAW_GATEWAY_TOKEN")

    if not gateway_token:
        raise ClawConfigError(
            "OPENCLAW_GATEWAY_TOKEN is not set. Export it before starting the app."
        )

    return base_url.rstrip("/"), gateway_token



def _strip_code_fence(text: str) -> str:
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

    return text



def _extract_json_blob(text: str) -> str:
    text = _strip_code_fence(text)

    try:
        json.loads(text)
        return text
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        json.loads(candidate)
        return candidate

    raise ClawRequestError("OpenClaw did not return valid JSON.")



def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts).strip()

    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"].strip()
        if isinstance(content.get("content"), str):
            return content["content"].strip()

    return str(content).strip()



def _validate_category(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value if value in CATEGORY_CHOICES else None



def _normalize_confidence(value) -> str:
    if not isinstance(value, str):
        return "low"

    value = value.strip().lower()
    if value not in {"low", "medium", "high"}:
        return "low"

    return value



def _optional_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None



def _optional_float(value) -> float | None:
    if value in (None, "", "-"):
        return None

    try:
        return float(value)
    except Exception:
        return None



def _optional_int(value) -> int | None:
    if value in (None, "", "-"):
        return None

    try:
        return int(value)
    except Exception:
        return None



def _extract_suggestion_list(parsed: dict) -> list:
    if isinstance(parsed.get("suggestions"), list):
        return parsed["suggestions"]

    if isinstance(parsed.get("review_suggestions"), list):
        return parsed["review_suggestions"]

    if isinstance(parsed.get("data"), dict):
        data = parsed["data"]
        if isinstance(data.get("suggestions"), list):
            return data["suggestions"]
        if isinstance(data.get("review_suggestions"), list):
            return data["review_suggestions"]

    return []



def _normalize_target_type(value, line_number: int | None, field_name: str | None) -> str:
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"item", "header"}:
            return value

    if line_number is not None:
        return "item"

    if field_name:
        return "header"

    return "item"



def _build_prompt_payload(receipt: dict, uncertain_items: list[dict], strategy: dict) -> dict:
    return {
        "request_type": "review_receipt_uncertainty",
        "strategy_context": {
            "lane": strategy.get("lane"),
            "lane_label": strategy.get("lane_label"),
            "agent_id": strategy.get("agent_id"),
            "model_label": strategy.get("model_label"),
        },
        "rules": [
            "Return JSON only.",
            "Do not use markdown code fences.",
            "Top-level key must be suggestions.",
            "Do not use review_suggestions as a top-level key.",
            "Do not invent prices.",
            "If price evidence is weak or conflicting, keep paid_price_hint as null.",
            "Use only the allowed category taxonomy.",
            "Keep suggestions conservative and compact.",
            "Only review the unresolved items supplied in this request.",
        ] + list(strategy.get("lane_rules") or []),
        "allowed_categories": CATEGORY_CHOICES,
        "receipt_context": {
            "receipt_id": receipt.get("id"),
            "artifact_id": receipt.get("artifact_id"),
            "merchant_raw": receipt.get("merchant_raw"),
            "merchant_canonical": receipt.get("merchant_canonical"),
            "purchase_date": receipt.get("purchase_date"),
            "receipt_total": receipt.get("total"),
            "review_notes": receipt.get("review_notes"),
        },
        "raw_ocr_excerpt": (receipt.get("ocr_text_raw") or "")[:12000],
        "uncertain_header_fields": [],
        "uncertain_items": uncertain_items,
        "required_output_schema": {
            "suggestions": [
                {
                    "target_type": "item",
                    "line_number": 0,
                    "field_name": None,
                    "suggested_name": "string or null",
                    "suggested_category": "one of allowed_categories or null",
                    "paid_price_hint": "number or null",
                    "confidence": "low|medium|high",
                    "reason": "short string",
                }
            ]
        },
    }



def _build_openclaw_request_body(receipt: dict, strategy: dict, unresolved_items: list[dict]) -> dict:
    return {
        "model": strategy.get("request_model"),
        "temperature": strategy.get("temperature", 0.1),
        "max_tokens": strategy.get("max_tokens", 1200),
        "user": f"receipt-review-{receipt.get('id', 'unknown')}-{strategy.get('lane', 'generic')}",
        "messages": [
            {"role": "system", "content": strategy.get("system_prompt")},
            {"role": "user", "content": json.dumps(_build_prompt_payload(receipt, unresolved_items, strategy), indent=2)},
        ],
    }



def _summarize_openclaw_stats(stats: dict, openclaw_invocations: list[dict]) -> None:
    stats["openclaw_invocation_count"] = len(openclaw_invocations)
    stats["openclaw_lanes"] = [inv.get("lane") for inv in openclaw_invocations if inv.get("lane")]
    stats["openclaw_lane_labels"] = {
        inv.get("lane"): inv.get("lane_label")
        for inv in openclaw_invocations
        if inv.get("lane")
    }
    stats["openclaw_agents_used"] = sorted({
        inv.get("agent_id") for inv in openclaw_invocations if inv.get("agent_id")
    })
    stats["openclaw_model_labels"] = sorted({
        inv.get("model_label") for inv in openclaw_invocations if inv.get("model_label")
    })
    stats["openclaw_total_latency_ms"] = sum(int(inv.get("duration_ms") or 0) for inv in openclaw_invocations)
    stats["openclaw_avg_latency_ms"] = round(
        stats["openclaw_total_latency_ms"] / max(1, len(openclaw_invocations)),
        1,
    ) if openclaw_invocations else 0.0

    item_count_by_lane: dict[str, int] = {}
    returned_by_lane: dict[str, int] = {}
    for inv in openclaw_invocations:
        lane = str(inv.get("lane") or "")
        if not lane:
            continue
        item_count_by_lane[lane] = item_count_by_lane.get(lane, 0) + int(inv.get("item_count") or 0)
        returned_by_lane[lane] = returned_by_lane.get(lane, 0) + int(inv.get("returned_count") or 0)

    stats["openclaw_item_count_by_lane"] = item_count_by_lane
    stats["openclaw_returned_by_lane"] = returned_by_lane
    stats["openclaw_telemetry"] = openclaw_invocations



def _invoke_openclaw_lane(
    *,
    base_url: str,
    gateway_token: str,
    receipt: dict,
    strategy: dict,
    unresolved_items: list[dict],
    prior_telemetry: list[dict],
) -> tuple[list[dict], dict]:
    request_body = _build_openclaw_request_body(receipt, strategy, unresolved_items)

    req = urllib.request.Request(
        url=f"{base_url}/v1/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {gateway_token}",
            "x-openclaw-agent-id": strategy.get("agent_id"),
        },
        method="POST",
    )

    started = perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        duration_ms = int((perf_counter() - started) * 1000)
        failure = {
            "lane": strategy.get("lane"),
            "lane_label": strategy.get("lane_label"),
            "agent_id": strategy.get("agent_id"),
            "model_label": strategy.get("model_label"),
            "request_model": strategy.get("request_model"),
            "temperature": strategy.get("temperature"),
            "max_tokens": strategy.get("max_tokens"),
            "item_count": len(unresolved_items),
            "line_numbers": [item.get("line_number") for item in unresolved_items if item.get("line_number") is not None],
            "returned_count": 0,
            "duration_ms": duration_ms,
            "success": False,
            "error_message": f"HTTP {exc.code}: {detail}",
        }
        raise ClawRequestError(f"OpenClaw HTTP {exc.code}: {detail}", telemetry=prior_telemetry + [failure]) from exc
    except urllib.error.URLError as exc:
        duration_ms = int((perf_counter() - started) * 1000)
        failure = {
            "lane": strategy.get("lane"),
            "lane_label": strategy.get("lane_label"),
            "agent_id": strategy.get("agent_id"),
            "model_label": strategy.get("model_label"),
            "request_model": strategy.get("request_model"),
            "temperature": strategy.get("temperature"),
            "max_tokens": strategy.get("max_tokens"),
            "item_count": len(unresolved_items),
            "line_numbers": [item.get("line_number") for item in unresolved_items if item.get("line_number") is not None],
            "returned_count": 0,
            "duration_ms": duration_ms,
            "success": False,
            "error_message": f"Could not reach OpenClaw gateway: {exc}",
        }
        raise ClawRequestError(f"Could not reach OpenClaw gateway: {exc}", telemetry=prior_telemetry + [failure]) from exc
    except Exception as exc:
        duration_ms = int((perf_counter() - started) * 1000)
        failure = {
            "lane": strategy.get("lane"),
            "lane_label": strategy.get("lane_label"),
            "agent_id": strategy.get("agent_id"),
            "model_label": strategy.get("model_label"),
            "request_model": strategy.get("request_model"),
            "temperature": strategy.get("temperature"),
            "max_tokens": strategy.get("max_tokens"),
            "item_count": len(unresolved_items),
            "line_numbers": [item.get("line_number") for item in unresolved_items if item.get("line_number") is not None],
            "returned_count": 0,
            "duration_ms": duration_ms,
            "success": False,
            "error_message": f"OpenClaw request failed: {exc}",
        }
        raise ClawRequestError(f"OpenClaw request failed: {exc}", telemetry=prior_telemetry + [failure]) from exc

    duration_ms = int((perf_counter() - started) * 1000)

    try:
        content = payload["choices"][0]["message"]["content"]
        content_text = _content_to_text(content)
        json_blob = _extract_json_blob(content_text)
        parsed = json.loads(json_blob)
    except Exception as exc:
        failure = {
            "lane": strategy.get("lane"),
            "lane_label": strategy.get("lane_label"),
            "agent_id": strategy.get("agent_id"),
            "model_label": strategy.get("model_label"),
            "request_model": strategy.get("request_model"),
            "temperature": strategy.get("temperature"),
            "max_tokens": strategy.get("max_tokens"),
            "item_count": len(unresolved_items),
            "line_numbers": [item.get("line_number") for item in unresolved_items if item.get("line_number") is not None],
            "returned_count": 0,
            "duration_ms": duration_ms,
            "success": False,
            "error_message": f"Could not parse OpenClaw response: {exc}",
        }
        raise ClawRequestError(f"Could not parse OpenClaw response: {exc}", telemetry=prior_telemetry + [failure]) from exc

    raw_suggestions = _extract_suggestion_list(parsed)
    if not isinstance(raw_suggestions, list):
        failure = {
            "lane": strategy.get("lane"),
            "lane_label": strategy.get("lane_label"),
            "agent_id": strategy.get("agent_id"),
            "model_label": strategy.get("model_label"),
            "request_model": strategy.get("request_model"),
            "temperature": strategy.get("temperature"),
            "max_tokens": strategy.get("max_tokens"),
            "item_count": len(unresolved_items),
            "line_numbers": [item.get("line_number") for item in unresolved_items if item.get("line_number") is not None],
            "returned_count": 0,
            "duration_ms": duration_ms,
            "success": False,
            "error_message": "OpenClaw returned invalid suggestions format.",
        }
        raise ClawRequestError(
            "OpenClaw returned invalid suggestions format.",
            telemetry=prior_telemetry + [failure],
        )

    cleaned: list[dict] = []
    openclaw_added = 0
    for suggestion in raw_suggestions:
        if not isinstance(suggestion, dict):
            continue

        line_number = _optional_int(suggestion.get("line_number"))
        field_name = _optional_text(suggestion.get("field_name"))
        target_type = _normalize_target_type(
            suggestion.get("target_type"),
            line_number,
            field_name,
        )

        suggested_name = _optional_text(suggestion.get("suggested_name"))
        suggested_category = _validate_category(suggestion.get("suggested_category"))
        paid_price_hint = _optional_float(suggestion.get("paid_price_hint"))
        confidence = _normalize_confidence(suggestion.get("confidence"))
        reason = _optional_text(suggestion.get("reason")) or "No reason provided."

        if target_type == "item" and line_number is None:
            continue

        if target_type == "item":
            field_name = None

        cleaned.append(
            {
                "source": "openclaw",
                "matched_observation_count": None,
                "target_type": target_type,
                "line_number": line_number,
                "field_name": field_name,
                "suggested_name": suggested_name,
                "suggested_category": suggested_category,
                "paid_price_hint": paid_price_hint,
                "confidence": confidence,
                "reason": reason,
                "openclaw_lane": strategy.get("lane"),
                "openclaw_lane_label": strategy.get("lane_label"),
                "openclaw_agent_id": strategy.get("agent_id"),
                "openclaw_model_label": strategy.get("model_label"),
                "openclaw_request_model": strategy.get("request_model"),
            }
        )
        openclaw_added += 1

    invocation = {
        "lane": strategy.get("lane"),
        "lane_label": strategy.get("lane_label"),
        "agent_id": strategy.get("agent_id"),
        "model_label": strategy.get("model_label"),
        "request_model": strategy.get("request_model"),
        "temperature": strategy.get("temperature"),
        "max_tokens": strategy.get("max_tokens"),
        "item_count": len(unresolved_items),
        "line_numbers": [item.get("line_number") for item in unresolved_items if item.get("line_number") is not None],
        "returned_count": openclaw_added,
        "duration_ms": duration_ms,
        "success": True,
        "error_message": None,
    }
    return cleaned, invocation



def get_line_item_suggestion_bundle(receipt: dict, items: list[dict]) -> dict:
    uncertain_items = []
    for item in items:
        if not item.get("needs_review"):
            continue

        uncertain_items.append(
            {
                "line_number": item.get("line_number"),
                "item_text_raw": item.get("item_text_raw"),
                "item_name_normalized": item.get("item_name_normalized"),
                "quantity": item.get("quantity"),
                "line_total": item.get("line_total"),
                "category": item.get("category"),
                "category_source_raw": item.get("category_source_raw"),
                "review_notes": item.get("review_notes"),
                "needs_review": item.get("needs_review"),
                "source_item_code": item.get("source_item_code"),
                "source_item_detail_hint": item.get("source_item_detail_hint"),
            }
        )

    stats = {
        "has_run": len(uncertain_items) > 0,
        "total_review_items": len(uncertain_items),
        "learned_exact_matches": 0,
        "local_mapping_hints": 0,
        "local_repair_hints": 0,
        "locally_handled_before_openclaw": 0,
        "remaining_for_openclaw": 0,
        "openclaw_called": False,
        "openclaw_returned": 0,
        "fully_covered_by_learned": False,
        "fully_covered_locally": False,
        "total_suggestions_returned": 0,
        "openclaw_invocation_count": 0,
        "openclaw_lanes": [],
        "openclaw_lane_labels": {},
        "openclaw_agents_used": [],
        "openclaw_model_labels": [],
        "openclaw_total_latency_ms": 0,
        "openclaw_avg_latency_ms": 0.0,
        "openclaw_item_count_by_lane": {},
        "openclaw_returned_by_lane": {},
        "openclaw_telemetry": [],
    }

    if not uncertain_items:
        return {
            "suggestions": [],
            "stats": stats,
            "openclaw_invocations": [],
        }

    learned_suggestions = get_learned_item_suggestions(receipt, uncertain_items)
    learned_line_numbers = {
        suggestion.get("line_number")
        for suggestion in learned_suggestions
        if suggestion.get("target_type") == "item"
    }

    unresolved_items = [
        item for item in uncertain_items
        if item.get("line_number") not in learned_line_numbers
    ]

    local_repair_hint_count = sum(
        1
        for suggestion in learned_suggestions
        if suggestion.get("source") == "approved_suppression_pattern"
        or suggestion.get("suggested_action") == "suppress"
    )
    local_mapping_hint_count = len(learned_suggestions) - local_repair_hint_count

    stats["learned_exact_matches"] = local_mapping_hint_count
    stats["local_mapping_hints"] = local_mapping_hint_count
    stats["local_repair_hints"] = local_repair_hint_count
    stats["locally_handled_before_openclaw"] = len(learned_suggestions)
    stats["remaining_for_openclaw"] = len(unresolved_items)
    stats["fully_covered_by_learned"] = (
        len(uncertain_items) > 0 and len(unresolved_items) == 0
    )
    stats["fully_covered_locally"] = (
        len(uncertain_items) > 0 and len(unresolved_items) == 0
    )

    if not unresolved_items:
        stats["total_suggestions_returned"] = len(learned_suggestions)
        return {
            "suggestions": learned_suggestions,
            "stats": stats,
            "openclaw_invocations": [],
        }

    base_url, gateway_token = _get_gateway_settings()
    stats["openclaw_called"] = True

    cleaned = list(learned_suggestions)
    openclaw_added = 0
    openclaw_invocations: list[dict] = []

    lane_groups = group_items_by_openclaw_lane(receipt, unresolved_items)
    for group in lane_groups:
        lane_items = list(group.get("items") or [])
        if not lane_items:
            continue

        strategy = build_openclaw_strategy(receipt, group["lane"], lane_items)
        lane_suggestions, invocation = _invoke_openclaw_lane(
            base_url=base_url,
            gateway_token=gateway_token,
            receipt=receipt,
            strategy=strategy,
            unresolved_items=lane_items,
            prior_telemetry=openclaw_invocations,
        )
        cleaned.extend(lane_suggestions)
        openclaw_added += len(lane_suggestions)
        openclaw_invocations.append(invocation)

    stats["openclaw_returned"] = openclaw_added
    stats["total_suggestions_returned"] = len(cleaned)
    _summarize_openclaw_stats(stats, openclaw_invocations)

    return {
        "suggestions": cleaned,
        "stats": stats,
        "openclaw_invocations": openclaw_invocations,
    }



def get_line_item_suggestions(receipt: dict, items: list[dict]) -> list[dict]:
    bundle = get_line_item_suggestion_bundle(receipt, items)
    return bundle["suggestions"]
