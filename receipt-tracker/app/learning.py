from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "receipts.db"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_key(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(str(value).strip().upper().split())
    return normalized or None


def canonicalize_merchant_key(value: str | None) -> str | None:
    return _normalize_key(value)


def normalize_raw_key(value: str | None) -> str | None:
    return _normalize_key(value)


def normalize_name_family_key(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^A-Z0-9]+", " ", str(value).upper())
    normalized = " ".join(normalized.split())
    return normalized or None


SUPPRESSION_UNIT_PATTERN = r"(?:LB|IB|B|KG|OZ|EA|CT)"
SUPPRESSION_PATTERN_SOURCE_TYPES = ("repair_suppression", "repair_merge_source")
SUPPRESSION_PATTERN_SOURCE_LABELS = {
    "repair_suppression": "manual suppression",
    "repair_merge_source": "merge-source fragment",
}


def _normalize_fragment_text(value: str | None) -> str | None:
    normalized = _normalize_key(value)
    if not normalized:
        return None

    normalized = re.sub(r"/\s*IB\b", "/LB", normalized)
    normalized = re.sub(r"/\s*B\b", "/LB", normalized)
    normalized = re.sub(r"\bIB\b", "LB", normalized)
    normalized = re.sub(r"\b1B\b", "LB", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or None


def _build_price_weight_fragment_signature(text: str) -> str:
    signature = text
    signature = re.sub(
        rf"\b\d+(?:\.\d+)?\s*{SUPPRESSION_UNIT_PATTERN}\b",
        "<MEASURE>",
        signature,
    )
    signature = re.sub(r"\$\s*\d+(?:\.\d{1,2})?", "$<AMOUNT>", signature)
    signature = re.sub(r"(?<![A-Z])\b\d+(?:\.\d+)?\b", "<AMOUNT>", signature)
    signature = re.sub(rf"/\s*{SUPPRESSION_UNIT_PATTERN}\b", "/<UNIT>", signature)
    signature = re.sub(rf"\b{SUPPRESSION_UNIT_PATTERN}\b", "<UNIT>", signature)
    signature = re.sub(r"\b[A-Z]{1,3}\b(?=\s*@)", "<TOKEN>", signature)
    return " ".join(signature.split())


def _build_footer_code_fragment_signature(text: str) -> str:
    signature = text
    signature = re.sub(r"\$\s*\d+(?:\.\d{1,2})?", "$<AMOUNT>", signature)
    signature = re.sub(r"\b\d{2,}-\d{2,}\b", "<CODEPAIR>", signature)
    signature = re.sub(r"\b\d+(?:\.\d+)?\b", "<NUM>", signature)
    return " ".join(signature.split())


def _build_numeric_fragment_signature(text: str) -> str:
    signature = text
    signature = re.sub(r"\$\s*\d+(?:\.\d{1,2})?", "$<AMOUNT>", signature)
    signature = re.sub(r"\b\d{2,}-\d{2,}\b", "<CODEPAIR>", signature)
    signature = re.sub(r"\b\d+(?:\.\d+)?\b", "<NUM>", signature)
    signature = re.sub(r"\b[A-Z]{1,3}\b", "<TOKEN>", signature)
    return " ".join(signature.split())


def derive_suppression_pattern(raw_text: str | None) -> dict | None:
    text = _normalize_fragment_text(raw_text)
    if not text:
        return None

    compact_text = re.sub(r"\s+", "", text)
    digit_count = sum(ch.isdigit() for ch in compact_text)
    digit_ratio = digit_count / max(1, len(compact_text))
    token_count = len(text.split())
    short_text = len(text) <= 40

    has_at = "@" in text
    has_currency = "$" in text
    has_unit_ratio = bool(re.search(rf"/\s*{SUPPRESSION_UNIT_PATTERN}\b", text))
    has_measure_value = bool(re.search(rf"\b\d+(?:\.\d+)?\s*{SUPPRESSION_UNIT_PATTERN}\b", text))
    has_hyphenated_numeric = bool(re.search(r"\b\d{2,}-\d{2,}\b", text))
    has_decimal = bool(re.search(r"\b\d+\.\d+\b", text))

    pattern_kind = None
    hint_label = "Likely fragment line"

    if short_text and has_at and (has_unit_ratio or has_measure_value or has_decimal):
        pattern_kind = "price_weight_fragment"
        signature = _build_price_weight_fragment_signature(text)
    elif short_text and has_currency and has_hyphenated_numeric:
        pattern_kind = "footer_code_fragment"
        signature = _build_footer_code_fragment_signature(text)
    elif short_text and token_count <= 4 and digit_ratio >= 0.35 and (has_currency or has_decimal or has_hyphenated_numeric):
        pattern_kind = "numeric_fragment"
        signature = _build_numeric_fragment_signature(text)
    else:
        return None

    if not signature:
        return None

    return {
        "pattern_key": f"{pattern_kind}|{signature}",
        "pattern_kind": pattern_kind,
        "signature": signature,
        "hint_label": hint_label,
        "suggested_action": "suppress",
        "match_mode": "signature_equals",
        "example_text": text,
    }


MERGE_REPAIR_REASON_PATTERN = re.compile(
    r"MERGED INTO ITEM\s+(\d+)\s+FROM SELECTED REPAIR FLOW\.?"
)


def parse_merge_target_item_id(suppression_reason: str | None) -> int | None:
    normalized_reason = _normalize_key(suppression_reason)
    if not normalized_reason:
        return None

    match = MERGE_REPAIR_REASON_PATTERN.search(normalized_reason)
    if not match:
        return None

    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _format_suppression_source_receipt_evidence(source_receipt_counts: dict[str, int] | None) -> str:
    if not source_receipt_counts:
        return ""

    parts: list[str] = []
    for source_type in SUPPRESSION_PATTERN_SOURCE_TYPES:
        count = int((source_receipt_counts or {}).get(source_type) or 0)
        if count <= 0:
            continue

        label = SUPPRESSION_PATTERN_SOURCE_LABELS.get(source_type, source_type)
        parts.append(f"{count} {label} receipt(s)")

    return ", ".join(parts)


def _get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_learning_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER,
            receipt_item_id INTEGER,
            artifact_id TEXT,
            merchant_key TEXT,
            observation_type TEXT,
            raw_key TEXT,
            normalized_value TEXT,
            category_value TEXT,
            confidence REAL,
            evidence_json TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_type TEXT,
            merchant_scope TEXT,
            source_scope TEXT,
            confidence TEXT,
            summary TEXT,
            evidence_count INTEGER,
            proposal_json TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_learning_observations_lookup
        ON learning_observations (
            merchant_key,
            raw_key,
            observation_type,
            status
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_learning_observations_receipt
        ON learning_observations (
            receipt_id,
            receipt_item_id
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_profile_proposals_status
        ON profile_proposals (
            status,
            proposal_type,
            merchant_scope
        )
        """
    )


    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS receipt_run_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            artifact_id TEXT,
            merchant_key TEXT,
            metric_type TEXT NOT NULL,
            source_run_id INTEGER,
            total_items INTEGER NOT NULL DEFAULT 0,
            active_item_count INTEGER NOT NULL DEFAULT 0,
            suppressed_item_count INTEGER NOT NULL DEFAULT 0,
            total_review_items INTEGER NOT NULL DEFAULT 0,
            local_mapping_hints INTEGER NOT NULL DEFAULT 0,
            local_repair_hints INTEGER NOT NULL DEFAULT 0,
            locally_handled_before_openclaw INTEGER NOT NULL DEFAULT 0,
            remaining_for_openclaw INTEGER NOT NULL DEFAULT 0,
            openclaw_called INTEGER NOT NULL DEFAULT 0,
            openclaw_returned INTEGER NOT NULL DEFAULT 0,
            total_suggestions_returned INTEGER NOT NULL DEFAULT 0,
            local_profile_mapping_count INTEGER NOT NULL DEFAULT 0,
            local_alias_bundle_count INTEGER NOT NULL DEFAULT 0,
            local_exact_reuse_count INTEGER NOT NULL DEFAULT 0,
            local_repair_hint_count INTEGER NOT NULL DEFAULT 0,
            openclaw_suggested_count INTEGER NOT NULL DEFAULT 0,
            no_suggestion_count INTEGER NOT NULL DEFAULT 0,
            manual_add_count INTEGER NOT NULL DEFAULT 0,
            manual_merge_source_count INTEGER NOT NULL DEFAULT 0,
            manual_suppression_count INTEGER NOT NULL DEFAULT 0,
            knowledge_captured_count INTEGER NOT NULL DEFAULT 0,
            summary_json TEXT,
            decision_breakdown_json TEXT,
            line_decisions_json TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_receipt_run_metrics_receipt
        ON receipt_run_metrics (
            receipt_id,
            metric_type,
            id
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_receipt_run_metrics_type_merchant
        ON receipt_run_metrics (
            metric_type,
            merchant_key,
            id
        )
        """
    )


    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS openclaw_invocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            artifact_id TEXT,
            merchant_key TEXT,
            source_run_id INTEGER,
            lane TEXT,
            lane_label TEXT,
            agent_id TEXT,
            model_label TEXT,
            request_model TEXT,
            temperature REAL,
            max_tokens INTEGER,
            item_count INTEGER NOT NULL DEFAULT 0,
            line_numbers_json TEXT,
            returned_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 1,
            error_message TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_openclaw_invocations_receipt
        ON openclaw_invocations (
            receipt_id,
            source_run_id,
            id
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_openclaw_invocations_lane
        ON openclaw_invocations (
            lane,
            success,
            id
        )
        """
    )


DECISION_PATH_ORDER = (
    "local_profile_mapping",
    "local_alias_bundle",
    "local_exact_reuse",
    "local_repair_hint",
    "openclaw_suggested",
    "no_suggestion",
)

DECISION_PATH_LABELS = {
    "local_profile_mapping": "Local profile mapping",
    "local_alias_bundle": "Local alias bundle",
    "local_exact_reuse": "Exact learned reuse",
    "local_repair_hint": "Local repair hint",
    "openclaw_suggested": "OpenClaw suggestion",
    "no_suggestion": "No suggestion returned",
}


OPENCLAW_LANE_LABELS = {
    "structured_item_resolution": "Structured lane",
    "category_only_resolution": "Category-only lane",
    "ocr_item_resolution": "OCR lane",
}


def _json_loads_safe(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _coerce_int(value: object) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except Exception:
        return 0


def _coerce_bool_int(value: object) -> int:
    return 1 if bool(value) else 0


def _row_value(record: sqlite3.Row | dict, key: str):
    if isinstance(record, dict):
        return record.get(key)
    return record[key]


def _metric_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator * 100.0) / denominator, 1)


def _normalize_compare_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _text_matches(left: object, right: object) -> bool:
    left_norm = _normalize_compare_text(left)
    right_norm = _normalize_compare_text(right)
    if not left_norm or not right_norm:
        return False
    return left_norm == right_norm


def _default_openclaw_lane_outcomes() -> dict:
    return {
        lane: {
            "touched": 0,
            "accepted": 0,
            "no_repair": 0,
            "repaired_after": 0,
        }
        for lane in OPENCLAW_LANE_LABELS
    }


def _merge_openclaw_lane_outcomes(base: dict | None, incoming: dict | None) -> dict:
    merged = _default_openclaw_lane_outcomes()

    for source in (base or {}, incoming or {}):
        if not isinstance(source, dict):
            continue
        for lane, counts in source.items():
            if lane not in merged:
                merged[lane] = {
                    "touched": 0,
                    "accepted": 0,
                    "no_repair": 0,
                    "repaired_after": 0,
                }
            if not isinstance(counts, dict):
                continue
            for key in ("touched", "accepted", "no_repair", "repaired_after"):
                merged[lane][key] += _coerce_int(counts.get(key))

    return merged


def _build_openclaw_lane_effectiveness_cards(lane_outcomes: dict | None) -> list[dict]:
    merged = _merge_openclaw_lane_outcomes({}, lane_outcomes or {})
    cards = []

    for lane_key, lane_label in OPENCLAW_LANE_LABELS.items():
        counts = merged.get(lane_key) or {}
        touched = _coerce_int(counts.get("touched"))
        accepted = _coerce_int(counts.get("accepted"))
        no_repair = _coerce_int(counts.get("no_repair"))
        repaired_after = _coerce_int(counts.get("repaired_after"))
        cards.append(
            {
                "key": lane_key,
                "label": lane_label,
                "touched": touched,
                "accepted": accepted,
                "no_repair": no_repair,
                "repaired_after": repaired_after,
                "acceptance_rate_pct": _metric_rate(accepted, touched),
                "no_repair_rate_pct": _metric_rate(no_repair, touched),
            }
        )

    return cards


def _build_openclaw_effectiveness_from_approval(
    latest_run: dict | None,
    items: list[sqlite3.Row | dict],
    repair_rows: list[sqlite3.Row | dict],
) -> dict:
    lane_outcomes = _default_openclaw_lane_outcomes()
    line_decisions: list[dict] = []

    if not latest_run:
        return {
            "ai_touched_item_count": 0,
            "ai_accepted_item_count": 0,
            "ai_no_repair_item_count": 0,
            "ai_repaired_after_item_count": 0,
            "ai_assisted_receipt": False,
            "ai_lane_outcomes": lane_outcomes,
            "ai_lane_cards": _build_openclaw_lane_effectiveness_cards(lane_outcomes),
            "line_decisions": line_decisions,
        }

    item_by_id: dict[int, sqlite3.Row | dict] = {}
    for item in items:
        item_id = _coerce_int(_row_value(item, "id"))
        if item_id > 0:
            item_by_id[item_id] = item

    repaired_item_ids: set[int] = set()
    for row in repair_rows:
        observation_type = str(_row_value(row, "observation_type") or "").strip()
        if observation_type not in {"repair_merge_source", "repair_suppression"}:
            continue

        direct_item_id = _coerce_int(_row_value(row, "receipt_item_id"))
        if direct_item_id > 0:
            repaired_item_ids.add(direct_item_id)

        evidence = _json_loads_safe(_row_value(row, "evidence_json"), {})
        if isinstance(evidence, dict):
            for candidate in (evidence.get("receipt_item_id"), evidence.get("source_item_id")):
                candidate_id = _coerce_int(candidate)
                if candidate_id > 0:
                    repaired_item_ids.add(candidate_id)

    ai_touched_item_count = 0
    ai_accepted_item_count = 0
    ai_no_repair_item_count = 0
    ai_repaired_after_item_count = 0

    for raw_entry in latest_run.get("line_decisions") or []:
        entry = dict(raw_entry)
        source = _normalize_compare_text(entry.get("source"))
        if source != "openclaw":
            line_decisions.append(entry)
            continue

        ai_touched_item_count += 1
        item_id = _coerce_int(entry.get("item_id"))
        final_item = item_by_id.get(item_id)
        final_missing = final_item is None
        final_is_suppressed = False
        final_name = None
        final_category = None

        if final_item is not None:
            final_is_suppressed = _coerce_int(_row_value(final_item, "is_suppressed")) == 1
            final_name = _row_value(final_item, "item_name_normalized")
            final_category = _row_value(final_item, "category")

        accepted_name = _text_matches(entry.get("suggested_name"), final_name)
        accepted_category = _text_matches(entry.get("suggested_category"), final_category)
        repaired_after = bool(final_missing or final_is_suppressed or item_id in repaired_item_ids)
        accepted = bool((accepted_name or accepted_category) and not repaired_after)
        no_repair = bool(accepted and not repaired_after)

        lane_key = str(entry.get("openclaw_lane") or "").strip() or "ocr_item_resolution"
        if lane_key not in lane_outcomes:
            lane_outcomes[lane_key] = {
                "touched": 0,
                "accepted": 0,
                "no_repair": 0,
                "repaired_after": 0,
            }
        lane_outcomes[lane_key]["touched"] += 1
        if accepted:
            ai_accepted_item_count += 1
            lane_outcomes[lane_key]["accepted"] += 1
        if no_repair:
            ai_no_repair_item_count += 1
            lane_outcomes[lane_key]["no_repair"] += 1
        if repaired_after:
            ai_repaired_after_item_count += 1
            lane_outcomes[lane_key]["repaired_after"] += 1

        entry["ai_accepted"] = accepted
        entry["ai_accepted_name"] = accepted_name
        entry["ai_accepted_category"] = accepted_category
        entry["ai_repaired_after"] = repaired_after
        entry["ai_no_repair"] = no_repair
        entry["final_item_name_normalized"] = final_name
        entry["final_category"] = final_category
        entry["final_is_suppressed"] = final_is_suppressed
        if no_repair:
            entry["approval_outcome_label"] = "Accepted without later repair"
        elif repaired_after:
            entry["approval_outcome_label"] = "Later repaired by human"
        elif accepted:
            entry["approval_outcome_label"] = "Retained in approved outcome"
        else:
            entry["approval_outcome_label"] = "Not retained in approved outcome"

        line_decisions.append(entry)

    return {
        "ai_touched_item_count": ai_touched_item_count,
        "ai_accepted_item_count": ai_accepted_item_count,
        "ai_no_repair_item_count": ai_no_repair_item_count,
        "ai_repaired_after_item_count": ai_repaired_after_item_count,
        "ai_assisted_receipt": bool(ai_accepted_item_count > 0),
        "ai_lane_outcomes": lane_outcomes,
        "ai_lane_cards": _build_openclaw_lane_effectiveness_cards(lane_outcomes),
        "line_decisions": line_decisions,
    }


def _default_ai_learning_lane_outcomes() -> dict:
    return {
        lane: {
            "captured": 0,
            "no_repair": 0,
        }
        for lane in OPENCLAW_LANE_LABELS
    }


def _merge_ai_learning_lane_outcomes(base: dict | None, incoming: dict | None) -> dict:
    merged = _default_ai_learning_lane_outcomes()

    for source in (base or {}, incoming or {}):
        if not isinstance(source, dict):
            continue
        for lane, counts in source.items():
            if lane not in merged:
                merged[lane] = {
                    "captured": 0,
                    "no_repair": 0,
                }
            if not isinstance(counts, dict):
                continue
            for key in ("captured", "no_repair"):
                merged[lane][key] += _coerce_int(counts.get(key))

    return merged


def _build_ai_learning_lane_cards(lane_outcomes: dict | None) -> list[dict]:
    merged = _merge_ai_learning_lane_outcomes({}, lane_outcomes or {})
    total_captured = sum(_coerce_int(counts.get("captured")) for counts in merged.values())
    cards: list[dict] = []

    for lane_key, lane_label in OPENCLAW_LANE_LABELS.items():
        counts = merged.get(lane_key) or {}
        captured = _coerce_int(counts.get("captured"))
        no_repair = _coerce_int(counts.get("no_repair"))
        cards.append(
            {
                "key": lane_key,
                "label": lane_label,
                "captured": captured,
                "no_repair": no_repair,
                "capture_share_pct": _metric_rate(captured, total_captured),
                "no_repair_rate_pct": _metric_rate(no_repair, captured),
            }
        )

    return cards


def _summarize_ai_learning_entries(entries: list[dict] | None, *, limit: int = 6) -> dict:
    lane_outcomes = _default_ai_learning_lane_outcomes()
    strategy_rollups: dict[tuple[str, str, str, str], dict] = {}
    captured_count = 0
    no_repair_count = 0

    for raw_entry in entries or []:
        if not isinstance(raw_entry, dict):
            continue
        if not raw_entry.get("ai_accepted"):
            continue

        captured_count += 1
        if raw_entry.get("ai_no_repair"):
            no_repair_count += 1

        lane_key = str(raw_entry.get("openclaw_lane") or "").strip() or "ocr_item_resolution"
        lane_label = str(raw_entry.get("openclaw_lane_label") or OPENCLAW_LANE_LABELS.get(lane_key, lane_key) or "OpenClaw lane")
        if lane_key not in lane_outcomes:
            lane_outcomes[lane_key] = {
                "captured": 0,
                "no_repair": 0,
            }
        lane_outcomes[lane_key]["captured"] += 1
        if raw_entry.get("ai_no_repair"):
            lane_outcomes[lane_key]["no_repair"] += 1

        model_label = str(raw_entry.get("openclaw_model_label") or "").strip() or str(raw_entry.get("openclaw_request_model") or "").strip() or "OpenClaw strategy"
        agent_id = str(raw_entry.get("openclaw_agent_id") or "").strip()
        request_model = str(raw_entry.get("openclaw_request_model") or "").strip()
        strategy_key = (lane_key, lane_label, model_label, agent_id or request_model)
        rollup = strategy_rollups.setdefault(
            strategy_key,
            {
                "lane_key": lane_key,
                "lane_label": lane_label,
                "model_label": model_label,
                "agent_id": agent_id or None,
                "request_model": request_model or None,
                "captured": 0,
                "no_repair": 0,
            },
        )
        rollup["captured"] += 1
        if raw_entry.get("ai_no_repair"):
            rollup["no_repair"] += 1

    strategy_cards = sorted(
        strategy_rollups.values(),
        key=lambda row: (
            _coerce_int(row.get("captured")),
            _coerce_int(row.get("no_repair")),
            str(row.get("lane_label") or ""),
            str(row.get("model_label") or ""),
        ),
        reverse=True,
    )

    for row in strategy_cards:
        captured = _coerce_int(row.get("captured"))
        no_repair = _coerce_int(row.get("no_repair"))
        row["capture_share_pct"] = _metric_rate(captured, captured_count)
        row["no_repair_rate_pct"] = _metric_rate(no_repair, captured)
        subtitle_parts = [row.get("lane_label") or "OpenClaw lane"]
        if row.get("agent_id"):
            subtitle_parts.append(f"agent {row['agent_id']}")
        elif row.get("request_model"):
            subtitle_parts.append(row["request_model"])
        row["subtitle"] = " · ".join(part for part in subtitle_parts if part)

    return {
        "captured_count": captured_count,
        "no_repair_count": no_repair_count,
        "lane_outcomes": lane_outcomes,
        "lane_cards": _build_ai_learning_lane_cards(lane_outcomes),
        "strategy_cards": strategy_cards[: max(1, int(limit))],
        "strategy_count": len(strategy_cards),
    }


def annotate_approved_item_observations_with_ai_provenance(
    conn: sqlite3.Connection,
    receipt_id: int,
    line_decisions: list[dict] | None,
) -> dict:
    accepted_entries_by_item_id: dict[int, dict] = {}
    accepted_entries: list[dict] = []

    for raw_entry in line_decisions or []:
        if not isinstance(raw_entry, dict):
            continue
        if str(raw_entry.get("source") or "").strip().lower() != "openclaw":
            continue
        if not raw_entry.get("ai_accepted"):
            continue

        item_id = _coerce_int(raw_entry.get("item_id"))
        if item_id <= 0:
            continue

        entry = dict(raw_entry)
        accepted_entries_by_item_id[item_id] = entry
        accepted_entries.append(entry)

    rows = conn.execute(
        """
        SELECT id, receipt_item_id, evidence_json
        FROM learning_observations
        WHERE receipt_id = ?
          AND observation_type = 'approved_item_observation'
          AND status = 'observed'
        ORDER BY id
        """,
        (receipt_id,),
    ).fetchall()

    now = _utcnow_iso()
    for row in rows:
        evidence = _json_loads_safe(row["evidence_json"], {})
        if not isinstance(evidence, dict):
            evidence = {}

        evidence["learning_source"] = "approved_item_observation"
        receipt_item_id = _coerce_int(row["receipt_item_id"])
        accepted_entry = accepted_entries_by_item_id.get(receipt_item_id)
        if accepted_entry:
            evidence["ai_provenance"] = {
                "accepted": True,
                "no_repair": bool(accepted_entry.get("ai_no_repair")),
                "repaired_after": bool(accepted_entry.get("ai_repaired_after")),
                "approval_outcome_label": accepted_entry.get("approval_outcome_label"),
                "lane": accepted_entry.get("openclaw_lane"),
                "lane_label": accepted_entry.get("openclaw_lane_label"),
                "agent_id": accepted_entry.get("openclaw_agent_id"),
                "model_label": accepted_entry.get("openclaw_model_label"),
                "request_model": accepted_entry.get("openclaw_request_model"),
            }
        else:
            evidence.pop("ai_provenance", None)

        conn.execute(
            """
            UPDATE learning_observations
            SET evidence_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                _stable_json(evidence),
                now,
                int(row["id"]),
            ),
        )

    return _summarize_ai_learning_entries(accepted_entries)


def _collect_ai_learning_summary_from_observations(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT receipt_id, evidence_json
        FROM learning_observations
        WHERE observation_type = 'approved_item_observation'
          AND status = 'observed'
        ORDER BY id DESC
        """
    ).fetchall()

    entries: list[dict] = []
    receipt_ids: set[int] = set()

    for row in rows:
        evidence = _json_loads_safe(row["evidence_json"], {})
        if not isinstance(evidence, dict):
            continue
        ai = evidence.get("ai_provenance")
        if not isinstance(ai, dict) or not ai.get("accepted"):
            continue

        receipt_id = _coerce_int(row["receipt_id"])
        if receipt_id > 0:
            receipt_ids.add(receipt_id)

        entries.append(
            {
                "ai_accepted": True,
                "ai_no_repair": bool(ai.get("no_repair")),
                "openclaw_lane": ai.get("lane"),
                "openclaw_lane_label": ai.get("lane_label"),
                "openclaw_agent_id": ai.get("agent_id"),
                "openclaw_model_label": ai.get("model_label"),
                "openclaw_request_model": ai.get("request_model"),
            }
        )

    summary = _summarize_ai_learning_entries(entries)
    summary["receipt_count"] = len(receipt_ids)
    return summary


def _classify_suggestion_decision_path(suggestion: dict | None) -> str:
    if not suggestion:
        return "no_suggestion"

    source = str(suggestion.get("source") or "").strip().lower()
    suggested_action = str(suggestion.get("suggested_action") or "").strip().lower()

    if source == "approved_profile_mapping":
        return "local_profile_mapping"
    if source == "approved_alias_bundle":
        return "local_alias_bundle"
    if source == "learned_observation_exact":
        return "local_exact_reuse"
    if source == "approved_suppression_pattern" or suggested_action == "suppress":
        return "local_repair_hint"
    if source == "openclaw":
        return "openclaw_suggested"

    if source.startswith("approved_") or source.startswith("learned_"):
        return "local_exact_reuse"

    return "openclaw_suggested"


def _build_decision_breakdown_from_line_decisions(line_decisions: list[dict]) -> dict:
    breakdown = {key: 0 for key in DECISION_PATH_ORDER}
    for entry in line_decisions:
        decision_path = str(entry.get("decision_path") or "").strip()
        if decision_path in breakdown:
            breakdown[decision_path] += 1
    return breakdown


def _parse_receipt_run_metric_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None

    parsed = {key: row[key] for key in row.keys()}
    parsed["id"] = _coerce_int(parsed.get("id"))
    parsed["receipt_id"] = _coerce_int(parsed.get("receipt_id"))
    parsed["source_run_id"] = _coerce_int(parsed.get("source_run_id")) or None

    for key in (
        "total_items",
        "active_item_count",
        "suppressed_item_count",
        "total_review_items",
        "local_mapping_hints",
        "local_repair_hints",
        "locally_handled_before_openclaw",
        "remaining_for_openclaw",
        "openclaw_called",
        "openclaw_returned",
        "total_suggestions_returned",
        "local_profile_mapping_count",
        "local_alias_bundle_count",
        "local_exact_reuse_count",
        "local_repair_hint_count",
        "openclaw_suggested_count",
        "no_suggestion_count",
        "manual_add_count",
        "manual_merge_source_count",
        "manual_suppression_count",
        "knowledge_captured_count",
    ):
        parsed[key] = _coerce_int(parsed.get(key))

    parsed["summary"] = _json_loads_safe(parsed.get("summary_json"), {})
    parsed["decision_breakdown"] = _json_loads_safe(parsed.get("decision_breakdown_json"), {})
    parsed["line_decisions"] = _json_loads_safe(parsed.get("line_decisions_json"), [])

    if not parsed["decision_breakdown"]:
        parsed["decision_breakdown"] = {
            "local_profile_mapping": parsed["local_profile_mapping_count"],
            "local_alias_bundle": parsed["local_alias_bundle_count"],
            "local_exact_reuse": parsed["local_exact_reuse_count"],
            "local_repair_hint": parsed["local_repair_hint_count"],
            "openclaw_suggested": parsed["openclaw_suggested_count"],
            "no_suggestion": parsed["no_suggestion_count"],
        }

    parsed["local_resolution_rate_pct"] = _metric_rate(
        parsed["locally_handled_before_openclaw"],
        parsed["total_review_items"],
    )
    parsed["openclaw_routed_rate_pct"] = _metric_rate(
        parsed["remaining_for_openclaw"],
        parsed["total_review_items"],
    )

    parsed["decision_path_cards"] = [
        {
            "key": key,
            "label": DECISION_PATH_LABELS[key],
            "count": _coerce_int(parsed["decision_breakdown"].get(key)),
        }
        for key in DECISION_PATH_ORDER
    ]

    for entry in parsed["line_decisions"]:
        decision_path = str(entry.get("decision_path") or "").strip()
        entry["decision_label"] = DECISION_PATH_LABELS.get(decision_path, decision_path or "Decision")
        source = str(entry.get("source") or "").strip().lower()
        if source == "approved_profile_mapping":
            entry["source_label"] = "Approved profile mapping"
        elif source == "approved_alias_bundle":
            entry["source_label"] = "Approved alias bundle"
        elif source == "learned_observation_exact":
            entry["source_label"] = "Exact learned reuse"
        elif source == "approved_suppression_pattern":
            entry["source_label"] = "Approved repair hint"
        elif source == "openclaw":
            entry["source_label"] = "OpenClaw"
        else:
            entry["source_label"] = source.replace("_", " ").title() if source else "Local"

    summary = parsed["summary"] if isinstance(parsed["summary"], dict) else {}
    parsed["human_repair_action_count"] = _coerce_int(summary.get("human_repair_action_count"))
    parsed["manual_merge_group_count"] = _coerce_int(summary.get("manual_merge_group_count"))
    parsed["manual_suppression_group_count"] = _coerce_int(summary.get("manual_suppression_group_count"))
    parsed["repair_action_cards"] = [
        {"label": "Manual Adds", "count": parsed["manual_add_count"]},
        {"label": "Merge Actions", "count": parsed["manual_merge_group_count"]},
        {"label": "Suppress Actions", "count": parsed["manual_suppression_group_count"]},
        {"label": "Knowledge Captured", "count": parsed["knowledge_captured_count"]},
    ]

    parsed["openclaw_invocation_count"] = _coerce_int(summary.get("openclaw_invocation_count"))
    parsed["openclaw_total_latency_ms"] = _coerce_int(summary.get("openclaw_total_latency_ms"))
    try:
        parsed["openclaw_avg_latency_ms"] = float(summary.get("openclaw_avg_latency_ms") or 0.0)
    except Exception:
        parsed["openclaw_avg_latency_ms"] = 0.0
    parsed["openclaw_agents_used"] = summary.get("openclaw_agents_used") or []
    parsed["openclaw_model_labels"] = summary.get("openclaw_model_labels") or []
    parsed["openclaw_item_count_by_lane"] = summary.get("openclaw_item_count_by_lane") or {}
    parsed["openclaw_returned_by_lane"] = summary.get("openclaw_returned_by_lane") or {}
    parsed["openclaw_lane_cards"] = []
    for lane_key in OPENCLAW_LANE_LABELS:
        parsed["openclaw_lane_cards"].append(
            {
                "key": lane_key,
                "label": OPENCLAW_LANE_LABELS.get(lane_key, lane_key),
                "item_count": _coerce_int(parsed["openclaw_item_count_by_lane"].get(lane_key)),
                "returned_count": _coerce_int(parsed["openclaw_returned_by_lane"].get(lane_key)),
            }
        )

    parsed["ai_touched_item_count"] = _coerce_int(summary.get("ai_touched_item_count"))
    parsed["ai_accepted_item_count"] = _coerce_int(summary.get("ai_accepted_item_count"))
    parsed["ai_no_repair_item_count"] = _coerce_int(summary.get("ai_no_repair_item_count"))
    parsed["ai_repaired_after_item_count"] = _coerce_int(summary.get("ai_repaired_after_item_count"))
    parsed["ai_assisted_receipt"] = bool(summary.get("ai_assisted_receipt"))
    parsed["ai_acceptance_rate_pct"] = _metric_rate(
        parsed["ai_accepted_item_count"],
        parsed["ai_touched_item_count"],
    )
    parsed["ai_no_repair_rate_pct"] = _metric_rate(
        parsed["ai_no_repair_item_count"],
        parsed["ai_touched_item_count"],
    )
    parsed["ai_repaired_after_rate_pct"] = _metric_rate(
        parsed["ai_repaired_after_item_count"],
        parsed["ai_touched_item_count"],
    )
    parsed["ai_lane_outcomes"] = _merge_openclaw_lane_outcomes({}, summary.get("ai_lane_outcomes") or {})
    parsed["ai_lane_cards"] = _build_openclaw_lane_effectiveness_cards(parsed["ai_lane_outcomes"])
    parsed["ai_learning_item_count"] = _coerce_int(summary.get("ai_learning_item_count"))
    parsed["ai_learning_no_repair_item_count"] = _coerce_int(summary.get("ai_learning_no_repair_item_count"))
    parsed["ai_learning_capture_rate_pct"] = _metric_rate(
        parsed["ai_learning_item_count"],
        parsed["knowledge_captured_count"],
    )
    parsed["ai_learning_lane_outcomes"] = _merge_ai_learning_lane_outcomes({}, summary.get("ai_learning_lane_outcomes") or {})
    parsed["ai_learning_lane_cards"] = _build_ai_learning_lane_cards(parsed["ai_learning_lane_outcomes"])
    parsed["ai_learning_strategy_cards"] = summary.get("ai_learning_strategy_cards") or []

    for entry in parsed["line_decisions"]:
        if str(entry.get("source") or "").strip().lower() == "openclaw":
            entry["openclaw_lane_label"] = entry.get("openclaw_lane_label") or "OpenClaw lane"
            entry["openclaw_model_label"] = entry.get("openclaw_model_label") or None
            entry["openclaw_agent_id"] = entry.get("openclaw_agent_id") or None
            entry["openclaw_request_model"] = entry.get("openclaw_request_model") or None
            entry["ai_accepted"] = bool(entry.get("ai_accepted"))
            entry["ai_no_repair"] = bool(entry.get("ai_no_repair"))
            entry["ai_repaired_after"] = bool(entry.get("ai_repaired_after"))
            if parsed.get("metric_type") == "approval_outcome":
                if entry["ai_no_repair"]:
                    entry["approval_outcome_label"] = entry.get("approval_outcome_label") or "Accepted without later repair"
                elif entry["ai_repaired_after"]:
                    entry["approval_outcome_label"] = entry.get("approval_outcome_label") or "Later repaired by human"
                elif entry["ai_accepted"]:
                    entry["approval_outcome_label"] = entry.get("approval_outcome_label") or "Retained in approved outcome"
                else:
                    entry["approval_outcome_label"] = entry.get("approval_outcome_label") or "Not retained in approved outcome"

    return parsed


def _insert_receipt_run_metric(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    artifact_id: str | None,
    merchant_key: str | None,
    metric_type: str,
    source_run_id: int | None = None,
    total_items: int = 0,
    active_item_count: int = 0,
    suppressed_item_count: int = 0,
    total_review_items: int = 0,
    local_mapping_hints: int = 0,
    local_repair_hints: int = 0,
    locally_handled_before_openclaw: int = 0,
    remaining_for_openclaw: int = 0,
    openclaw_called: int = 0,
    openclaw_returned: int = 0,
    total_suggestions_returned: int = 0,
    local_profile_mapping_count: int = 0,
    local_alias_bundle_count: int = 0,
    local_exact_reuse_count: int = 0,
    local_repair_hint_count: int = 0,
    openclaw_suggested_count: int = 0,
    no_suggestion_count: int = 0,
    manual_add_count: int = 0,
    manual_merge_source_count: int = 0,
    manual_suppression_count: int = 0,
    knowledge_captured_count: int = 0,
    summary: dict | None = None,
    decision_breakdown: dict | None = None,
    line_decisions: list[dict] | None = None,
) -> dict:
    now = _utcnow_iso()
    summary_payload = summary or {}
    decision_payload = decision_breakdown or {}
    line_payload = line_decisions or []

    cur = conn.execute(
        """
        INSERT INTO receipt_run_metrics (
            receipt_id,
            artifact_id,
            merchant_key,
            metric_type,
            source_run_id,
            total_items,
            active_item_count,
            suppressed_item_count,
            total_review_items,
            local_mapping_hints,
            local_repair_hints,
            locally_handled_before_openclaw,
            remaining_for_openclaw,
            openclaw_called,
            openclaw_returned,
            total_suggestions_returned,
            local_profile_mapping_count,
            local_alias_bundle_count,
            local_exact_reuse_count,
            local_repair_hint_count,
            openclaw_suggested_count,
            no_suggestion_count,
            manual_add_count,
            manual_merge_source_count,
            manual_suppression_count,
            knowledge_captured_count,
            summary_json,
            decision_breakdown_json,
            line_decisions_json,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt_id,
            artifact_id,
            merchant_key,
            metric_type,
            source_run_id,
            total_items,
            active_item_count,
            suppressed_item_count,
            total_review_items,
            local_mapping_hints,
            local_repair_hints,
            locally_handled_before_openclaw,
            remaining_for_openclaw,
            openclaw_called,
            openclaw_returned,
            total_suggestions_returned,
            local_profile_mapping_count,
            local_alias_bundle_count,
            local_exact_reuse_count,
            local_repair_hint_count,
            openclaw_suggested_count,
            no_suggestion_count,
            manual_add_count,
            manual_merge_source_count,
            manual_suppression_count,
            knowledge_captured_count,
            _stable_json(summary_payload),
            _stable_json(decision_payload),
            _stable_json(line_payload),
            now,
            now,
        ),
    )

    row = {
        "id": cur.lastrowid,
        "receipt_id": receipt_id,
        "artifact_id": artifact_id,
        "merchant_key": merchant_key,
        "metric_type": metric_type,
        "source_run_id": source_run_id,
        "total_items": total_items,
        "active_item_count": active_item_count,
        "suppressed_item_count": suppressed_item_count,
        "total_review_items": total_review_items,
        "local_mapping_hints": local_mapping_hints,
        "local_repair_hints": local_repair_hints,
        "locally_handled_before_openclaw": locally_handled_before_openclaw,
        "remaining_for_openclaw": remaining_for_openclaw,
        "openclaw_called": openclaw_called,
        "openclaw_returned": openclaw_returned,
        "total_suggestions_returned": total_suggestions_returned,
        "local_profile_mapping_count": local_profile_mapping_count,
        "local_alias_bundle_count": local_alias_bundle_count,
        "local_exact_reuse_count": local_exact_reuse_count,
        "local_repair_hint_count": local_repair_hint_count,
        "openclaw_suggested_count": openclaw_suggested_count,
        "no_suggestion_count": no_suggestion_count,
        "manual_add_count": manual_add_count,
        "manual_merge_source_count": manual_merge_source_count,
        "manual_suppression_count": manual_suppression_count,
        "knowledge_captured_count": knowledge_captured_count,
        "summary_json": _stable_json(summary_payload),
        "decision_breakdown_json": _stable_json(decision_payload),
        "line_decisions_json": _stable_json(line_payload),
        "created_at": now,
        "updated_at": now,
    }

    return _parse_receipt_run_metric_row(
        conn.execute("SELECT * FROM receipt_run_metrics WHERE id = ?", (cur.lastrowid,)).fetchone()
    )


def get_latest_receipt_metric(
    conn: sqlite3.Connection,
    receipt_id: int,
    *,
    metric_type: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT *
        FROM receipt_run_metrics
        WHERE receipt_id = ?
          AND metric_type = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (receipt_id, metric_type),
    ).fetchone()
    return _parse_receipt_run_metric_row(row)


def _parse_openclaw_invocation_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None

    parsed = {key: row[key] for key in row.keys()}
    parsed["id"] = _coerce_int(parsed.get("id"))
    parsed["receipt_id"] = _coerce_int(parsed.get("receipt_id"))
    parsed["source_run_id"] = _coerce_int(parsed.get("source_run_id")) or None
    parsed["item_count"] = _coerce_int(parsed.get("item_count"))
    parsed["returned_count"] = _coerce_int(parsed.get("returned_count"))
    parsed["duration_ms"] = _coerce_int(parsed.get("duration_ms"))
    parsed["success"] = bool(_coerce_int(parsed.get("success")))
    parsed["line_numbers"] = _json_loads_safe(parsed.get("line_numbers_json"), [])
    parsed["lane_label"] = parsed.get("lane_label") or (str(parsed.get("lane") or "").replace("_", " ").title() if parsed.get("lane") else "OpenClaw lane")
    return parsed


def record_openclaw_invocations(
    conn: sqlite3.Connection,
    receipt: sqlite3.Row | dict,
    invocations: list[dict] | None,
    *,
    source_run_id: int | None = None,
) -> list[dict]:
    invocations = list(invocations or [])
    if not invocations:
        return []

    merchant_key = canonicalize_merchant_key(
        _row_value(receipt, "merchant_canonical") or _row_value(receipt, "merchant_raw")
    )
    artifact_id = _row_value(receipt, "artifact_id")
    receipt_id = int(_row_value(receipt, "id"))
    now = _utcnow_iso()

    rows: list[dict] = []
    for invocation in invocations:
        line_numbers = invocation.get("line_numbers") or []
        cur = conn.execute(
            """
            INSERT INTO openclaw_invocations (
                receipt_id,
                artifact_id,
                merchant_key,
                source_run_id,
                lane,
                lane_label,
                agent_id,
                model_label,
                request_model,
                temperature,
                max_tokens,
                item_count,
                line_numbers_json,
                returned_count,
                duration_ms,
                success,
                error_message,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                artifact_id,
                merchant_key,
                source_run_id,
                invocation.get("lane"),
                invocation.get("lane_label"),
                invocation.get("agent_id"),
                invocation.get("model_label"),
                invocation.get("request_model"),
                invocation.get("temperature"),
                invocation.get("max_tokens"),
                _coerce_int(invocation.get("item_count")),
                _stable_json(line_numbers),
                _coerce_int(invocation.get("returned_count")),
                _coerce_int(invocation.get("duration_ms")),
                _coerce_bool_int(invocation.get("success", True)),
                invocation.get("error_message"),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM openclaw_invocations WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        rows.append(_parse_openclaw_invocation_row(row))

    return rows


def list_receipt_openclaw_invocations(
    conn: sqlite3.Connection,
    receipt_id: int,
    *,
    source_run_id: int | None = None,
    limit: int = 8,
) -> list[dict]:
    normalized_limit = max(1, int(limit))
    if source_run_id is None:
        rows = conn.execute(
            """
            SELECT *
            FROM openclaw_invocations
            WHERE receipt_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (receipt_id, normalized_limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT *
            FROM openclaw_invocations
            WHERE receipt_id = ?
              AND source_run_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (receipt_id, source_run_id, normalized_limit),
        ).fetchall()
    return [_parse_openclaw_invocation_row(row) for row in rows]


def get_receipt_decision_telemetry(
    conn: sqlite3.Connection,
    receipt_id: int,
) -> dict:
    latest_suggestion_run = get_latest_receipt_metric(
        conn,
        receipt_id,
        metric_type="suggestion_run",
    )
    latest_approval_outcome = get_latest_receipt_metric(
        conn,
        receipt_id,
        metric_type="approval_outcome",
    )
    latest_openclaw_invocations = list_receipt_openclaw_invocations(
        conn,
        receipt_id,
        source_run_id=latest_suggestion_run.get("id") if latest_suggestion_run else None,
        limit=8,
    )
    return {
        "latest_suggestion_run": latest_suggestion_run,
        "latest_approval_outcome": latest_approval_outcome,
        "latest_openclaw_invocations": latest_openclaw_invocations,
    }


def record_receipt_suggestion_run_metrics(
    conn: sqlite3.Connection,
    receipt: sqlite3.Row | dict,
    items: list[sqlite3.Row | dict],
    suggestions_by_item_id: dict | None,
    suggestion_stats: dict | None,
    openclaw_invocations: list[dict] | None = None,
) -> dict:
    merchant_key = canonicalize_merchant_key(
        _row_value(receipt, "merchant_canonical") or _row_value(receipt, "merchant_raw")
    )
    artifact_id = _row_value(receipt, "artifact_id")
    receipt_id = int(_row_value(receipt, "id"))

    suggestions_by_item_id = suggestions_by_item_id or {}
    suggestion_stats = suggestion_stats or {}

    total_items = len(items)
    active_items = [item for item in items if int(item.get("is_suppressed") if isinstance(item, dict) else item["is_suppressed"] or 0) != 1]
    suppressed_item_count = total_items - len(active_items)
    review_items = [item for item in active_items if int(item.get("needs_review") if isinstance(item, dict) else item["needs_review"] or 0) == 1]

    line_decisions: list[dict] = []
    for item in review_items:
        item_id = int(item.get("id") if isinstance(item, dict) else item["id"])
        line_number = int(item.get("line_number") if isinstance(item, dict) else item["line_number"])
        suggestion = suggestions_by_item_id.get(item_id)
        decision_path = _classify_suggestion_decision_path(suggestion)
        line_decisions.append(
            {
                "item_id": item_id,
                "line_number": line_number,
                "item_text_raw": item.get("item_text_raw") if isinstance(item, dict) else item["item_text_raw"],
                "decision_path": decision_path,
                "source": suggestion.get("source") if suggestion else None,
                "confidence": suggestion.get("confidence") if suggestion else None,
                "suggested_action": suggestion.get("suggested_action") if suggestion else None,
                "suggested_name": suggestion.get("suggested_name") if suggestion else None,
                "suggested_category": suggestion.get("suggested_category") if suggestion else None,
                "openclaw_lane": suggestion.get("openclaw_lane") if suggestion else None,
                "openclaw_lane_label": suggestion.get("openclaw_lane_label") if suggestion else None,
                "openclaw_agent_id": suggestion.get("openclaw_agent_id") if suggestion else None,
                "openclaw_model_label": suggestion.get("openclaw_model_label") if suggestion else None,
                "openclaw_request_model": suggestion.get("openclaw_request_model") if suggestion else None,
            }
        )

    decision_breakdown = _build_decision_breakdown_from_line_decisions(line_decisions)
    total_review_items = _coerce_int(suggestion_stats.get("total_review_items")) or len(review_items)
    local_mapping_hints = _coerce_int(
        suggestion_stats.get("local_mapping_hints", suggestion_stats.get("learned_exact_matches"))
    )
    local_repair_hints = _coerce_int(suggestion_stats.get("local_repair_hints"))
    locally_handled = _coerce_int(
        suggestion_stats.get(
            "locally_handled_before_openclaw",
            local_mapping_hints + local_repair_hints,
        )
    )
    remaining_for_openclaw = _coerce_int(suggestion_stats.get("remaining_for_openclaw"))
    openclaw_called = _coerce_bool_int(suggestion_stats.get("openclaw_called"))
    openclaw_returned = _coerce_int(suggestion_stats.get("openclaw_returned"))
    total_suggestions_returned = _coerce_int(suggestion_stats.get("total_suggestions_returned"))

    openclaw_invocations = list(openclaw_invocations or suggestion_stats.get("openclaw_telemetry") or [])

    summary = {
        "has_run": bool(suggestion_stats.get("has_run")),
        "fully_covered_locally": bool(suggestion_stats.get("fully_covered_locally")),
        "fully_covered_by_learned": bool(suggestion_stats.get("fully_covered_by_learned")),
        "local_resolution_rate_pct": _metric_rate(locally_handled, total_review_items),
        "openclaw_routed_rate_pct": _metric_rate(remaining_for_openclaw, total_review_items),
        "line_decision_count": len(line_decisions),
        "openclaw_invocation_count": _coerce_int(suggestion_stats.get("openclaw_invocation_count", len(openclaw_invocations))),
        "openclaw_total_latency_ms": _coerce_int(suggestion_stats.get("openclaw_total_latency_ms")),
        "openclaw_avg_latency_ms": suggestion_stats.get("openclaw_avg_latency_ms", 0.0),
        "openclaw_agents_used": suggestion_stats.get("openclaw_agents_used") or [],
        "openclaw_model_labels": suggestion_stats.get("openclaw_model_labels") or [],
        "openclaw_item_count_by_lane": suggestion_stats.get("openclaw_item_count_by_lane") or {},
        "openclaw_returned_by_lane": suggestion_stats.get("openclaw_returned_by_lane") or {},
    }

    metrics_row = _insert_receipt_run_metric(
        conn,
        receipt_id=receipt_id,
        artifact_id=artifact_id,
        merchant_key=merchant_key,
        metric_type="suggestion_run",
        total_items=total_items,
        active_item_count=len(active_items),
        suppressed_item_count=suppressed_item_count,
        total_review_items=total_review_items,
        local_mapping_hints=local_mapping_hints,
        local_repair_hints=local_repair_hints,
        locally_handled_before_openclaw=locally_handled,
        remaining_for_openclaw=remaining_for_openclaw,
        openclaw_called=openclaw_called,
        openclaw_returned=openclaw_returned,
        total_suggestions_returned=total_suggestions_returned,
        local_profile_mapping_count=_coerce_int(decision_breakdown.get("local_profile_mapping")),
        local_alias_bundle_count=_coerce_int(decision_breakdown.get("local_alias_bundle")),
        local_exact_reuse_count=_coerce_int(decision_breakdown.get("local_exact_reuse")),
        local_repair_hint_count=_coerce_int(decision_breakdown.get("local_repair_hint")),
        openclaw_suggested_count=_coerce_int(decision_breakdown.get("openclaw_suggested")),
        no_suggestion_count=_coerce_int(decision_breakdown.get("no_suggestion")),
        summary=summary,
        decision_breakdown=decision_breakdown,
        line_decisions=line_decisions,
    )

    record_openclaw_invocations(
        conn,
        receipt,
        openclaw_invocations,
        source_run_id=metrics_row.get("id"),
    )
    return metrics_row


def record_receipt_approval_outcome_metrics(
    conn: sqlite3.Connection,
    receipt_id: int,
) -> dict | None:
    receipt = conn.execute(
        """
        SELECT id, artifact_id, merchant_raw, merchant_canonical
        FROM receipts
        WHERE id = ?
        """,
        (receipt_id,),
    ).fetchone()
    if receipt is None:
        return None

    items = conn.execute(
        """
        SELECT id, line_number, item_name_normalized, category, is_suppressed, needs_review
        FROM receipt_items
        WHERE receipt_id = ?
        ORDER BY line_number, id
        """,
        (receipt_id,),
    ).fetchall()

    latest_run = get_latest_receipt_metric(conn, receipt_id, metric_type="suggestion_run")

    repair_rows = conn.execute(
        """
        SELECT observation_type, receipt_item_id, evidence_json
        FROM learning_observations
        WHERE receipt_id = ?
          AND status = 'observed'
          AND observation_type IN ('repair_manual_add', 'repair_merge_source', 'repair_suppression', 'approved_item_observation')
        ORDER BY id
        """,
        (receipt_id,),
    ).fetchall()

    manual_add_count = 0
    manual_merge_source_count = 0
    manual_suppression_count = 0
    knowledge_captured_count = 0
    merge_group_ids: set[int] = set()
    suppression_group_keys: set[str] = set()

    for row in repair_rows:
        observation_type = row["observation_type"]
        evidence = _json_loads_safe(row["evidence_json"], {})

        if observation_type == "repair_manual_add":
            manual_add_count += 1
        elif observation_type == "repair_merge_source":
            manual_merge_source_count += 1
            merged_item_id = _coerce_int(evidence.get("merged_item_id"))
            if merged_item_id > 0:
                merge_group_ids.add(merged_item_id)
            else:
                source_item_ids = evidence.get("source_item_ids") or []
                if source_item_ids:
                    merge_group_ids.add(hash(tuple(source_item_ids)))
        elif observation_type == "repair_suppression":
            manual_suppression_count += 1
            suppression_key = str(evidence.get("suppression_reason") or evidence.get("source_line_number") or row["evidence_json"] or "")
            if suppression_key:
                suppression_group_keys.add(suppression_key)
        elif observation_type == "approved_item_observation":
            knowledge_captured_count += 1

    total_items = len(items)
    active_item_count = sum(1 for item in items if int(item["is_suppressed"] or 0) != 1)
    suppressed_item_count = total_items - active_item_count
    total_review_items = sum(
        1
        for item in items
        if int(item["is_suppressed"] or 0) != 1 and int(item["needs_review"] or 0) == 1
    )

    ai_effectiveness = _build_openclaw_effectiveness_from_approval(latest_run, items, repair_rows)
    ai_learning_capture = annotate_approved_item_observations_with_ai_provenance(
        conn,
        receipt_id,
        ai_effectiveness.get("line_decisions") or [],
    )

    summary = {
        "latest_suggestion_run_available": bool(latest_run),
        "latest_suggestion_run_id": latest_run.get("id") if latest_run else None,
        "manual_merge_group_count": len(merge_group_ids),
        "manual_suppression_group_count": len(suppression_group_keys),
        "human_repair_action_count": manual_add_count + len(merge_group_ids) + len(suppression_group_keys),
        "active_item_count": active_item_count,
        "suppressed_item_count": suppressed_item_count,
        "total_item_count": total_items,
        "outstanding_review_item_count": total_review_items,
        "ai_touched_item_count": ai_effectiveness["ai_touched_item_count"],
        "ai_accepted_item_count": ai_effectiveness["ai_accepted_item_count"],
        "ai_no_repair_item_count": ai_effectiveness["ai_no_repair_item_count"],
        "ai_repaired_after_item_count": ai_effectiveness["ai_repaired_after_item_count"],
        "ai_assisted_receipt": ai_effectiveness["ai_assisted_receipt"],
        "ai_lane_outcomes": ai_effectiveness["ai_lane_outcomes"],
        "ai_learning_item_count": ai_learning_capture["captured_count"],
        "ai_learning_no_repair_item_count": ai_learning_capture["no_repair_count"],
        "ai_learning_lane_outcomes": ai_learning_capture["lane_outcomes"],
        "ai_learning_strategy_cards": ai_learning_capture["strategy_cards"],
    }

    if latest_run:
        summary["local_resolution_rate_pct"] = latest_run.get("local_resolution_rate_pct", 0.0)
        summary["openclaw_routed_rate_pct"] = latest_run.get("openclaw_routed_rate_pct", 0.0)

    merchant_key = canonicalize_merchant_key(receipt["merchant_canonical"] or receipt["merchant_raw"])

    return _insert_receipt_run_metric(
        conn,
        receipt_id=receipt_id,
        artifact_id=receipt["artifact_id"],
        merchant_key=merchant_key,
        metric_type="approval_outcome",
        source_run_id=latest_run.get("id") if latest_run else None,
        total_items=total_items,
        active_item_count=active_item_count,
        suppressed_item_count=suppressed_item_count,
        total_review_items=total_review_items,
        local_mapping_hints=latest_run.get("local_mapping_hints", 0) if latest_run else 0,
        local_repair_hints=latest_run.get("local_repair_hints", 0) if latest_run else 0,
        locally_handled_before_openclaw=latest_run.get("locally_handled_before_openclaw", 0) if latest_run else 0,
        remaining_for_openclaw=latest_run.get("remaining_for_openclaw", 0) if latest_run else 0,
        openclaw_called=latest_run.get("openclaw_called", 0) if latest_run else 0,
        openclaw_returned=latest_run.get("openclaw_returned", 0) if latest_run else 0,
        total_suggestions_returned=latest_run.get("total_suggestions_returned", 0) if latest_run else 0,
        local_profile_mapping_count=latest_run.get("local_profile_mapping_count", 0) if latest_run else 0,
        local_alias_bundle_count=latest_run.get("local_alias_bundle_count", 0) if latest_run else 0,
        local_exact_reuse_count=latest_run.get("local_exact_reuse_count", 0) if latest_run else 0,
        local_repair_hint_count=latest_run.get("local_repair_hint_count", 0) if latest_run else 0,
        openclaw_suggested_count=latest_run.get("openclaw_suggested_count", 0) if latest_run else 0,
        no_suggestion_count=latest_run.get("no_suggestion_count", 0) if latest_run else 0,
        manual_add_count=manual_add_count,
        manual_merge_source_count=manual_merge_source_count,
        manual_suppression_count=manual_suppression_count,
        knowledge_captured_count=knowledge_captured_count,
        summary=summary,
        decision_breakdown=latest_run.get("decision_breakdown", {}) if latest_run else {},
        line_decisions=ai_effectiveness.get("line_decisions", []) if latest_run else [],
    )


def _load_latest_metrics_by_receipt(
    conn: sqlite3.Connection,
    *,
    metric_type: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM receipt_run_metrics
        WHERE metric_type = ?
        ORDER BY receipt_id, id DESC
        """,
        (metric_type,),
    ).fetchall()

    latest_by_receipt: dict[int, dict] = {}
    for row in rows:
        receipt_id = _coerce_int(row["receipt_id"])
        if receipt_id in latest_by_receipt:
            continue
        latest_by_receipt[receipt_id] = _parse_receipt_run_metric_row(row)

    return list(latest_by_receipt.values())


def _insert_learning_observation(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    receipt_item_id: int | None,
    artifact_id: str | None,
    merchant_key: str,
    observation_type: str,
    raw_key: str | None,
    normalized_value: str | None,
    category_value: str | None,
    confidence: float | None,
    evidence: dict,
    status: str = "observed",
) -> None:
    now = _utcnow_iso()

    conn.execute(
        """
        INSERT INTO learning_observations (
            receipt_id,
            receipt_item_id,
            artifact_id,
            merchant_key,
            observation_type,
            raw_key,
            normalized_value,
            category_value,
            confidence,
            evidence_json,
            status,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt_id,
            receipt_item_id,
            artifact_id,
            merchant_key,
            observation_type,
            raw_key,
            normalized_value,
            category_value,
            confidence,
            _stable_json(evidence),
            status,
            now,
            now,
        ),
    )


def remove_observations_for_receipt(
    conn: sqlite3.Connection,
    receipt_id: int,
    observation_type: str | None = None,
) -> int:
    if observation_type:
        cur = conn.execute(
            """
            DELETE FROM learning_observations
            WHERE receipt_id = ?
              AND observation_type = ?
            """,
            (receipt_id, observation_type),
        )
    else:
        cur = conn.execute(
            """
            DELETE FROM learning_observations
            WHERE receipt_id = ?
            """,
            (receipt_id,),
        )

    return cur.rowcount or 0


def capture_repair_manual_add_observation(
    conn: sqlite3.Connection,
    receipt: sqlite3.Row | dict,
    *,
    item_id: int,
    line_number: int,
    item_text_raw: str,
    item_name_normalized: str,
    category: str,
    line_total: float | None,
    review_notes: str | None,
) -> int:
    merchant_key = canonicalize_merchant_key(
        receipt["merchant_canonical"] or receipt["merchant_raw"]
    )
    raw_key = normalize_raw_key(item_text_raw)

    if not merchant_key or not raw_key:
        return 0

    evidence = {
        "action": "manual_add",
        "receipt_id": int(receipt["id"]),
        "receipt_item_id": int(item_id),
        "artifact_id": receipt["artifact_id"],
        "merchant_raw": receipt["merchant_raw"],
        "merchant_canonical": receipt["merchant_canonical"],
        "line_number": int(line_number),
        "item_text_raw": item_text_raw,
        "item_name_normalized": item_name_normalized,
        "category": category,
        "line_total": line_total,
        "review_notes": review_notes,
    }

    _insert_learning_observation(
        conn,
        receipt_id=int(receipt["id"]),
        receipt_item_id=int(item_id),
        artifact_id=receipt["artifact_id"],
        merchant_key=merchant_key,
        observation_type="repair_manual_add",
        raw_key=raw_key,
        normalized_value=(item_name_normalized or "").strip() or None,
        category_value=(category or "").strip() or None,
        confidence=1.0,
        evidence=evidence,
    )
    return 1


def capture_repair_merge_observations(
    conn: sqlite3.Connection,
    receipt: sqlite3.Row | dict,
    *,
    source_rows: list[sqlite3.Row | dict],
    merged_item_id: int,
    merged_line_number: int,
    merged_item_text_raw: str,
    merged_item_name_normalized: str,
    merged_category: str,
    suppression_reason: str,
) -> int:
    merchant_key = canonicalize_merchant_key(
        receipt["merchant_canonical"] or receipt["merchant_raw"]
    )
    if not merchant_key:
        return 0

    source_item_ids = [int(row["id"]) for row in source_rows]
    inserted = 0

    for row in source_rows:
        raw_key = normalize_raw_key(row["item_text_raw"])
        if not raw_key:
            continue

        evidence = {
            "action": "merge_source",
            "receipt_id": int(receipt["id"]),
            "receipt_item_id": int(row["id"]),
            "artifact_id": receipt["artifact_id"],
            "merchant_raw": receipt["merchant_raw"],
            "merchant_canonical": receipt["merchant_canonical"],
            "source_item_id": int(row["id"]),
            "source_line_number": int(row["line_number"]),
            "source_item_text_raw": row["item_text_raw"],
            "source_item_name_normalized": row["item_name_normalized"],
            "source_category": row["category"],
            "merged_item_id": int(merged_item_id),
            "merged_line_number": int(merged_line_number),
            "merged_item_text_raw": merged_item_text_raw,
            "merged_item_name_normalized": merged_item_name_normalized,
            "merged_category": merged_category,
            "source_item_ids": source_item_ids,
            "suppression_reason": suppression_reason,
        }

        _insert_learning_observation(
            conn,
            receipt_id=int(receipt["id"]),
            receipt_item_id=int(row["id"]),
            artifact_id=receipt["artifact_id"],
            merchant_key=merchant_key,
            observation_type="repair_merge_source",
            raw_key=raw_key,
            normalized_value=(merged_item_name_normalized or "").strip() or None,
            category_value=(merged_category or "").strip() or None,
            confidence=row["item_confidence"],
            evidence=evidence,
        )
        inserted += 1

    return inserted


def capture_repair_suppression_observations(
    conn: sqlite3.Connection,
    receipt: sqlite3.Row | dict,
    *,
    source_rows: list[sqlite3.Row | dict],
    suppression_reason: str,
) -> int:
    merchant_key = canonicalize_merchant_key(
        receipt["merchant_canonical"] or receipt["merchant_raw"]
    )
    if not merchant_key:
        return 0

    inserted = 0

    for row in source_rows:
        raw_key = normalize_raw_key(row["item_text_raw"])
        if not raw_key:
            continue

        evidence = {
            "action": "suppress",
            "receipt_id": int(receipt["id"]),
            "receipt_item_id": int(row["id"]),
            "artifact_id": receipt["artifact_id"],
            "merchant_raw": receipt["merchant_raw"],
            "merchant_canonical": receipt["merchant_canonical"],
            "source_item_id": int(row["id"]),
            "source_line_number": int(row["line_number"]),
            "source_item_text_raw": row["item_text_raw"],
            "source_item_name_normalized": row["item_name_normalized"],
            "source_category": row["category"],
            "suppression_reason": suppression_reason,
        }

        _insert_learning_observation(
            conn,
            receipt_id=int(receipt["id"]),
            receipt_item_id=int(row["id"]),
            artifact_id=receipt["artifact_id"],
            merchant_key=merchant_key,
            observation_type="repair_suppression",
            raw_key=raw_key,
            normalized_value=(row["item_name_normalized"] or "").strip() or None,
            category_value=(row["category"] or "").strip() or None,
            confidence=row["item_confidence"],
            evidence=evidence,
        )
        inserted += 1

    return inserted


def capture_approved_observations_for_receipt(
    conn: sqlite3.Connection,
    receipt_id: int,
    item_id: int | None = None,
) -> int:
    receipt = conn.execute(
        """
        SELECT *
        FROM receipts
        WHERE id = ?
        """,
        (receipt_id,),
    ).fetchone()

    if receipt is None:
        return 0

    merchant_key = canonicalize_merchant_key(
        receipt["merchant_canonical"] or receipt["merchant_raw"]
    )
    if not merchant_key:
        return 0

    if item_id is None:
        conn.execute(
            """
            DELETE FROM learning_observations
            WHERE receipt_id = ?
              AND observation_type = 'approved_item_observation'
            """,
            (receipt_id,),
        )

        items = conn.execute(
            """
            SELECT *
            FROM receipt_items
            WHERE receipt_id = ?
              AND COALESCE(is_suppressed, 0) = 0
            ORDER BY line_number, id
            """,
            (receipt_id,),
        ).fetchall()
    else:
        conn.execute(
            """
            DELETE FROM learning_observations
            WHERE receipt_id = ?
              AND receipt_item_id = ?
              AND observation_type = 'approved_item_observation'
            """,
            (receipt_id, item_id),
        )

        items = conn.execute(
            """
            SELECT *
            FROM receipt_items
            WHERE receipt_id = ?
              AND id = ?
              AND COALESCE(is_suppressed, 0) = 0
            ORDER BY line_number, id
            """,
            (receipt_id, item_id),
        ).fetchall()

    inserted = 0

    for item in items:
        raw_key = normalize_raw_key(item["item_text_raw"])
        normalized_value = (item["item_name_normalized"] or "").strip()
        category_value = (item["category"] or "").strip()

        if not raw_key or not normalized_value or not category_value:
            continue

        evidence = {
            "receipt_id": receipt_id,
            "receipt_item_id": item["id"],
            "artifact_id": receipt["artifact_id"],
            "merchant_raw": receipt["merchant_raw"],
            "merchant_canonical": receipt["merchant_canonical"],
            "line_number": item["line_number"],
            "item_text_raw": item["item_text_raw"],
            "item_name_normalized": item["item_name_normalized"],
            "category": item["category"],
            "category_source_raw": item["category_source_raw"],
            "is_suppressed": int(item["is_suppressed"] or 0),
            "suppression_reason": item["suppression_reason"],
        }

        _insert_learning_observation(
            conn,
            receipt_id=receipt_id,
            receipt_item_id=item["id"],
            artifact_id=receipt["artifact_id"],
            merchant_key=merchant_key,
            observation_type="approved_item_observation",
            raw_key=raw_key,
            normalized_value=normalized_value,
            category_value=category_value,
            confidence=item["item_confidence"],
            evidence=evidence,
        )
        inserted += 1

    return inserted


def _load_active_profile_item_mappings(
    conn: sqlite3.Connection,
    merchant_key: str,
) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT
            id,
            confidence,
            evidence_count,
            proposal_json,
            updated_at
        FROM profile_proposals
        WHERE proposal_type = 'item_mapping_bundle'
          AND status = 'approved'
          AND merchant_scope = ?
        ORDER BY
            evidence_count DESC,
            updated_at DESC,
            id DESC
        """,
        (merchant_key,),
    ).fetchall()

    approved_by_raw_key: dict[str, dict] = {}
    conflicted_raw_keys: set[str] = set()

    for row in rows:
        try:
            payload = json.loads(row["proposal_json"] or "{}")
        except Exception:
            continue

        mapping = payload.get("mapping") or {}
        evidence = payload.get("evidence") or {}

        raw_key = normalize_raw_key(mapping.get("raw_key"))
        approved_name = (mapping.get("approved_name") or "").strip()
        approved_category = (mapping.get("approved_category") or "").strip()

        if not raw_key or not approved_name or not approved_category:
            continue

        distinct_receipt_count = int(
            evidence.get("distinct_receipt_count")
            or row["evidence_count"]
            or 0
        )
        distinct_artifact_count = int(
            evidence.get("distinct_artifact_count")
            or 0
        )

        candidate = {
            "proposal_id": int(row["id"]),
            "suggested_name": approved_name,
            "suggested_category": approved_category,
            "matched_observation_count": distinct_receipt_count,
            "distinct_artifact_count": distinct_artifact_count,
            "confidence": "high",
        }

        existing = approved_by_raw_key.get(raw_key)
        if existing is None:
            approved_by_raw_key[raw_key] = candidate
            continue

        same_mapping = (
            existing["suggested_name"] == candidate["suggested_name"]
            and existing["suggested_category"] == candidate["suggested_category"]
        )

        if same_mapping:
            continue

        conflicted_raw_keys.add(raw_key)

    for raw_key in conflicted_raw_keys:
        approved_by_raw_key.pop(raw_key, None)

    return approved_by_raw_key


def _load_active_profile_alias_mappings(
    conn: sqlite3.Connection,
    merchant_key: str,
) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT
            id,
            confidence,
            evidence_count,
            proposal_json,
            updated_at
        FROM profile_proposals
        WHERE proposal_type = 'item_alias_bundle'
          AND status = 'approved'
          AND merchant_scope = ?
        ORDER BY
            evidence_count DESC,
            updated_at DESC,
            id DESC
        """,
        (merchant_key,),
    ).fetchall()

    alias_by_raw_key: dict[str, dict] = {}
    conflicted_raw_keys: set[str] = set()

    for row in rows:
        try:
            payload = json.loads(row["proposal_json"] or "{}")
        except Exception:
            continue

        mapping = payload.get("mapping") or {}
        evidence = payload.get("evidence") or {}
        raw_variants = payload.get("raw_variants") or []

        approved_name = (mapping.get("approved_name") or "").strip()
        approved_category = (mapping.get("approved_category") or "").strip()

        if not approved_name or not approved_category or not raw_variants:
            continue

        distinct_receipt_count = int(
            evidence.get("distinct_receipt_count")
            or row["evidence_count"]
            or 0
        )
        distinct_artifact_count = int(
            evidence.get("distinct_artifact_count")
            or 0
        )

        for raw_variant in raw_variants:
            raw_key = normalize_raw_key(raw_variant)
            if not raw_key:
                continue

            candidate = {
                "proposal_id": int(row["id"]),
                "suggested_name": approved_name,
                "suggested_category": approved_category,
                "matched_observation_count": distinct_receipt_count,
                "distinct_artifact_count": distinct_artifact_count,
                "raw_variant_count": len(raw_variants),
                "confidence": "high" if distinct_receipt_count >= 3 else "medium",
            }

            existing = alias_by_raw_key.get(raw_key)
            if existing is None:
                alias_by_raw_key[raw_key] = candidate
                continue

            same_mapping = (
                existing["suggested_name"] == candidate["suggested_name"]
                and existing["suggested_category"] == candidate["suggested_category"]
            )

            if same_mapping:
                continue

            conflicted_raw_keys.add(raw_key)

    for raw_key in conflicted_raw_keys:
        alias_by_raw_key.pop(raw_key, None)

    return alias_by_raw_key



def _load_active_profile_suppression_patterns(
    conn: sqlite3.Connection,
    merchant_key: str,
) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT
            id,
            confidence,
            evidence_count,
            proposal_json,
            updated_at
        FROM profile_proposals
        WHERE proposal_type = 'suppression_pattern'
          AND status = 'approved'
          AND merchant_scope = ?
        ORDER BY
            evidence_count DESC,
            updated_at DESC,
            id DESC
        """,
        (merchant_key,),
    ).fetchall()

    patterns_by_key: dict[str, dict] = {}

    for row in rows:
        try:
            payload = json.loads(row["proposal_json"] or "{}")
        except Exception:
            continue

        pattern = payload.get("pattern") or {}
        evidence = payload.get("evidence") or {}

        pattern_key = (pattern.get("pattern_key") or "").strip()
        hint_label = (pattern.get("hint_label") or "Likely fragment line").strip()
        suggested_action = (pattern.get("suggested_action") or "suppress").strip().lower()

        if not pattern_key or suggested_action != "suppress":
            continue

        candidate = {
            "proposal_id": int(row["id"]),
            "pattern_key": pattern_key,
            "pattern_kind": pattern.get("pattern_kind"),
            "signature": pattern.get("signature"),
            "hint_label": hint_label,
            "suggested_action": "suppress",
            "matched_observation_count": int(
                evidence.get("distinct_receipt_count")
                or row["evidence_count"]
                or 0
            ),
            "distinct_artifact_count": int(evidence.get("distinct_artifact_count") or 0),
            "raw_variant_count": len(payload.get("raw_variants") or []),
            "source_observation_counts": evidence.get("source_observation_counts") or {},
            "source_receipt_counts": evidence.get("source_receipt_counts") or {},
            "source_artifact_counts": evidence.get("source_artifact_counts") or {},
            "confidence": row["confidence"] or "medium",
            "example_text": pattern.get("example_text") or None,
        }

        if pattern_key not in patterns_by_key:
            patterns_by_key[pattern_key] = candidate

    return patterns_by_key


def _get_exact_observation_suggestion(
    conn: sqlite3.Connection,
    merchant_key: str,
    raw_key: str,
    line_number: int | None,
) -> dict | None:
    rows = conn.execute(
        """
        SELECT
            normalized_value,
            category_value,
            COUNT(*) AS observation_count,
            COUNT(DISTINCT receipt_id) AS distinct_receipt_count,
            COUNT(DISTINCT artifact_id) AS distinct_artifact_count
        FROM learning_observations
        WHERE observation_type = 'approved_item_observation'
          AND status = 'observed'
          AND merchant_key = ?
          AND raw_key = ?
        GROUP BY normalized_value, category_value
        ORDER BY
            distinct_receipt_count DESC,
            distinct_artifact_count DESC,
            observation_count DESC,
            normalized_value ASC
        """,
        (merchant_key, raw_key),
    ).fetchall()

    if not rows:
        return None

    top = rows[0]

    if len(rows) > 1:
        runner_up = rows[1]
        top_receipts = int(top["distinct_receipt_count"] or 0)
        top_observations = int(top["observation_count"] or 0)
        runner_receipts = int(runner_up["distinct_receipt_count"] or 0)
        runner_observations = int(runner_up["observation_count"] or 0)

        if runner_receipts >= top_receipts or runner_observations >= top_observations:
            return None

        if top_receipts < 2:
            return None

    matched_count = int(top["distinct_receipt_count"] or 0)
    if matched_count <= 0:
        matched_count = int(top["observation_count"] or 0)

    confidence = "high" if matched_count >= 3 else "medium"

    return {
        "source": "learned_observation_exact",
        "matched_observation_count": matched_count,
        "target_type": "item",
        "line_number": line_number,
        "field_name": None,
        "suggested_name": top["normalized_value"],
        "suggested_category": top["category_value"],
        "paid_price_hint": None,
        "confidence": confidence,
        "reason": (
            f"Exact learned match from {matched_count} approved observation(s) "
            f"for this merchant/raw item text."
        ),
    }



def _get_suppression_pattern_suggestion(
    active_patterns: dict[str, dict],
    item: dict,
) -> dict | None:
    pattern = derive_suppression_pattern(item.get("item_text_raw"))
    if not pattern:
        return None

    matched_pattern = active_patterns.get(pattern["pattern_key"])
    if not matched_pattern:
        return None

    matched_count = int(matched_pattern.get("matched_observation_count") or 0)
    distinct_artifact_count = int(matched_pattern.get("distinct_artifact_count") or 0)
    raw_variant_count = int(matched_pattern.get("raw_variant_count") or 0)
    source_receipt_counts = matched_pattern.get("source_receipt_counts") or {}

    evidence_phrase = f"{matched_count} approved repair receipt(s)"
    source_receipt_evidence = _format_suppression_source_receipt_evidence(source_receipt_counts)
    if source_receipt_evidence:
        evidence_phrase += f" ({source_receipt_evidence})"
    if raw_variant_count > 0:
        evidence_phrase += f", {raw_variant_count} raw variant(s)"
    if distinct_artifact_count > 0:
        evidence_phrase += f", {distinct_artifact_count} distinct artifact(s)"

    hint_label = matched_pattern.get("hint_label") or "Likely fragment line"
    example_text = matched_pattern.get("example_text")
    pattern_kind = matched_pattern.get("pattern_kind") or pattern.get("pattern_kind")

    reason = (
        f"Approved suppression pattern matched this line before OpenClaw. Evidence: {evidence_phrase}."
    )
    if example_text:
        reason += f" Learned example: {example_text}."

    return {
        "source": "approved_suppression_pattern",
        "proposal_id": matched_pattern.get("proposal_id"),
        "matched_observation_count": matched_count,
        "target_type": "item",
        "line_number": item.get("line_number"),
        "field_name": None,
        "suggested_name": None,
        "suggested_category": None,
        "paid_price_hint": None,
        "confidence": matched_pattern.get("confidence") or "medium",
        "suggested_action": "suppress",
        "hint_label": hint_label,
        "pattern_kind": pattern_kind,
        "reason": reason,
    }


def get_learned_item_suggestions(receipt: dict, items: list[dict]) -> list[dict]:
    merchant_key = canonicalize_merchant_key(
        receipt.get("merchant_canonical") or receipt.get("merchant_raw")
    )
    if not merchant_key:
        return []

    conn = _get_db_connection()
    suggestions: list[dict] = []

    try:
        active_profile_mappings = _load_active_profile_item_mappings(conn, merchant_key)
        active_alias_mappings = _load_active_profile_alias_mappings(conn, merchant_key)
        active_suppression_patterns = _load_active_profile_suppression_patterns(conn, merchant_key)

        for item in items:
            if int(item.get("is_suppressed") or 0) == 1:
                continue

            if not item.get("needs_review"):
                continue

            raw_key = normalize_raw_key(item.get("item_text_raw"))
            if not raw_key:
                continue

            active_mapping = active_profile_mappings.get(raw_key)
            if active_mapping:
                matched_count = int(active_mapping.get("matched_observation_count") or 0)
                distinct_artifact_count = int(active_mapping.get("distinct_artifact_count") or 0)

                evidence_phrase = f"{matched_count} approved receipt(s)"
                if distinct_artifact_count > 0:
                    evidence_phrase += f", {distinct_artifact_count} distinct artifact(s)"

                suggestions.append(
                    {
                        "source": "approved_profile_mapping",
                        "proposal_id": active_mapping.get("proposal_id"),
                        "matched_observation_count": matched_count,
                        "target_type": "item",
                        "line_number": item.get("line_number"),
                        "field_name": None,
                        "suggested_name": active_mapping["suggested_name"],
                        "suggested_category": active_mapping["suggested_category"],
                        "paid_price_hint": None,
                        "confidence": active_mapping.get("confidence") or "high",
                        "reason": (
                            f"Approved profile mapping for this merchant/raw item text. "
                            f"Evidence: {evidence_phrase}."
                        ),
                    }
                )
                continue

            active_alias = active_alias_mappings.get(raw_key)
            if active_alias:
                matched_count = int(active_alias.get("matched_observation_count") or 0)
                raw_variant_count = int(active_alias.get("raw_variant_count") or 0)
                distinct_artifact_count = int(active_alias.get("distinct_artifact_count") or 0)

                evidence_phrase = f"{matched_count} approved receipt(s), {raw_variant_count} raw variant(s)"
                if distinct_artifact_count > 0:
                    evidence_phrase += f", {distinct_artifact_count} distinct artifact(s)"

                suggestions.append(
                    {
                        "source": "approved_alias_bundle",
                        "proposal_id": active_alias.get("proposal_id"),
                        "matched_observation_count": matched_count,
                        "target_type": "item",
                        "line_number": item.get("line_number"),
                        "field_name": None,
                        "suggested_name": active_alias["suggested_name"],
                        "suggested_category": active_alias["suggested_category"],
                        "paid_price_hint": None,
                        "confidence": active_alias.get("confidence") or "medium",
                        "reason": (
                            f"Approved alias bundle for this merchant/item family. "
                            f"Evidence: {evidence_phrase}."
                        ),
                    }
                )
                continue

            observation_suggestion = _get_exact_observation_suggestion(
                conn,
                merchant_key,
                raw_key,
                item.get("line_number"),
            )
            if observation_suggestion:
                suggestions.append(observation_suggestion)
                continue

            suppression_suggestion = _get_suppression_pattern_suggestion(
                active_suppression_patterns,
                item,
            )
            if suppression_suggestion:
                suggestions.append(suppression_suggestion)
    finally:
        conn.close()

    return suggestions


def _proposal_identity_from_payload(payload: dict) -> tuple | None:
    proposal_type = payload.get("proposal_type")
    merchant_key = payload.get("merchant_key")

    if proposal_type == "item_mapping_bundle":
        mapping = payload.get("mapping") or {}
        raw_key = mapping.get("raw_key")
        approved_name = mapping.get("approved_name")
        approved_category = mapping.get("approved_category")

        if not all([merchant_key, raw_key, approved_name, approved_category]):
            return None

        return (
            "item_mapping_bundle",
            str(merchant_key),
            str(raw_key),
            str(approved_name),
            str(approved_category),
        )

    if proposal_type == "item_alias_bundle":
        mapping = payload.get("mapping") or {}
        approved_name_key = mapping.get("approved_name_key")
        approved_category = mapping.get("approved_category")

        if not all([merchant_key, approved_name_key, approved_category]):
            return None

        return (
            "item_alias_bundle",
            str(merchant_key),
            str(approved_name_key),
            str(approved_category),
        )

    if proposal_type == "suppression_pattern":
        pattern = payload.get("pattern") or {}
        pattern_key = pattern.get("pattern_key")
        suggested_action = pattern.get("suggested_action") or payload.get("suggested_action")

        if not all([merchant_key, pattern_key, suggested_action]):
            return None

        return (
            "suppression_pattern",
            str(merchant_key),
            str(pattern_key),
            str(suggested_action),
        )

    return None


def _load_existing_proposals_by_identity(conn: sqlite3.Connection) -> dict[tuple, dict]:
    existing_rows = conn.execute(
        """
        SELECT *
        FROM profile_proposals
        """
    ).fetchall()

    existing_by_identity: dict[tuple, dict] = {}

    for row in existing_rows:
        try:
            payload = json.loads(row["proposal_json"] or "{}")
        except Exception:
            payload = {}

        identity = _proposal_identity_from_payload(payload)
        if identity is None:
            continue

        existing_by_identity[identity] = {
            "id": row["id"],
            "status": row["status"],
            "proposal_json": row["proposal_json"],
            "summary": row["summary"],
            "confidence": row["confidence"],
            "evidence_count": row["evidence_count"],
            "payload": payload,
            "generation": payload.get("generation") or {},
        }

    return existing_by_identity


def _upsert_pending_proposal(
    conn: sqlite3.Connection,
    *,
    payload: dict,
    summary: str,
    confidence: str,
    evidence_count: int,
    now: str,
    existing_by_identity: dict[tuple, dict],
) -> str:
    identity = _proposal_identity_from_payload(payload)
    if identity is None:
        return "skipped_invalid"

    proposal_json = _stable_json(payload)
    existing = existing_by_identity.get(identity)

    if existing:
        if existing["status"] == "pending":
            should_update = any(
                [
                    existing["proposal_json"] != proposal_json,
                    existing["summary"] != summary,
                    existing["confidence"] != confidence,
                    int(existing["evidence_count"] or 0) != int(evidence_count),
                ]
            )

            if should_update:
                conn.execute(
                    """
                    UPDATE profile_proposals
                    SET source_scope = ?,
                        confidence = ?,
                        summary = ?,
                        evidence_count = ?,
                        proposal_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        payload.get("source_scope"),
                        confidence,
                        summary,
                        evidence_count,
                        proposal_json,
                        now,
                        existing["id"],
                    ),
                )
                existing_by_identity[identity] = {
                    "id": existing["id"],
                    "status": "pending",
                    "proposal_json": proposal_json,
                    "summary": summary,
                    "confidence": confidence,
                    "evidence_count": evidence_count,
                }
                return "updated"

            return "skipped_existing"

        return "skipped_existing"

    conn.execute(
        """
        INSERT INTO profile_proposals (
            proposal_type,
            merchant_scope,
            source_scope,
            confidence,
            summary,
            evidence_count,
            proposal_json,
            status,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.get("proposal_type"),
            payload.get("merchant_key"),
            payload.get("source_scope"),
            confidence,
            summary,
            evidence_count,
            proposal_json,
            "pending",
            now,
            now,
        ),
    )

    inserted_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    existing_by_identity[identity] = {
        "id": inserted_id,
        "status": "pending",
        "proposal_json": proposal_json,
        "summary": summary,
        "confidence": confidence,
        "evidence_count": evidence_count,
    }
    return "created"


def generate_item_mapping_proposals(
    conn: sqlite3.Connection,
    *,
    min_confirmations: int = 2,
    merchant_scope: str | None = None,
) -> dict:
    min_confirmations = max(2, int(min_confirmations))
    merchant_key_filter = canonicalize_merchant_key(merchant_scope)

    if merchant_key_filter:
        rows = conn.execute(
            """
            SELECT
                id,
                receipt_id,
                artifact_id,
                merchant_key,
                raw_key,
                normalized_value,
                category_value
            FROM learning_observations
            WHERE observation_type = 'approved_item_observation'
              AND status = 'observed'
              AND merchant_key = ?
              AND raw_key IS NOT NULL
              AND normalized_value IS NOT NULL
              AND category_value IS NOT NULL
            ORDER BY merchant_key, raw_key, id
            """,
            (merchant_key_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT
                id,
                receipt_id,
                artifact_id,
                merchant_key,
                raw_key,
                normalized_value,
                category_value
            FROM learning_observations
            WHERE observation_type = 'approved_item_observation'
              AND status = 'observed'
              AND raw_key IS NOT NULL
              AND normalized_value IS NOT NULL
              AND category_value IS NOT NULL
            ORDER BY merchant_key, raw_key, id
            """
        ).fetchall()

    existing_by_identity = _load_existing_proposals_by_identity(conn)

    grouped: dict[tuple[str, str], dict[tuple[str, str], dict[str, set]]] = {}

    for row in rows:
        merchant_key = row["merchant_key"]
        raw_key = row["raw_key"]
        normalized_value = row["normalized_value"]
        category_value = row["category_value"]

        group_key = (merchant_key, raw_key)
        mapping_key = (normalized_value, category_value)

        grouped.setdefault(group_key, {})
        grouped[group_key].setdefault(
            mapping_key,
            {
                "observation_ids": set(),
                "receipt_ids": set(),
                "artifact_ids": set(),
            },
        )

        grouped[group_key][mapping_key]["observation_ids"].add(int(row["id"]))

        if row["receipt_id"] is not None:
            grouped[group_key][mapping_key]["receipt_ids"].add(int(row["receipt_id"]))

        if row["artifact_id"]:
            grouped[group_key][mapping_key]["artifact_ids"].add(str(row["artifact_id"]))

    created_count = 0
    updated_count = 0
    skipped_conflict_count = 0
    skipped_existing_count = 0
    considered_group_count = 0

    now = _utcnow_iso()

    for (merchant_key, raw_key), mapping_bucket in grouped.items():
        considered_group_count += 1

        if len(mapping_bucket) != 1:
            skipped_conflict_count += 1
            continue

        (approved_name, approved_category), evidence_sets = next(iter(mapping_bucket.items()))

        receipt_ids = sorted(evidence_sets["receipt_ids"])
        artifact_ids = sorted(evidence_sets["artifact_ids"])
        observation_ids = sorted(evidence_sets["observation_ids"])

        distinct_receipt_count = len(receipt_ids)
        distinct_artifact_count = len(artifact_ids)

        if distinct_receipt_count < min_confirmations:
            continue

        if distinct_artifact_count > 0 and distinct_artifact_count < min_confirmations:
            continue

        payload = {
            "proposal_type": "item_mapping_bundle",
            "merchant_key": merchant_key,
            "source_scope": "approved_item_observation",
            "mapping": {
                "raw_key": raw_key,
                "approved_name": approved_name,
                "approved_category": approved_category,
            },
            "evidence": {
                "distinct_receipt_count": distinct_receipt_count,
                "distinct_artifact_count": distinct_artifact_count,
                "receipt_ids": receipt_ids,
                "artifact_ids": artifact_ids,
                "observation_ids": observation_ids,
            },
            "generation": {
                "min_confirmations": min_confirmations,
                "conflict_free": True,
            },
        }

        confidence = "high" if distinct_receipt_count >= max(3, min_confirmations + 1) else "medium"
        summary = f"{merchant_key}: {raw_key} -> {approved_name} / {approved_category}"
        evidence_count = distinct_receipt_count

        upsert_result = _upsert_pending_proposal(
            conn,
            payload=payload,
            summary=summary,
            confidence=confidence,
            evidence_count=evidence_count,
            now=now,
            existing_by_identity=existing_by_identity,
        )

        if upsert_result == "created":
            created_count += 1
        elif upsert_result == "updated":
            updated_count += 1
        elif upsert_result == "skipped_existing":
            skipped_existing_count += 1

    return {
        "created_count": created_count,
        "updated_count": updated_count,
        "skipped_conflict_count": skipped_conflict_count,
        "skipped_existing_count": skipped_existing_count,
        "considered_group_count": considered_group_count,
        "min_confirmations": min_confirmations,
        "merchant_scope": merchant_key_filter,
    }


def _sorted_variant_counts(bucket: dict[str, dict[str, set]], field_name: str) -> list[dict]:
    rows = []

    for value, evidence in bucket.items():
        rows.append(
            {
                field_name: value,
                "distinct_receipt_count": len(evidence["receipt_ids"]),
                "distinct_artifact_count": len(evidence["artifact_ids"]),
                "observation_count": len(evidence["observation_ids"]),
            }
        )

    rows.sort(
        key=lambda row: (
            -int(row["distinct_receipt_count"]),
            -int(row["distinct_artifact_count"]),
            -int(row["observation_count"]),
            str(row[field_name]),
        )
    )
    return rows


def _pick_best_display_name(name_bucket: dict[str, dict[str, set]]) -> str | None:
    if not name_bucket:
        return None

    candidates = _sorted_variant_counts(name_bucket, "normalized_value")
    if not candidates:
        return None

    candidates.sort(
        key=lambda row: (
            -int(row["distinct_receipt_count"]),
            -int(row["observation_count"]),
            -len(str(row["normalized_value"])),
            str(row["normalized_value"]),
        )
    )
    return candidates[0]["normalized_value"]


def generate_item_alias_proposals(
    conn: sqlite3.Connection,
    *,
    min_confirmations: int = 2,
    merchant_scope: str | None = None,
) -> dict:
    min_confirmations = max(2, int(min_confirmations))
    merchant_key_filter = canonicalize_merchant_key(merchant_scope)

    if merchant_key_filter:
        rows = conn.execute(
            """
            SELECT
                id,
                receipt_id,
                artifact_id,
                merchant_key,
                raw_key,
                normalized_value,
                category_value
            FROM learning_observations
            WHERE observation_type = 'approved_item_observation'
              AND status = 'observed'
              AND merchant_key = ?
              AND raw_key IS NOT NULL
              AND normalized_value IS NOT NULL
              AND category_value IS NOT NULL
            ORDER BY merchant_key, normalized_value, id
            """,
            (merchant_key_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT
                id,
                receipt_id,
                artifact_id,
                merchant_key,
                raw_key,
                normalized_value,
                category_value
            FROM learning_observations
            WHERE observation_type = 'approved_item_observation'
              AND status = 'observed'
              AND raw_key IS NOT NULL
              AND normalized_value IS NOT NULL
              AND category_value IS NOT NULL
            ORDER BY merchant_key, normalized_value, id
            """
        ).fetchall()

    existing_by_identity = _load_existing_proposals_by_identity(conn)

    grouped: dict[tuple[str, str, str], dict] = {}
    raw_key_to_family_keys: dict[str, set[tuple[str, str, str]]] = {}

    for row in rows:
        merchant_key = row["merchant_key"]
        raw_key = row["raw_key"]
        normalized_value = row["normalized_value"]
        category_value = row["category_value"]
        approved_name_key = normalize_name_family_key(normalized_value)

        if not merchant_key or not raw_key or not approved_name_key or not category_value:
            continue

        family_key = (merchant_key, approved_name_key, category_value)

        grouped.setdefault(
            family_key,
            {
                "merchant_key": merchant_key,
                "approved_name_key": approved_name_key,
                "approved_category": category_value,
                "receipt_ids": set(),
                "artifact_ids": set(),
                "observation_ids": set(),
                "raw_variants": {},
                "name_variants": {},
            },
        )

        if row["receipt_id"] is not None:
            grouped[family_key]["receipt_ids"].add(int(row["receipt_id"]))
        if row["artifact_id"]:
            grouped[family_key]["artifact_ids"].add(str(row["artifact_id"]))
        grouped[family_key]["observation_ids"].add(int(row["id"]))

        grouped[family_key]["raw_variants"].setdefault(
            raw_key,
            {"receipt_ids": set(), "artifact_ids": set(), "observation_ids": set()},
        )
        grouped[family_key]["name_variants"].setdefault(
            normalized_value,
            {"receipt_ids": set(), "artifact_ids": set(), "observation_ids": set()},
        )

        if row["receipt_id"] is not None:
            grouped[family_key]["raw_variants"][raw_key]["receipt_ids"].add(int(row["receipt_id"]))
            grouped[family_key]["name_variants"][normalized_value]["receipt_ids"].add(int(row["receipt_id"]))
        if row["artifact_id"]:
            grouped[family_key]["raw_variants"][raw_key]["artifact_ids"].add(str(row["artifact_id"]))
            grouped[family_key]["name_variants"][normalized_value]["artifact_ids"].add(str(row["artifact_id"]))

        grouped[family_key]["raw_variants"][raw_key]["observation_ids"].add(int(row["id"]))
        grouped[family_key]["name_variants"][normalized_value]["observation_ids"].add(int(row["id"]))

        raw_key_to_family_keys.setdefault(raw_key, set()).add(family_key)

    created_count = 0
    updated_count = 0
    skipped_conflict_count = 0
    skipped_existing_count = 0
    considered_family_count = 0

    now = _utcnow_iso()

    for family_key, family in grouped.items():
        considered_family_count += 1

        raw_variants = family["raw_variants"]
        name_variants = family["name_variants"]
        distinct_receipt_count = len(family["receipt_ids"])
        distinct_artifact_count = len(family["artifact_ids"])

        if len(raw_variants) < 2:
            continue

        if distinct_receipt_count < min_confirmations:
            continue

        if distinct_artifact_count > 0 and distinct_artifact_count < min_confirmations:
            continue

        overlapping_raw_keys = [
            raw_key for raw_key in raw_variants
            if len(raw_key_to_family_keys.get(raw_key, set())) > 1
        ]
        if overlapping_raw_keys:
            skipped_conflict_count += 1
            continue

        approved_name = _pick_best_display_name(name_variants)
        if not approved_name:
            continue

        raw_variant_rows = _sorted_variant_counts(raw_variants, "raw_key")
        name_variant_rows = _sorted_variant_counts(name_variants, "normalized_value")

        payload = {
            "proposal_type": "item_alias_bundle",
            "merchant_key": family["merchant_key"],
            "source_scope": "approved_item_observation",
            "mapping": {
                "approved_name_key": family["approved_name_key"],
                "approved_name": approved_name,
                "approved_category": family["approved_category"],
            },
            "raw_variants": [row["raw_key"] for row in raw_variant_rows],
            "normalized_variants": [row["normalized_value"] for row in name_variant_rows],
            "evidence": {
                "distinct_receipt_count": distinct_receipt_count,
                "distinct_artifact_count": distinct_artifact_count,
                "receipt_ids": sorted(family["receipt_ids"]),
                "artifact_ids": sorted(family["artifact_ids"]),
                "observation_ids": sorted(family["observation_ids"]),
                "raw_variant_counts": raw_variant_rows,
                "normalized_variant_counts": name_variant_rows,
            },
            "generation": {
                "min_confirmations": min_confirmations,
                "raw_variant_count": len(raw_variants),
            },
        }

        confidence = "high" if distinct_receipt_count >= max(3, min_confirmations + 1) and len(raw_variants) >= 3 else "medium"
        summary = (
            f"{family['merchant_key']}: {approved_name} / {family['approved_category']} "
            f"alias bundle ({len(raw_variants)} raw variants)"
        )
        evidence_count = distinct_receipt_count

        upsert_result = _upsert_pending_proposal(
            conn,
            payload=payload,
            summary=summary,
            confidence=confidence,
            evidence_count=evidence_count,
            now=now,
            existing_by_identity=existing_by_identity,
        )

        if upsert_result == "created":
            created_count += 1
        elif upsert_result == "updated":
            updated_count += 1
        elif upsert_result == "skipped_existing":
            skipped_existing_count += 1

    return {
        "created_count": created_count,
        "updated_count": updated_count,
        "skipped_conflict_count": skipped_conflict_count,
        "skipped_existing_count": skipped_existing_count,
        "considered_family_count": considered_family_count,
        "min_confirmations": min_confirmations,
        "merchant_scope": merchant_key_filter,
    }


def _collect_suppression_pattern_groups(
    conn: sqlite3.Connection,
    *,
    merchant_scope: str | None = None,
) -> dict:
    merchant_key_filter = canonicalize_merchant_key(merchant_scope)

    observation_placeholders = ", ".join("?" for _ in SUPPRESSION_PATTERN_SOURCE_TYPES)
    params: list[object] = list(SUPPRESSION_PATTERN_SOURCE_TYPES)
    merchant_clause = ""
    if merchant_key_filter:
        merchant_clause = "AND merchant_key = ?"
        params.append(merchant_key_filter)

    rows = conn.execute(
        f"""
        SELECT
            id,
            receipt_id,
            artifact_id,
            merchant_key,
            observation_type,
            raw_key,
            evidence_json
        FROM learning_observations
        WHERE observation_type IN ({observation_placeholders})
          AND status = 'observed'
          AND raw_key IS NOT NULL
          {merchant_clause}
        ORDER BY merchant_key, raw_key, id
        """,
        params,
    ).fetchall()

    grouped: dict[tuple[str, str], dict] = {}
    considered_observation_count = 0
    considered_source_observation_counts = {
        source_type: 0 for source_type in SUPPRESSION_PATTERN_SOURCE_TYPES
    }

    for row in rows:
        pattern = derive_suppression_pattern(row["raw_key"])
        if not pattern:
            continue

        observation_type = (row["observation_type"] or "").strip()
        considered_observation_count += 1
        if observation_type in considered_source_observation_counts:
            considered_source_observation_counts[observation_type] += 1

        group_key = (row["merchant_key"], pattern["pattern_key"])
        grouped.setdefault(
            group_key,
            {
                "merchant_key": row["merchant_key"],
                "pattern": pattern,
                "receipt_ids": set(),
                "artifact_ids": set(),
                "observation_ids": set(),
                "raw_variants": {},
                "source_observation_counts": {
                    source_type: 0 for source_type in SUPPRESSION_PATTERN_SOURCE_TYPES
                },
                "source_receipt_ids": {
                    source_type: set() for source_type in SUPPRESSION_PATTERN_SOURCE_TYPES
                },
                "source_artifact_ids": {
                    source_type: set() for source_type in SUPPRESSION_PATTERN_SOURCE_TYPES
                },
            },
        )

        grouped[group_key]["observation_ids"].add(int(row["id"]))
        if row["receipt_id"] is not None:
            grouped[group_key]["receipt_ids"].add(int(row["receipt_id"]))
        if row["artifact_id"]:
            grouped[group_key]["artifact_ids"].add(str(row["artifact_id"]))

        if observation_type in grouped[group_key]["source_observation_counts"]:
            grouped[group_key]["source_observation_counts"][observation_type] += 1
            if row["receipt_id"] is not None:
                grouped[group_key]["source_receipt_ids"][observation_type].add(int(row["receipt_id"]))
            if row["artifact_id"]:
                grouped[group_key]["source_artifact_ids"][observation_type].add(str(row["artifact_id"]))

        raw_key = row["raw_key"]
        grouped[group_key]["raw_variants"].setdefault(
            raw_key,
            {"receipt_ids": set(), "artifact_ids": set(), "observation_ids": set()},
        )
        grouped[group_key]["raw_variants"][raw_key]["observation_ids"].add(int(row["id"]))
        if row["receipt_id"] is not None:
            grouped[group_key]["raw_variants"][raw_key]["receipt_ids"].add(int(row["receipt_id"]))
        if row["artifact_id"]:
            grouped[group_key]["raw_variants"][raw_key]["artifact_ids"].add(str(row["artifact_id"]))

    groups: list[dict] = []
    for group in grouped.values():
        raw_variant_rows = _sorted_variant_counts(group["raw_variants"], "raw_key")
        source_observation_counts = {
            source_type: int(count)
            for source_type, count in group["source_observation_counts"].items()
            if int(count) > 0
        }
        source_receipt_counts = {
            source_type: len(receipt_ids)
            for source_type, receipt_ids in group["source_receipt_ids"].items()
            if len(receipt_ids) > 0
        }
        source_artifact_counts = {
            source_type: len(artifact_ids)
            for source_type, artifact_ids in group["source_artifact_ids"].items()
            if len(artifact_ids) > 0
        }
        evidence_source_types = [
            source_type
            for source_type in SUPPRESSION_PATTERN_SOURCE_TYPES
            if source_type in source_observation_counts
        ]
        source_scope = (
            evidence_source_types[0]
            if len(evidence_source_types) == 1
            else "repair_fragment_evidence"
        )

        group["distinct_receipt_count"] = len(group["receipt_ids"])
        group["distinct_artifact_count"] = len(group["artifact_ids"])
        group["raw_variant_rows"] = raw_variant_rows
        group["raw_variant_count"] = len(raw_variant_rows)
        group["source_observation_counts_compact"] = source_observation_counts
        group["source_receipt_counts_compact"] = source_receipt_counts
        group["source_artifact_counts_compact"] = source_artifact_counts
        group["evidence_source_types"] = evidence_source_types
        group["source_scope"] = source_scope
        groups.append(group)

    groups.sort(
        key=lambda group: (
            -int(group["distinct_receipt_count"]),
            -int(group["distinct_artifact_count"]),
            -len(group["observation_ids"]),
            -int(group["raw_variant_count"]),
            str(group["merchant_key"] or ""),
            str(group["pattern"].get("pattern_key") or ""),
        )
    )

    return {
        "merchant_scope": merchant_key_filter,
        "groups": groups,
        "considered_group_count": len(groups),
        "considered_observation_count": considered_observation_count,
        "considered_source_observation_counts": {
            source_type: int(count)
            for source_type, count in considered_source_observation_counts.items()
            if int(count) > 0
        },
    }



def _suppression_pattern_group_shortfalls(group: dict, min_confirmations: int) -> tuple[int, int]:
    normalized_min_confirmations = max(2, int(min_confirmations))
    receipt_shortfall = max(0, normalized_min_confirmations - int(group.get("distinct_receipt_count") or 0))

    distinct_artifact_count = int(group.get("distinct_artifact_count") or 0)
    artifact_shortfall = 0
    if distinct_artifact_count > 0 and distinct_artifact_count < normalized_min_confirmations:
        artifact_shortfall = normalized_min_confirmations - distinct_artifact_count

    return receipt_shortfall, artifact_shortfall



def _suppression_pattern_group_is_auto_ready(group: dict, min_confirmations: int) -> bool:
    receipt_shortfall, artifact_shortfall = _suppression_pattern_group_shortfalls(group, min_confirmations)
    return receipt_shortfall == 0 and artifact_shortfall == 0



def _build_suppression_pattern_proposal_bundle(
    group: dict,
    *,
    min_confirmations: int,
    proposal_mode: str = "auto_repeated_evidence",
) -> tuple[dict, str, str, int]:
    distinct_receipt_count = int(group.get("distinct_receipt_count") or 0)
    distinct_artifact_count = int(group.get("distinct_artifact_count") or 0)
    raw_variant_rows = list(group.get("raw_variant_rows") or [])
    source_observation_counts = dict(group.get("source_observation_counts_compact") or {})
    source_receipt_counts = dict(group.get("source_receipt_counts_compact") or {})
    source_artifact_counts = dict(group.get("source_artifact_counts_compact") or {})
    evidence_source_types = list(group.get("evidence_source_types") or [])
    receipt_shortfall, artifact_shortfall = _suppression_pattern_group_shortfalls(
        group,
        min_confirmations,
    )
    below_auto_threshold = bool(receipt_shortfall or artifact_shortfall)

    payload = {
        "proposal_type": "suppression_pattern",
        "merchant_key": group["merchant_key"],
        "source_scope": group.get("source_scope"),
        "pattern": group["pattern"],
        "raw_variants": [row["raw_key"] for row in raw_variant_rows],
        "evidence": {
            "distinct_receipt_count": distinct_receipt_count,
            "distinct_artifact_count": distinct_artifact_count,
            "receipt_ids": sorted(group["receipt_ids"]),
            "artifact_ids": sorted(group["artifact_ids"]),
            "observation_ids": sorted(group["observation_ids"]),
            "raw_variant_counts": raw_variant_rows,
            "source_observation_counts": source_observation_counts,
            "source_receipt_counts": source_receipt_counts,
            "source_artifact_counts": source_artifact_counts,
        },
        "generation": {
            "min_confirmations": max(2, int(min_confirmations)),
            "auto_threshold_min_confirmations": max(2, int(min_confirmations)),
            "raw_variant_count": len(raw_variant_rows),
            "match_mode": group["pattern"].get("match_mode"),
            "evidence_source_types": evidence_source_types,
            "proposal_mode": proposal_mode,
            "below_auto_threshold": below_auto_threshold,
            "receipt_shortfall": receipt_shortfall,
            "artifact_shortfall": artifact_shortfall,
            "seeded_by_analyst": proposal_mode.startswith("seeded"),
            "trigger": (
                "analyst_seeded_repair_pattern"
                if proposal_mode.startswith("seeded")
                else "auto_repeated_evidence"
            ),
        },
    }

    if proposal_mode.startswith("seeded"):
        payload["generation"]["seeded_from_recognized_pattern"] = True

    if proposal_mode.startswith("seeded") and below_auto_threshold:
        confidence = "low"
    elif distinct_receipt_count >= max(3, int(min_confirmations) + 1) and len(raw_variant_rows) >= 2:
        confidence = "high"
    else:
        confidence = "medium"

    summary = (
        f"{group['merchant_key']}: {group['pattern'].get('hint_label') or 'Suppression pattern'} "
        f"({group['pattern'].get('pattern_kind') or 'pattern'})"
    )
    evidence_count = distinct_receipt_count
    return payload, summary, confidence, evidence_count



def generate_suppression_pattern_proposals(
    conn: sqlite3.Connection,
    *,
    min_confirmations: int = 2,
    merchant_scope: str | None = None,
) -> dict:
    min_confirmations = max(2, int(min_confirmations))
    group_result = _collect_suppression_pattern_groups(
        conn,
        merchant_scope=merchant_scope,
    )
    existing_by_identity = _load_existing_proposals_by_identity(conn)

    created_count = 0
    updated_count = 0
    skipped_existing_count = 0
    ready_group_count = 0
    seedable_group_count = 0
    now = _utcnow_iso()

    for group in group_result["groups"]:
        ready_for_auto = _suppression_pattern_group_is_auto_ready(group, min_confirmations)
        if ready_for_auto:
            ready_group_count += 1
        if not ready_for_auto:
            payload_preview, _, _, _ = _build_suppression_pattern_proposal_bundle(
                group,
                min_confirmations=min_confirmations,
                proposal_mode="seeded_repair_pattern",
            )
            if _proposal_identity_from_payload(payload_preview) not in existing_by_identity:
                seedable_group_count += 1

        if not ready_for_auto:
            continue

        payload, summary, confidence, evidence_count = _build_suppression_pattern_proposal_bundle(
            group,
            min_confirmations=min_confirmations,
            proposal_mode="auto_repeated_evidence",
        )

        identity = _proposal_identity_from_payload(payload)
        existing = existing_by_identity.get(identity)
        if existing:
            existing_generation = existing.get("generation") or {}
            existing_mode = str(existing_generation.get("proposal_mode") or "")
            if existing_mode.startswith("seeded"):
                payload_generation = payload.setdefault("generation", {})
                payload_generation["proposal_mode"] = existing_mode
                payload_generation["seeded_by_analyst"] = True
                payload_generation["trigger"] = existing_generation.get("trigger") or "analyst_seeded_repair_pattern"
                payload_generation["seeded_from_recognized_pattern"] = True
                if existing_generation.get("seeded_at"):
                    payload_generation["seeded_at"] = existing_generation["seeded_at"]

        upsert_result = _upsert_pending_proposal(
            conn,
            payload=payload,
            summary=summary,
            confidence=confidence,
            evidence_count=evidence_count,
            now=now,
            existing_by_identity=existing_by_identity,
        )

        if upsert_result == "created":
            created_count += 1
        elif upsert_result == "updated":
            updated_count += 1
        elif upsert_result == "skipped_existing":
            skipped_existing_count += 1

    return {
        "created_count": created_count,
        "updated_count": updated_count,
        "skipped_existing_count": skipped_existing_count,
        "considered_pattern_count": int(group_result["considered_group_count"]),
        "considered_observation_count": int(group_result["considered_observation_count"]),
        "considered_source_observation_counts": dict(group_result["considered_source_observation_counts"]),
        "recognized_group_count": int(group_result["considered_group_count"]),
        "ready_group_count": int(ready_group_count),
        "seedable_group_count": int(seedable_group_count),
        "min_confirmations": min_confirmations,
        "merchant_scope": group_result["merchant_scope"],
    }



def generate_learning_proposals(
    conn: sqlite3.Connection,
    *,
    min_confirmations: int = 2,
    merchant_scope: str | None = None,
) -> dict:
    mapping_result = generate_item_mapping_proposals(
        conn,
        min_confirmations=min_confirmations,
        merchant_scope=merchant_scope,
    )
    alias_result = generate_item_alias_proposals(
        conn,
        min_confirmations=min_confirmations,
        merchant_scope=merchant_scope,
    )
    suppression_result = generate_suppression_pattern_proposals(
        conn,
        min_confirmations=min_confirmations,
        merchant_scope=merchant_scope,
    )

    return {
        "mapping": mapping_result,
        "alias": alias_result,
        "suppression": suppression_result,
        "total_created_count": (
            int(mapping_result["created_count"])
            + int(alias_result["created_count"])
            + int(suppression_result["created_count"])
        ),
        "total_updated_count": (
            int(mapping_result["updated_count"])
            + int(alias_result["updated_count"])
            + int(suppression_result["updated_count"])
        ),
    }


def list_profile_proposals(
    conn: sqlite3.Connection,
    *,
    status_filter: str | None = None,
    merchant_scope: str | None = None,
) -> list[dict]:
    normalized_status = (status_filter or "").strip().lower() or None
    normalized_merchant = canonicalize_merchant_key(merchant_scope)

    rows = conn.execute(
        """
        SELECT *
        FROM profile_proposals
        WHERE (? IS NULL OR status = ?)
          AND (? IS NULL OR merchant_scope = ?)
        ORDER BY
            CASE status
                WHEN 'pending' THEN 0
                WHEN 'approved' THEN 1
                WHEN 'rejected' THEN 2
                ELSE 3
            END,
            updated_at DESC,
            id DESC
        """,
        (
            normalized_status,
            normalized_status,
            normalized_merchant,
            normalized_merchant,
        ),
    ).fetchall()

    proposals: list[dict] = []
    for row in rows:
        row_dict = dict(row)
        try:
            proposal_payload = json.loads(row["proposal_json"] or "{}")
        except Exception:
            proposal_payload = {}

        row_dict["proposal"] = proposal_payload
        row_dict["mapping"] = proposal_payload.get("mapping") or {}
        row_dict["pattern"] = proposal_payload.get("pattern") or {}
        row_dict["evidence"] = proposal_payload.get("evidence") or {}
        row_dict["generation"] = proposal_payload.get("generation") or {}
        row_dict["raw_variants"] = proposal_payload.get("raw_variants") or []
        row_dict["normalized_variants"] = proposal_payload.get("normalized_variants") or []
        row_dict["source_observation_counts"] = row_dict["evidence"].get("source_observation_counts") or {}
        row_dict["source_receipt_counts"] = row_dict["evidence"].get("source_receipt_counts") or {}
        row_dict["source_artifact_counts"] = row_dict["evidence"].get("source_artifact_counts") or {}
        row_dict["suggested_action"] = (
            proposal_payload.get("suggested_action")
            or row_dict["pattern"].get("suggested_action")
            or None
        )
        row_dict["hint_label"] = row_dict["pattern"].get("hint_label") or None
        row_dict["proposal_mode"] = row_dict["generation"].get("proposal_mode") or "auto_repeated_evidence"
        row_dict["is_seeded_proposal"] = bool(
            row_dict["generation"].get("seeded_by_analyst")
            or str(row_dict["proposal_mode"]).startswith("seeded")
        )
        row_dict["below_auto_threshold"] = bool(row_dict["generation"].get("below_auto_threshold"))
        row_dict["receipt_shortfall"] = int(row_dict["generation"].get("receipt_shortfall") or 0)
        row_dict["artifact_shortfall"] = int(row_dict["generation"].get("artifact_shortfall") or 0)
        proposals.append(row_dict)

    return proposals


def update_profile_proposal_status(
    conn: sqlite3.Connection,
    proposal_id: int,
    status: str,
) -> bool:
    normalized_status = (status or "").strip().lower()
    if normalized_status not in {"pending", "approved", "rejected"}:
        return False

    cur = conn.execute(
        """
        UPDATE profile_proposals
        SET status = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            normalized_status,
            _utcnow_iso(),
            proposal_id,
        ),
    )
    return (cur.rowcount or 0) > 0


def list_recognized_repair_patterns(
    conn: sqlite3.Connection,
    *,
    merchant_scope: str | None = None,
    min_confirmations: int = 2,
    limit: int = 40,
) -> list[dict]:
    normalized_limit = max(1, int(limit))
    normalized_min_confirmations = max(2, int(min_confirmations))
    group_result = _collect_suppression_pattern_groups(
        conn,
        merchant_scope=merchant_scope,
    )
    existing_by_identity = _load_existing_proposals_by_identity(conn)

    rows: list[dict] = []
    for group in group_result["groups"]:
        payload_preview, _, _, _ = _build_suppression_pattern_proposal_bundle(
            group,
            min_confirmations=normalized_min_confirmations,
            proposal_mode="seeded_repair_pattern",
        )
        identity = _proposal_identity_from_payload(payload_preview)
        existing = existing_by_identity.get(identity) if identity else None
        ready_for_auto = _suppression_pattern_group_is_auto_ready(group, normalized_min_confirmations)
        receipt_shortfall, artifact_shortfall = _suppression_pattern_group_shortfalls(
            group,
            normalized_min_confirmations,
        )
        existing_generation = (existing or {}).get("generation") or {}

        row = {
            "merchant_key": group["merchant_key"],
            "pattern": group["pattern"],
            "pattern_key": group["pattern"].get("pattern_key"),
            "raw_variants": [variant_row["raw_key"] for variant_row in group.get("raw_variant_rows") or []],
            "raw_variant_count": int(group.get("raw_variant_count") or 0),
            "distinct_receipt_count": int(group.get("distinct_receipt_count") or 0),
            "distinct_artifact_count": int(group.get("distinct_artifact_count") or 0),
            "evidence_observation_count": int(len(group.get("observation_ids") or [])),
            "source_scope": group.get("source_scope"),
            "source_observation_counts": dict(group.get("source_observation_counts_compact") or {}),
            "source_receipt_counts": dict(group.get("source_receipt_counts_compact") or {}),
            "source_artifact_counts": dict(group.get("source_artifact_counts_compact") or {}),
            "ready_for_auto": ready_for_auto,
            "receipt_shortfall": int(receipt_shortfall),
            "artifact_shortfall": int(artifact_shortfall),
            "proposal_id": (existing or {}).get("id"),
            "proposal_status": (existing or {}).get("status"),
            "has_existing_proposal": bool(existing),
            "existing_is_seeded": bool(
                existing_generation.get("seeded_by_analyst")
                or str(existing_generation.get("proposal_mode") or "").startswith("seeded")
            ),
            "can_seed": not bool(existing) and not ready_for_auto,
            "proposal_mode": existing_generation.get("proposal_mode") or None,
        }
        rows.append(row)

    rows.sort(
        key=lambda row: (
            0 if row["can_seed"] else 1,
            0 if row["ready_for_auto"] else 1,
            0 if not row["has_existing_proposal"] else 1,
            -int(row["distinct_receipt_count"]),
            -int(row["evidence_observation_count"]),
            -int(row["raw_variant_count"]),
            str(row["merchant_key"] or ""),
            str(row["pattern_key"] or ""),
        )
    )
    return rows[:normalized_limit]



def seed_suppression_pattern_proposal(
    conn: sqlite3.Connection,
    *,
    merchant_key: str,
    pattern_key: str,
    min_confirmations: int = 2,
) -> dict:
    normalized_merchant_key = canonicalize_merchant_key(merchant_key)
    normalized_pattern_key = str(pattern_key or "").strip()
    normalized_min_confirmations = max(2, int(min_confirmations))
    if not normalized_merchant_key or not normalized_pattern_key:
        return {"status": "invalid"}

    group_result = _collect_suppression_pattern_groups(
        conn,
        merchant_scope=normalized_merchant_key,
    )
    target_group = None
    for group in group_result["groups"]:
        if (
            canonicalize_merchant_key(group.get("merchant_key")) == normalized_merchant_key
            and str(group.get("pattern", {}).get("pattern_key") or "").strip() == normalized_pattern_key
        ):
            target_group = group
            break

    if target_group is None:
        return {
            "status": "not_found",
            "merchant_key": normalized_merchant_key,
            "pattern_key": normalized_pattern_key,
        }

    payload, summary, confidence, evidence_count = _build_suppression_pattern_proposal_bundle(
        target_group,
        min_confirmations=normalized_min_confirmations,
        proposal_mode="seeded_repair_pattern",
    )
    payload.setdefault("generation", {})["seeded_at"] = _utcnow_iso()

    identity = _proposal_identity_from_payload(payload)
    existing_by_identity = _load_existing_proposals_by_identity(conn)
    existing = existing_by_identity.get(identity) if identity else None
    if existing:
        return {
            "status": "existing",
            "proposal_id": existing.get("id"),
            "proposal_status": existing.get("status"),
            "merchant_key": normalized_merchant_key,
            "pattern_key": normalized_pattern_key,
            "group": {
                "distinct_receipt_count": int(target_group.get("distinct_receipt_count") or 0),
                "distinct_artifact_count": int(target_group.get("distinct_artifact_count") or 0),
                "evidence_observation_count": int(len(target_group.get("observation_ids") or [])),
                "ready_for_auto": _suppression_pattern_group_is_auto_ready(
                    target_group,
                    normalized_min_confirmations,
                ),
            },
        }

    now = _utcnow_iso()
    upsert_result = _upsert_pending_proposal(
        conn,
        payload=payload,
        summary=summary,
        confidence=confidence,
        evidence_count=evidence_count,
        now=now,
        existing_by_identity=existing_by_identity,
    )

    proposal_id = None
    existing_after = existing_by_identity.get(identity) if identity else None
    if existing_after:
        proposal_id = existing_after.get("id")

    return {
        "status": upsert_result,
        "proposal_id": proposal_id,
        "merchant_key": normalized_merchant_key,
        "pattern_key": normalized_pattern_key,
        "group": {
            "distinct_receipt_count": int(target_group.get("distinct_receipt_count") or 0),
            "distinct_artifact_count": int(target_group.get("distinct_artifact_count") or 0),
            "evidence_observation_count": int(len(target_group.get("observation_ids") or [])),
            "ready_for_auto": _suppression_pattern_group_is_auto_ready(
                target_group,
                normalized_min_confirmations,
            ),
            "receipt_shortfall": _suppression_pattern_group_shortfalls(
                target_group,
                normalized_min_confirmations,
            )[0],
            "artifact_shortfall": _suppression_pattern_group_shortfalls(
                target_group,
                normalized_min_confirmations,
            )[1],
        },
    }



def get_learning_dashboard_summary(conn: sqlite3.Connection) -> dict:
    observed_item_count = conn.execute(
        """
        SELECT COUNT(*) AS count_value
        FROM learning_observations
        WHERE observation_type = 'approved_item_observation'
          AND status = 'observed'
        """
    ).fetchone()["count_value"]

    distinct_merchant_count = conn.execute(
        """
        SELECT COUNT(DISTINCT merchant_key) AS count_value
        FROM learning_observations
        WHERE observation_type = 'approved_item_observation'
          AND status = 'observed'
          AND merchant_key IS NOT NULL
        """
    ).fetchone()["count_value"]

    proposal_counts = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_count,
            COALESCE(SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END), 0) AS approved_count,
            COALESCE(SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END), 0) AS rejected_count,
            COUNT(*) AS total_count
        FROM profile_proposals
        """
    ).fetchone()

    alias_counts = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status = 'pending' AND proposal_type = 'item_alias_bundle' THEN 1 ELSE 0 END), 0) AS pending_alias_count,
            COALESCE(SUM(CASE WHEN status = 'approved' AND proposal_type = 'item_alias_bundle' THEN 1 ELSE 0 END), 0) AS approved_alias_count,
            COALESCE(SUM(CASE WHEN status = 'pending' AND proposal_type = 'suppression_pattern' THEN 1 ELSE 0 END), 0) AS pending_suppression_count,
            COALESCE(SUM(CASE WHEN status = 'approved' AND proposal_type = 'suppression_pattern' THEN 1 ELSE 0 END), 0) AS approved_suppression_count
        FROM profile_proposals
        """
    ).fetchone()

    repair_counts = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN observation_type = 'repair_merge_source' THEN 1 ELSE 0 END), 0) AS merge_count,
            COALESCE(SUM(CASE WHEN observation_type = 'repair_suppression' THEN 1 ELSE 0 END), 0) AS suppression_count,
            COALESCE(SUM(CASE WHEN observation_type = 'repair_manual_add' THEN 1 ELSE 0 END), 0) AS manual_add_count
        FROM learning_observations
        WHERE status = 'observed'
        """
    ).fetchone()

    group_result = _collect_suppression_pattern_groups(conn)
    recognized_pattern_count = int(group_result["considered_group_count"])
    ready_pattern_count = 0
    seedable_pattern_count = 0
    for group in group_result["groups"]:
        if _suppression_pattern_group_is_auto_ready(group, 2):
            ready_pattern_count += 1
        else:
            seedable_pattern_count += 1

    suppression_proposal_rows = conn.execute(
        """
        SELECT status, proposal_json
        FROM profile_proposals
        WHERE proposal_type = 'suppression_pattern'
        """
    ).fetchall()

    seeded_pending_suppression_proposal_count = 0
    seeded_approved_suppression_proposal_count = 0
    auto_pending_suppression_proposal_count = 0
    auto_approved_suppression_proposal_count = 0

    for row in suppression_proposal_rows:
        payload = _json_loads_safe(row["proposal_json"], {})
        generation = payload.get("generation") or {}
        is_seeded = bool(
            generation.get("seeded_by_analyst")
            or str(generation.get("proposal_mode") or "").startswith("seeded")
        )
        status_value = (row["status"] or "").strip().lower()
        if status_value == "pending":
            if is_seeded:
                seeded_pending_suppression_proposal_count += 1
            else:
                auto_pending_suppression_proposal_count += 1
        elif status_value == "approved":
            if is_seeded:
                seeded_approved_suppression_proposal_count += 1
            else:
                auto_approved_suppression_proposal_count += 1

    fragment_observation_count = int(group_result["considered_observation_count"] or 0)
    fragment_receipt_ids: set[int] = set()
    fragment_source_observation_counts = {
        source_type: 0 for source_type in SUPPRESSION_PATTERN_SOURCE_TYPES
    }
    fragment_source_receipt_ids = {
        source_type: set() for source_type in SUPPRESSION_PATTERN_SOURCE_TYPES
    }

    for group in group_result["groups"]:
        fragment_receipt_ids.update(group.get("receipt_ids") or set())
        for source_type, count in (group.get("source_observation_counts_compact") or {}).items():
            if source_type in fragment_source_observation_counts:
                fragment_source_observation_counts[source_type] += int(count)
        for source_type, receipt_ids in (group.get("source_receipt_ids") or {}).items():
            if source_type in fragment_source_receipt_ids:
                fragment_source_receipt_ids[source_type].update(receipt_ids)

    recorded_suggestion_run_count = conn.execute(
        """
        SELECT COUNT(*) AS count_value
        FROM receipt_run_metrics
        WHERE metric_type = 'suggestion_run'
        """
    ).fetchone()["count_value"]

    latest_suggestion_runs = _load_latest_metrics_by_receipt(
        conn,
        metric_type="suggestion_run",
    )
    latest_approval_outcomes = _load_latest_metrics_by_receipt(
        conn,
        metric_type="approval_outcome",
    )

    latest_total_review_item_count = sum(row.get("total_review_items", 0) for row in latest_suggestion_runs)
    latest_local_handled_count = sum(row.get("locally_handled_before_openclaw", 0) for row in latest_suggestion_runs)
    latest_openclaw_routed_count = sum(row.get("remaining_for_openclaw", 0) for row in latest_suggestion_runs)
    latest_local_mapping_hint_count = sum(row.get("local_mapping_hints", 0) for row in latest_suggestion_runs)
    latest_local_repair_hint_count = sum(row.get("local_repair_hints", 0) for row in latest_suggestion_runs)
    latest_openclaw_returned_count = sum(row.get("openclaw_returned", 0) for row in latest_suggestion_runs)
    latest_profile_mapping_decision_count = sum(row.get("local_profile_mapping_count", 0) for row in latest_suggestion_runs)
    latest_alias_bundle_decision_count = sum(row.get("local_alias_bundle_count", 0) for row in latest_suggestion_runs)
    latest_exact_reuse_decision_count = sum(row.get("local_exact_reuse_count", 0) for row in latest_suggestion_runs)
    latest_openclaw_suggested_count = sum(row.get("openclaw_suggested_count", 0) for row in latest_suggestion_runs)
    latest_no_suggestion_count = sum(row.get("no_suggestion_count", 0) for row in latest_suggestion_runs)

    latest_human_repair_action_count = sum(row.get("human_repair_action_count", 0) for row in latest_approval_outcomes)
    latest_manual_add_count = sum(row.get("manual_add_count", 0) for row in latest_approval_outcomes)
    latest_manual_merge_action_count = sum(row.get("manual_merge_group_count", 0) for row in latest_approval_outcomes)
    latest_manual_suppression_action_count = sum(row.get("manual_suppression_group_count", 0) for row in latest_approval_outcomes)
    latest_manual_merge_source_observation_count = sum(row.get("manual_merge_source_count", 0) for row in latest_approval_outcomes)
    latest_manual_suppression_observation_count = sum(row.get("manual_suppression_count", 0) for row in latest_approval_outcomes)
    latest_knowledge_captured_count = sum(row.get("knowledge_captured_count", 0) for row in latest_approval_outcomes)
    latest_active_item_count = sum(row.get("active_item_count", 0) for row in latest_approval_outcomes)
    latest_suppressed_item_count = sum(row.get("suppressed_item_count", 0) for row in latest_approval_outcomes)
    latest_ai_touched_item_count = sum(row.get("ai_touched_item_count", 0) for row in latest_approval_outcomes)
    latest_ai_accepted_item_count = sum(row.get("ai_accepted_item_count", 0) for row in latest_approval_outcomes)
    latest_ai_no_repair_item_count = sum(row.get("ai_no_repair_item_count", 0) for row in latest_approval_outcomes)
    latest_ai_repaired_after_item_count = sum(row.get("ai_repaired_after_item_count", 0) for row in latest_approval_outcomes)
    latest_ai_assisted_receipt_count = sum(1 for row in latest_approval_outcomes if row.get("ai_assisted_receipt"))
    latest_ai_lane_outcomes = _default_openclaw_lane_outcomes()
    for row in latest_approval_outcomes:
        latest_ai_lane_outcomes = _merge_openclaw_lane_outcomes(
            latest_ai_lane_outcomes,
            row.get("ai_lane_outcomes") or {},
        )

    ai_learning_summary = _collect_ai_learning_summary_from_observations(conn)

    openclaw_invocation_rows = conn.execute(
        """
        SELECT lane, success, agent_id, model_label, item_count, returned_count, duration_ms
        FROM openclaw_invocations
        ORDER BY id DESC
        """
    ).fetchall()

    openclaw_invocation_count = len(openclaw_invocation_rows)
    openclaw_success_invocation_count = sum(1 for row in openclaw_invocation_rows if _coerce_int(row["success"]) == 1)
    openclaw_failure_invocation_count = openclaw_invocation_count - openclaw_success_invocation_count
    openclaw_total_latency_ms = sum(_coerce_int(row["duration_ms"]) for row in openclaw_invocation_rows)
    openclaw_total_items_sent = sum(_coerce_int(row["item_count"]) for row in openclaw_invocation_rows)
    openclaw_total_returned = sum(_coerce_int(row["returned_count"]) for row in openclaw_invocation_rows)
    openclaw_structured_invocation_count = sum(1 for row in openclaw_invocation_rows if (row["lane"] or "") == "structured_item_resolution")
    openclaw_category_invocation_count = sum(1 for row in openclaw_invocation_rows if (row["lane"] or "") == "category_only_resolution")
    openclaw_ocr_invocation_count = sum(1 for row in openclaw_invocation_rows if (row["lane"] or "") == "ocr_item_resolution")
    openclaw_distinct_agent_count = len({row["agent_id"] for row in openclaw_invocation_rows if row["agent_id"]})
    openclaw_distinct_model_count = len({row["model_label"] for row in openclaw_invocation_rows if row["model_label"]})

    return {
        "observed_item_observation_count": int(observed_item_count or 0),
        "distinct_merchant_count": int(distinct_merchant_count or 0),
        "pending_proposal_count": int(proposal_counts["pending_count"] or 0),
        "approved_proposal_count": int(proposal_counts["approved_count"] or 0),
        "rejected_proposal_count": int(proposal_counts["rejected_count"] or 0),
        "total_proposal_count": int(proposal_counts["total_count"] or 0),
        "pending_alias_proposal_count": int(alias_counts["pending_alias_count"] or 0),
        "approved_alias_proposal_count": int(alias_counts["approved_alias_count"] or 0),
        "pending_suppression_proposal_count": int(alias_counts["pending_suppression_count"] or 0),
        "approved_suppression_proposal_count": int(alias_counts["approved_suppression_count"] or 0),
        "repair_merge_observation_count": int(repair_counts["merge_count"] or 0),
        "repair_suppression_observation_count": int(repair_counts["suppression_count"] or 0),
        "repair_manual_add_observation_count": int(repair_counts["manual_add_count"] or 0),
        "repair_fragment_observation_count": int(fragment_observation_count or 0),
        "repair_fragment_receipt_count": int(len(fragment_receipt_ids)),
        "repair_fragment_merge_observation_count": int(fragment_source_observation_counts["repair_merge_source"] or 0),
        "repair_fragment_merge_receipt_count": int(len(fragment_source_receipt_ids["repair_merge_source"])),
        "repair_fragment_suppression_observation_count": int(fragment_source_observation_counts["repair_suppression"] or 0),
        "repair_fragment_suppression_receipt_count": int(len(fragment_source_receipt_ids["repair_suppression"])),
        "recognized_repair_pattern_count": int(recognized_pattern_count),
        "ready_repair_pattern_count": int(ready_pattern_count),
        "seedable_repair_pattern_count": int(seedable_pattern_count),
        "seeded_pending_suppression_proposal_count": int(seeded_pending_suppression_proposal_count),
        "seeded_approved_suppression_proposal_count": int(seeded_approved_suppression_proposal_count),
        "auto_pending_suppression_proposal_count": int(auto_pending_suppression_proposal_count),
        "auto_approved_suppression_proposal_count": int(auto_approved_suppression_proposal_count),
        "recorded_suggestion_run_count": int(recorded_suggestion_run_count or 0),
        "openclaw_invocation_count": int(openclaw_invocation_count),
        "openclaw_success_invocation_count": int(openclaw_success_invocation_count),
        "openclaw_failure_invocation_count": int(openclaw_failure_invocation_count),
        "openclaw_success_rate_pct": _metric_rate(openclaw_success_invocation_count, openclaw_invocation_count),
        "openclaw_avg_latency_ms": round(openclaw_total_latency_ms / max(1, openclaw_invocation_count), 1) if openclaw_invocation_count else 0.0,
        "openclaw_total_items_sent": int(openclaw_total_items_sent),
        "openclaw_total_returned": int(openclaw_total_returned),
        "openclaw_avg_items_per_invocation": round(openclaw_total_items_sent / max(1, openclaw_invocation_count), 1) if openclaw_invocation_count else 0.0,
        "openclaw_avg_returned_per_invocation": round(openclaw_total_returned / max(1, openclaw_invocation_count), 1) if openclaw_invocation_count else 0.0,
        "openclaw_structured_invocation_count": int(openclaw_structured_invocation_count),
        "openclaw_category_invocation_count": int(openclaw_category_invocation_count),
        "openclaw_ocr_invocation_count": int(openclaw_ocr_invocation_count),
        "openclaw_distinct_agent_count": int(openclaw_distinct_agent_count),
        "openclaw_distinct_model_count": int(openclaw_distinct_model_count),
        "ai_backed_learning_observation_count": int(ai_learning_summary["captured_count"]),
        "ai_backed_learning_no_repair_count": int(ai_learning_summary["no_repair_count"]),
        "ai_backed_learning_receipt_count": int(ai_learning_summary["receipt_count"]),
        "ai_backed_learning_rate_pct": _metric_rate(
            ai_learning_summary["captured_count"],
            int(observed_item_count or 0),
        ),
        "ai_learning_lane_outcomes": ai_learning_summary["lane_outcomes"],
        "ai_learning_lane_cards": ai_learning_summary["lane_cards"],
        "ai_learning_strategy_cards": ai_learning_summary["strategy_cards"],
        "ai_learning_strategy_count": int(ai_learning_summary["strategy_count"]),
        "latest_suggestion_run_receipt_count": int(len(latest_suggestion_runs)),
        "latest_total_review_item_count": int(latest_total_review_item_count),
        "latest_local_handled_count": int(latest_local_handled_count),
        "latest_openclaw_routed_count": int(latest_openclaw_routed_count),
        "latest_local_mapping_hint_count": int(latest_local_mapping_hint_count),
        "latest_local_repair_hint_count": int(latest_local_repair_hint_count),
        "latest_openclaw_returned_count": int(latest_openclaw_returned_count),
        "latest_profile_mapping_decision_count": int(latest_profile_mapping_decision_count),
        "latest_alias_bundle_decision_count": int(latest_alias_bundle_decision_count),
        "latest_exact_reuse_decision_count": int(latest_exact_reuse_decision_count),
        "latest_openclaw_suggested_count": int(latest_openclaw_suggested_count),
        "latest_no_suggestion_count": int(latest_no_suggestion_count),
        "latest_local_resolution_rate_pct": _metric_rate(
            latest_local_handled_count,
            latest_total_review_item_count,
        ),
        "latest_openclaw_routed_rate_pct": _metric_rate(
            latest_openclaw_routed_count,
            latest_total_review_item_count,
        ),
        "latest_approval_outcome_count": int(len(latest_approval_outcomes)),
        "latest_human_repair_action_count": int(latest_human_repair_action_count),
        "latest_manual_add_count": int(latest_manual_add_count),
        "latest_manual_merge_action_count": int(latest_manual_merge_action_count),
        "latest_manual_suppression_action_count": int(latest_manual_suppression_action_count),
        "latest_manual_merge_source_observation_count": int(latest_manual_merge_source_observation_count),
        "latest_manual_suppression_observation_count": int(latest_manual_suppression_observation_count),
        "latest_knowledge_captured_count": int(latest_knowledge_captured_count),
        "latest_active_item_count": int(latest_active_item_count),
        "latest_suppressed_item_count": int(latest_suppressed_item_count),
        "latest_ai_touched_item_count": int(latest_ai_touched_item_count),
        "latest_ai_accepted_item_count": int(latest_ai_accepted_item_count),
        "latest_ai_no_repair_item_count": int(latest_ai_no_repair_item_count),
        "latest_ai_repaired_after_item_count": int(latest_ai_repaired_after_item_count),
        "latest_ai_assisted_receipt_count": int(latest_ai_assisted_receipt_count),
        "latest_ai_acceptance_rate_pct": _metric_rate(
            latest_ai_accepted_item_count,
            latest_ai_touched_item_count,
        ),
        "latest_ai_no_repair_rate_pct": _metric_rate(
            latest_ai_no_repair_item_count,
            latest_ai_touched_item_count,
        ),
        "latest_ai_repaired_after_rate_pct": _metric_rate(
            latest_ai_repaired_after_item_count,
            latest_ai_touched_item_count,
        ),
        "latest_ai_assisted_receipt_rate_pct": _metric_rate(
            latest_ai_assisted_receipt_count,
            len(latest_approval_outcomes),
        ),
        "latest_ai_lane_outcomes": latest_ai_lane_outcomes,
        "latest_ai_lane_cards": _build_openclaw_lane_effectiveness_cards(latest_ai_lane_outcomes),
    }



def list_recent_learning_observations(
    conn: sqlite3.Connection,
    *,
    merchant_scope: str | None = None,
    limit: int = 40,
) -> list[dict]:
    normalized_merchant = canonicalize_merchant_key(merchant_scope)
    normalized_limit = max(1, int(limit))

    rows = conn.execute(
        """
        SELECT
            id,
            receipt_id,
            receipt_item_id,
            artifact_id,
            merchant_key,
            raw_key,
            normalized_value,
            category_value,
            evidence_json,
            created_at,
            updated_at
        FROM learning_observations
        WHERE observation_type = 'approved_item_observation'
          AND status = 'observed'
          AND (? IS NULL OR merchant_key = ?)
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            normalized_merchant,
            normalized_merchant,
            normalized_limit,
        ),
    ).fetchall()

    parsed_rows: list[dict] = []
    for row in rows:
        parsed = dict(row)
        evidence = _json_loads_safe(row["evidence_json"], {})
        ai = evidence.get("ai_provenance") if isinstance(evidence, dict) else None
        parsed["ai_backed"] = bool(isinstance(ai, dict) and ai.get("accepted"))
        parsed["ai_no_repair"] = bool(isinstance(ai, dict) and ai.get("no_repair"))
        parsed["ai_lane_label"] = ai.get("lane_label") if isinstance(ai, dict) else None
        parsed["ai_model_label"] = ai.get("model_label") if isinstance(ai, dict) else None
        parsed["ai_agent_id"] = ai.get("agent_id") if isinstance(ai, dict) else None
        parsed["ai_request_model"] = ai.get("request_model") if isinstance(ai, dict) else None
        parsed_rows.append(parsed)

    return parsed_rows


def list_item_mapping_candidate_rollups(
    conn: sqlite3.Connection,
    *,
    merchant_scope: str | None = None,
    min_confirmations: int = 2,
    limit: int = 80,
) -> list[dict]:
    normalized_merchant = canonicalize_merchant_key(merchant_scope)
    normalized_min_confirmations = max(2, int(min_confirmations))
    normalized_limit = max(1, int(limit))

    rows = conn.execute(
        """
        SELECT
            id,
            receipt_id,
            artifact_id,
            merchant_key,
            raw_key,
            normalized_value,
            category_value
        FROM learning_observations
        WHERE observation_type = 'approved_item_observation'
          AND status = 'observed'
          AND raw_key IS NOT NULL
          AND normalized_value IS NOT NULL
          AND category_value IS NOT NULL
          AND (? IS NULL OR merchant_key = ?)
        ORDER BY merchant_key, raw_key, id
        """,
        (
            normalized_merchant,
            normalized_merchant,
        ),
    ).fetchall()

    grouped: dict[tuple[str, str], dict] = {}

    for row in rows:
        merchant_key = row["merchant_key"]
        raw_key = row["raw_key"]
        normalized_value = row["normalized_value"]
        category_value = row["category_value"]

        if not merchant_key or not raw_key or not normalized_value or not category_value:
            continue

        group_key = (merchant_key, raw_key)
        mapping_key = (normalized_value, category_value)

        if group_key not in grouped:
            grouped[group_key] = {
                "merchant_key": merchant_key,
                "raw_key": raw_key,
                "receipt_ids": set(),
                "artifact_ids": set(),
                "observation_ids": set(),
                "mapping_variants": {},
            }

        grouped[group_key]["observation_ids"].add(int(row["id"]))

        if row["receipt_id"] is not None:
            grouped[group_key]["receipt_ids"].add(int(row["receipt_id"]))

        if row["artifact_id"]:
            grouped[group_key]["artifact_ids"].add(str(row["artifact_id"]))

        grouped[group_key]["mapping_variants"].setdefault(
            mapping_key,
            {
                "receipt_ids": set(),
                "artifact_ids": set(),
                "observation_ids": set(),
            },
        )

        grouped[group_key]["mapping_variants"][mapping_key]["observation_ids"].add(int(row["id"]))

        if row["receipt_id"] is not None:
            grouped[group_key]["mapping_variants"][mapping_key]["receipt_ids"].add(int(row["receipt_id"]))

        if row["artifact_id"]:
            grouped[group_key]["mapping_variants"][mapping_key]["artifact_ids"].add(str(row["artifact_id"]))

    results: list[dict] = []

    for group in grouped.values():
        mapping_variants = group["mapping_variants"]
        distinct_receipt_count = len(group["receipt_ids"])
        distinct_artifact_count = len(group["artifact_ids"])
        observation_count = len(group["observation_ids"])
        mapping_variant_count = len(mapping_variants)

        top_variant_key = None
        top_variant_data = None

        for mapping_key, mapping_data in mapping_variants.items():
            if top_variant_data is None:
                top_variant_key = mapping_key
                top_variant_data = mapping_data
                continue

            current_receipts = len(mapping_data["receipt_ids"])
            current_artifacts = len(mapping_data["artifact_ids"])
            current_observations = len(mapping_data["observation_ids"])

            best_receipts = len(top_variant_data["receipt_ids"])
            best_artifacts = len(top_variant_data["artifact_ids"])
            best_observations = len(top_variant_data["observation_ids"])

            if (
                current_receipts > best_receipts
                or (
                    current_receipts == best_receipts
                    and current_artifacts > best_artifacts
                )
                or (
                    current_receipts == best_receipts
                    and current_artifacts == best_artifacts
                    and current_observations > best_observations
                )
            ):
                top_variant_key = mapping_key
                top_variant_data = mapping_data

        approved_name = top_variant_key[0] if top_variant_key else None
        approved_category = top_variant_key[1] if top_variant_key else None

        if mapping_variant_count > 1:
            state = "conflict"
            state_reason = f"{mapping_variant_count} competing approved mappings exist for this raw key."
        else:
            artifact_gate_ok = distinct_artifact_count == 0 or distinct_artifact_count >= normalized_min_confirmations
            receipt_gate_ok = distinct_receipt_count >= normalized_min_confirmations

            if receipt_gate_ok and artifact_gate_ok:
                state = "ready"
                state_reason = (
                    f"Ready for proposal generation with {distinct_receipt_count} distinct receipt(s)"
                    + (
                        f" and {distinct_artifact_count} distinct artifact(s)."
                        if distinct_artifact_count > 0
                        else "."
                    )
                )
            else:
                state = "needs_more_confirmations"
                state_reason = (
                    f"Needs more confirmations. Distinct receipts: {distinct_receipt_count}/{normalized_min_confirmations}"
                    + (
                        f", distinct artifacts: {distinct_artifact_count}/{normalized_min_confirmations}."
                        if distinct_artifact_count > 0
                        else "."
                    )
                )

        results.append(
            {
                "merchant_key": group["merchant_key"],
                "raw_key": group["raw_key"],
                "approved_name": approved_name,
                "approved_category": approved_category,
                "distinct_receipt_count": distinct_receipt_count,
                "distinct_artifact_count": distinct_artifact_count,
                "observation_count": observation_count,
                "mapping_variant_count": mapping_variant_count,
                "state": state,
                "state_reason": state_reason,
            }
        )

    state_order = {
        "ready": 0,
        "needs_more_confirmations": 1,
        "conflict": 2,
    }

    results.sort(
        key=lambda row: (
            state_order.get(row["state"], 9),
            -int(row["distinct_receipt_count"]),
            -int(row["distinct_artifact_count"]),
            -int(row["observation_count"]),
            row["merchant_key"] or "",
            row["raw_key"] or "",
        )
    )

    return results[:normalized_limit]