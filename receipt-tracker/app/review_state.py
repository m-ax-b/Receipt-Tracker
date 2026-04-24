from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import secrets


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REVIEW_STATE_DIR = DATA_DIR / "review_state"
REVIEW_STATE_DIR.mkdir(parents=True, exist_ok=True)

REVIEW_STATE_TTL_HOURS = 12


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _state_path(token: str) -> Path:
    return REVIEW_STATE_DIR / f"{token}.json"


def purge_expired_review_states() -> None:
    cutoff = _now_utc() - timedelta(hours=REVIEW_STATE_TTL_HOURS)

    for path in REVIEW_STATE_DIR.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created_at = _from_iso(payload.get("created_at"))
            if created_at is None or created_at < cutoff:
                path.unlink(missing_ok=True)
        except Exception:
            path.unlink(missing_ok=True)


def save_review_state(
    *,
    receipt_id: int,
    suggestions_by_item_id: dict | None = None,
    suggestion_stats: dict | None = None,
    suggestion_notice: str | None = None,
) -> str:
    purge_expired_review_states()

    token = secrets.token_urlsafe(18)
    payload = {
        "schema_version": "review-state.v1",
        "token": token,
        "receipt_id": receipt_id,
        "created_at": _to_iso(_now_utc()),
        "suggestions_by_item_id": {
            str(key): value for key, value in (suggestions_by_item_id or {}).items()
        },
        "suggestion_stats": suggestion_stats or {},
        "suggestion_notice": suggestion_notice,
    }

    _state_path(token).write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return token


def load_review_state(token: str | None, *, receipt_id: int | None = None) -> dict | None:
    purge_expired_review_states()

    if not token:
        return None

    path = _state_path(token)
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        path.unlink(missing_ok=True)
        return None

    created_at = _from_iso(payload.get("created_at"))
    if created_at is None:
        path.unlink(missing_ok=True)
        return None

    cutoff = _now_utc() - timedelta(hours=REVIEW_STATE_TTL_HOURS)
    if created_at < cutoff:
        path.unlink(missing_ok=True)
        return None

    stored_receipt_id = payload.get("receipt_id")
    if receipt_id is not None and stored_receipt_id != receipt_id:
        return None

    raw_suggestions = payload.get("suggestions_by_item_id") or {}
    suggestions_by_item_id = {}
    for key, value in raw_suggestions.items():
        try:
            item_id = int(key)
        except Exception:
            continue
        if isinstance(value, dict):
            suggestions_by_item_id[item_id] = value

    return {
        "token": token,
        "receipt_id": stored_receipt_id,
        "suggestions_by_item_id": suggestions_by_item_id,
        "suggestion_stats": payload.get("suggestion_stats") or {},
        "suggestion_notice": payload.get("suggestion_notice"),
    }