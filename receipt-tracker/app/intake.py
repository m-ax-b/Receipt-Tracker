from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import sqlite3

from app.artifacts import (
    create_connector_fetched_artifact,
    read_manifest,
    update_artifact_workflow,
)
from app.parsing.bjs_html import looks_like_bjs_saved_html, parse_bjs_saved_html
from app.parsing.parser import parse_receipt_image


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
DB_PATH = DATA_DIR / "receipts.db"


HTML_MEDIA_TYPES = {"text/html", "application/xhtml+xml"}


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _is_image_artifact(artifact_info: dict) -> bool:
    media_type = artifact_info.get("primary_media_type") or ""
    return str(media_type).startswith("image/")


def _is_html_artifact(artifact_info: dict) -> bool:
    media_type = (artifact_info.get("primary_media_type") or "").lower()
    return media_type in HTML_MEDIA_TYPES


def _is_pdf_artifact(artifact_info: dict) -> bool:
    media_type = (artifact_info.get("primary_media_type") or "").lower()
    return media_type == "application/pdf"


def _update_receipt_status_only(receipt_id: int, *, status: str, review_notes: str) -> None:
    conn = get_db_connection()
    conn.execute(
        """
        UPDATE receipts
        SET status = ?,
            review_notes = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            review_notes,
            _now_iso(),
            receipt_id,
        ),
    )
    conn.commit()
    conn.close()


def _load_parser_hints(artifact_info: dict) -> dict:
    parser_hints = dict(artifact_info.get("parser_hints") or {})
    if parser_hints:
        return parser_hints

    artifact_id = artifact_info.get("artifact_id")
    if not artifact_id:
        return {}

    try:
        _, manifest = read_manifest(ARTIFACTS_DIR, str(artifact_id))
    except Exception:
        return {}

    return dict(manifest.get("parser_hints") or {})


def insert_receipt_stub_for_artifact(
    artifact_info: dict,
    *,
    preserve_duplicate_artifact: bool = True,
) -> dict:
    now = _now_iso()
    conn = get_db_connection()

    existing = conn.execute(
        "SELECT id FROM receipts WHERE image_hash = ?",
        (artifact_info["primary_sha256"],),
    ).fetchone()

    if existing is not None:
        existing_id = existing["id"]
        conn.close()

        if preserve_duplicate_artifact:
            update_artifact_workflow(
                ARTIFACTS_DIR,
                artifact_info["artifact_id"],
                status="duplicate_exact",
                imported_receipt_id=existing_id,
                notes="Exact duplicate file hash matched an existing receipt during intake.",
            )
        else:
            shutil.rmtree(artifact_info["artifact_dir"], ignore_errors=True)

        return {
            "receipt_id": existing_id,
            "is_duplicate": True,
            "duplicate_receipt_id": existing_id,
            "artifact_id": artifact_info["artifact_id"],
        }

    conn.execute(
        """
        INSERT INTO receipts (
            artifact_id,
            image_path,
            image_hash,
            merchant_raw,
            merchant_canonical,
            purchase_date,
            purchase_time,
            subtotal,
            tax,
            total,
            currency,
            ocr_text_raw,
            receipt_confidence,
            status,
            review_notes,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_info["artifact_id"],
            str(artifact_info["primary_path"]),
            artifact_info["primary_sha256"],
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "USD",
            None,
            None,
            "uploaded",
            f"Artifact package created: {artifact_info['artifact_id']}.",
            now,
            now,
        ),
    )
    conn.commit()
    receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    update_artifact_workflow(
        ARTIFACTS_DIR,
        artifact_info["artifact_id"],
        imported_receipt_id=receipt_id,
    )

    return {
        "receipt_id": receipt_id,
        "is_duplicate": False,
        "duplicate_receipt_id": None,
        "artifact_id": artifact_info["artifact_id"],
    }


def _select_parse_result_for_artifact(artifact_info: dict) -> dict | None:
    parser_hints = _load_parser_hints(artifact_info)

    if _is_image_artifact(artifact_info):
        return parse_receipt_image(str(artifact_info["primary_path"]))

    if _is_html_artifact(artifact_info):
        html_text = Path(artifact_info["primary_path"]).read_text(encoding="utf-8", errors="ignore")
        merchant_hint = (parser_hints.get("merchant_hint") or "").strip().lower()
        expected_layout = (parser_hints.get("expected_layout") or "").strip().lower()

        if (
            "bj" in merchant_hint
            or "bjs" in expected_layout
            or looks_like_bjs_saved_html(html_text)
        ):
            return parse_bjs_saved_html(html_text)

    return None


def _apply_parse_result_to_receipt(conn, receipt_id: int, parse_result: dict) -> None:
    receipt_data = parse_result["receipt"]
    item_rows = parse_result["items"]

    conn.execute(
        """
        UPDATE receipts
        SET merchant_raw = ?,
            merchant_canonical = ?,
            purchase_date = ?,
            purchase_time = ?,
            subtotal = ?,
            tax = ?,
            total = ?,
            currency = ?,
            ocr_text_raw = ?,
            receipt_confidence = ?,
            status = ?,
            review_notes = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            receipt_data["merchant_raw"],
            receipt_data["merchant_canonical"],
            receipt_data["purchase_date"],
            receipt_data["purchase_time"],
            receipt_data["subtotal"],
            receipt_data["tax"],
            receipt_data["total"],
            receipt_data["currency"],
            receipt_data["ocr_text_raw"],
            receipt_data["receipt_confidence"],
            receipt_data["status"],
            receipt_data["review_notes"],
            receipt_data["updated_at"],
            receipt_id,
        ),
    )

    insert_rows = []
    for item in item_rows:
        insert_rows.append(
            (
                receipt_id,
                item["line_number"],
                item["item_text_raw"],
                item["item_name_normalized"],
                item["quantity"],
                item["unit"],
                item["unit_price"],
                item["line_total"],
                item["category"],
                item.get("category_source_raw"),
                item["item_confidence"],
                item["needs_review"],
                item["review_notes"],
                item.get("source_item_code"),
                item.get("source_item_detail_hint"),
                item["created_at"],
                item["updated_at"],
            )
        )

    if insert_rows:
        conn.executemany(
            """
            INSERT INTO receipt_items (
                receipt_id,
                line_number,
                item_text_raw,
                item_name_normalized,
                quantity,
                unit,
                unit_price,
                line_total,
                category,
                category_source_raw,
                item_confidence,
                needs_review,
                review_notes,
                source_item_code,
                source_item_detail_hint,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_rows,
        )


def seed_parse_result_from_artifact(receipt_id: int, artifact_info: dict) -> dict:
    conn = get_db_connection()
    existing = conn.execute(
        "SELECT COUNT(*) AS count FROM receipt_items WHERE receipt_id = ?",
        (receipt_id,),
    ).fetchone()

    if existing["count"] > 0:
        conn.close()
        return {
            "parsed": False,
            "parse_skipped": True,
            "reason": "receipt_items_already_present",
        }

    try:
        parse_result = _select_parse_result_for_artifact(artifact_info)
    except Exception as exc:
        conn.close()

        note = f"Artifact captured successfully, but parse step failed: {exc}"
        _update_receipt_status_only(receipt_id, status="uploaded", review_notes=note)
        update_artifact_workflow(
            ARTIFACTS_DIR,
            artifact_info["artifact_id"],
            status="normalized",
            imported_receipt_id=receipt_id,
            notes=note,
        )
        return {
            "parsed": False,
            "parse_skipped": True,
            "reason": "parse_failed",
        }

    if parse_result is None:
        conn.close()

        if _is_html_artifact(artifact_info):
            note = (
                "Artifact captured successfully, but no structured HTML adapter matched this page yet. "
                "BJ's saved order-history HTML is supported now; other HTML layouts remain staged for later adapters."
            )
            skip_reason = "html_adapter_not_available"
        elif _is_pdf_artifact(artifact_info):
            note = (
                "Artifact captured successfully through the unified intake router, but deterministic PDF parsing is not enabled yet. "
                "The PDF package has been preserved for future adapter work."
            )
            skip_reason = "pdf_parser_not_available"
        else:
            note = (
                "Artifact captured successfully, but parse step was skipped because "
                "the primary artifact is not an image or a supported structured HTML receipt."
            )
            skip_reason = "primary_artifact_not_supported"

        _update_receipt_status_only(receipt_id, status="uploaded", review_notes=note)
        update_artifact_workflow(
            ARTIFACTS_DIR,
            artifact_info["artifact_id"],
            status="normalized",
            imported_receipt_id=receipt_id,
            notes=note,
        )
        return {
            "parsed": False,
            "parse_skipped": True,
            "reason": skip_reason,
        }

    _apply_parse_result_to_receipt(conn, receipt_id, parse_result)
    conn.commit()
    conn.close()

    update_artifact_workflow(
        ARTIFACTS_DIR,
        artifact_info["artifact_id"],
        status="parsed",
        imported_receipt_id=receipt_id,
    )

    return {
        "parsed": True,
        "parse_skipped": False,
        "reason": None,
    }


def ingest_artifact_info(
    artifact_info: dict,
    *,
    preserve_duplicate_artifact: bool = True,
) -> dict:
    stub_result = insert_receipt_stub_for_artifact(
        artifact_info,
        preserve_duplicate_artifact=preserve_duplicate_artifact,
    )

    if stub_result["is_duplicate"]:
        return {
            **stub_result,
            "parse_result": None,
        }

    parse_result = seed_parse_result_from_artifact(
        stub_result["receipt_id"],
        artifact_info,
    )

    return {
        **stub_result,
        "parse_result": parse_result,
    }


def intake_connector_file(
    *,
    source_file_path: str | Path,
    source_name: str,
    connector_skill: str,
    source_type: str = "portal_fetch",
    source_url: str | None = None,
    page_title: str | None = None,
    account_label: str | None = None,
    original_filename: str | None = None,
    artifact_kind: str | None = None,
    needs_ocr: bool | None = None,
    likely_language: str = "en",
    expected_layout: str = "unknown",
    preserve_duplicate_artifact: bool = True,
) -> dict:
    artifact_info = create_connector_fetched_artifact(
        source_file_path=source_file_path,
        artifacts_root=ARTIFACTS_DIR,
        source_name=source_name,
        connector_skill=connector_skill,
        source_type=source_type,
        source_url=source_url,
        page_title=page_title,
        account_label=account_label,
        original_filename=original_filename,
        artifact_kind=artifact_kind,
        needs_ocr=needs_ocr,
        likely_language=likely_language,
        expected_layout=expected_layout,
    )

    return ingest_artifact_info(
        artifact_info,
        preserve_duplicate_artifact=preserve_duplicate_artifact,
    )
