from pathlib import Path
import shutil
import sqlite3
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.claw_client import (
    ClawConfigError,
    ClawRequestError,
    get_line_item_suggestion_bundle,
)
from app.artifacts import create_artifact_package_from_bytes, read_manifest, update_artifact_workflow
from app.learning import (
    ensure_learning_schema,
    capture_approved_observations_for_receipt,
    capture_repair_manual_add_observation,
    capture_repair_merge_observations,
    capture_repair_suppression_observations,
    remove_observations_for_receipt,
    record_receipt_suggestion_run_metrics,
    record_openclaw_invocations,
    record_receipt_approval_outcome_metrics,
    generate_learning_proposals,
    get_learning_dashboard_summary,
    get_receipt_decision_telemetry,
    list_profile_proposals,
    update_profile_proposal_status,
    list_recent_learning_observations,
    list_item_mapping_candidate_rollups,
    list_recognized_repair_patterns,
    seed_suppression_pattern_proposal,
)
from app.intake import ingest_artifact_info
from app.parsing.detectors import detect_upload_artifact
from app.review_state import load_review_state, save_review_state

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
DB_PATH = DATA_DIR / "receipts.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Receipt Tracker")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

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


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_receipts_schema(conn):
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(receipts)").fetchall()
    }

    if "artifact_id" not in existing_columns:
        conn.execute("ALTER TABLE receipts ADD COLUMN artifact_id TEXT")


def ensure_receipt_items_schema(conn):
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(receipt_items)").fetchall()
    }

    if "category_source_raw" not in existing_columns:
        conn.execute("ALTER TABLE receipt_items ADD COLUMN category_source_raw TEXT")

    if "is_suppressed" not in existing_columns:
        conn.execute(
            "ALTER TABLE receipt_items ADD COLUMN is_suppressed INTEGER NOT NULL DEFAULT 0"
        )

    if "suppression_reason" not in existing_columns:
        conn.execute("ALTER TABLE receipt_items ADD COLUMN suppression_reason TEXT")

    if "source_item_code" not in existing_columns:
        conn.execute("ALTER TABLE receipt_items ADD COLUMN source_item_code TEXT")

    if "source_item_detail_hint" not in existing_columns:
        conn.execute("ALTER TABLE receipt_items ADD COLUMN source_item_detail_hint TEXT")


def init_db():
    conn = get_db_connection()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_id TEXT,
            image_path TEXT NOT NULL,
            image_hash TEXT NOT NULL UNIQUE,
            merchant_raw TEXT,
            merchant_canonical TEXT,
            purchase_date TEXT,
            purchase_time TEXT,
            subtotal REAL,
            tax REAL,
            total REAL,
            currency TEXT NOT NULL DEFAULT 'USD',
            ocr_text_raw TEXT,
            receipt_confidence REAL,
            status TEXT NOT NULL,
            review_notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS receipt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            line_number INTEGER NOT NULL,
            item_text_raw TEXT NOT NULL,
            item_name_normalized TEXT,
            quantity REAL,
            unit TEXT,
            unit_price REAL,
            line_total REAL,
            category TEXT NOT NULL DEFAULT 'Uncategorized',
            category_source_raw TEXT,
            item_confidence REAL,
            needs_review INTEGER NOT NULL DEFAULT 0,
            review_notes TEXT,
            source_item_code TEXT,
            source_item_detail_hint TEXT,
            is_suppressed INTEGER NOT NULL DEFAULT 0,
            suppression_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
        )
        """
    )

    ensure_receipts_schema(conn)
    ensure_receipt_items_schema(conn)
    ensure_learning_schema(conn)

    conn.commit()
    conn.close()


def load_receipt_with_items(receipt_id: int):
    conn = get_db_connection()

    receipt = conn.execute(
        "SELECT * FROM receipts WHERE id = ?",
        (receipt_id,),
    ).fetchone()

    items = conn.execute(
        """
        SELECT *
        FROM receipt_items
        WHERE receipt_id = ?
        ORDER BY line_number, id
        """,
        (receipt_id,),
    ).fetchall()

    conn.close()
    return receipt, items


def split_active_and_suppressed_items(items):
    active_items = []
    suppressed_items = []

    for item in items:
        if int(item["is_suppressed"] or 0) == 1:
            suppressed_items.append(item)
        else:
            active_items.append(item)

    return active_items, suppressed_items


def parse_selected_item_ids_csv(value: str) -> list[int]:
    if not value:
        return []

    ids = []
    for raw_part in str(value).split(","):
        raw_part = raw_part.strip()
        if not raw_part:
            continue
        try:
            item_id = int(raw_part)
        except ValueError:
            continue
        if item_id > 0:
            ids.append(item_id)

    return sorted(set(ids))


def choose_anchor_after_item_removal(items, removed_item_ids: list[int]) -> str:
    removed_set = {int(item_id) for item_id in removed_item_ids}
    active_items, _ = split_active_and_suppressed_items(items)

    target_line_numbers = [
        int(item["line_number"])
        for item in active_items
        if int(item["id"]) in removed_set
    ]

    remaining_items = [
        item
        for item in active_items
        if int(item["id"]) not in removed_set
    ]

    if not remaining_items:
        return "items-panel"

    if not target_line_numbers:
        return f"item-{remaining_items[0]['id']}"

    first_removed_line = min(target_line_numbers)

    previous_candidate = None
    for item in remaining_items:
        if int(item["line_number"]) < first_removed_line:
            previous_candidate = item

    if previous_candidate is not None:
        return f"item-{previous_candidate['id']}"

    return f"item-{remaining_items[0]['id']}"


def build_suggestions_by_item_id(items, suggestions):
    line_to_item_id = {item["line_number"]: item["id"] for item in items}
    suggestions_by_item_id = {}

    for suggestion in suggestions:
        line_number = suggestion.get("line_number")
        item_id = line_to_item_id.get(line_number)
        if item_id is not None:
            suggestions_by_item_id[item_id] = suggestion

    return suggestions_by_item_id


def build_suggestion_notice(stats: dict) -> str:
    total_review_items = stats.get("total_review_items", 0)
    local_mapping_hints = stats.get("local_mapping_hints", stats.get("learned_exact_matches", 0))
    local_repair_hints = stats.get("local_repair_hints", 0)
    local_handled = stats.get(
        "locally_handled_before_openclaw",
        local_mapping_hints + local_repair_hints,
    )
    remaining_for_openclaw = stats.get("remaining_for_openclaw", 0)
    openclaw_returned = stats.get("openclaw_returned", 0)
    lane_labels = stats.get("openclaw_lane_labels") or {}
    used_lanes = [lane for lane in stats.get("openclaw_lanes") or [] if lane]
    distinct_lane_labels = []
    for lane in used_lanes:
        label = lane_labels.get(lane) or lane.replace("_", " ").title()
        if label not in distinct_lane_labels:
            distinct_lane_labels.append(label)
    lane_text = ", ".join(distinct_lane_labels)

    if total_review_items == 0:
        return "This receipt has no review-needed items, so no suggestion call was made."

    if stats.get("fully_covered_locally") or stats.get("fully_covered_by_learned"):
        if local_repair_hints > 0 and local_mapping_hints > 0:
            return (
                f"Local checks handled all {total_review_items} review item(s): "
                f"{local_mapping_hints} learned mapping hint(s) and "
                f"{local_repair_hints} repair hint(s). OpenClaw was not called."
            )
        if local_repair_hints > 0:
            return (
                f"Local repair hints handled all {total_review_items} review item(s). "
                f"OpenClaw was not called."
            )
        return (
            f"Learned memory handled all {total_review_items} review item(s). "
            f"OpenClaw was not called."
        )

    lane_suffix = ""
    if distinct_lane_labels:
        lane_suffix = f" across {len(distinct_lane_labels)} AI lane(s): {lane_text}."

    if local_handled > 0:
        detail_parts = []
        if local_mapping_hints > 0:
            detail_parts.append(f"{local_mapping_hints} learned mapping hint(s)")
        if local_repair_hints > 0:
            detail_parts.append(f"{local_repair_hints} repair hint(s)")
        detail_text = ", ".join(detail_parts) if detail_parts else f"{local_handled} local hint(s)"
        return (
            f"Local checks handled {local_handled} item(s) before OpenClaw "
            f"({detail_text}). Sent {remaining_for_openclaw} unresolved item(s) to OpenClaw, "
            f"which returned {openclaw_returned} suggestion(s){lane_suffix}"
        )

    return (
        f"Sent {remaining_for_openclaw} review item(s) to OpenClaw, "
        f"which returned {openclaw_returned} suggestion(s){lane_suffix}"
    )


def fetch_suggestions_for_receipt(receipt, items):
    item_dicts = [dict(item) for item in items]
    bundle = get_line_item_suggestion_bundle(dict(receipt), item_dicts)
    suggestions_by_item_id = build_suggestions_by_item_id(items, bundle["suggestions"])
    return suggestions_by_item_id, bundle["stats"], bundle.get("openclaw_invocations", [])


def build_receipt_detail_url(
    receipt_id: int,
    *,
    review_token: str | None = None,
    anchor: str | None = None,
) -> str:
    url = f"/receipts/{receipt_id}"
    if review_token:
        url += f"?review_token={review_token}"
    if anchor:
        url += f"#{anchor}"
    return url


def _safe_remove_path(path: Path | None) -> None:
    if path is None:
        return

    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def delete_receipts_with_cleanup(receipt_ids: list[int]) -> int:
    normalized_ids = sorted({int(receipt_id) for receipt_id in receipt_ids if int(receipt_id) > 0})
    if not normalized_ids:
        return 0

    conn = get_db_connection()
    placeholders = ",".join("?" for _ in normalized_ids)

    rows = conn.execute(
        f"""
        SELECT id, artifact_id, image_path
        FROM receipts
        WHERE id IN ({placeholders})
        """,
        normalized_ids,
    ).fetchall()

    if not rows:
        conn.close()
        return 0

    artifact_paths: list[Path] = []
    image_paths: list[Path] = []

    for row in rows:
        remove_observations_for_receipt(conn, int(row["id"]))

        if row["artifact_id"]:
            artifact_paths.append(ARTIFACTS_DIR / str(row["artifact_id"]))

        if row["image_path"]:
            image_paths.append(Path(str(row["image_path"])))

    conn.execute(
        f"DELETE FROM receipt_items WHERE receipt_id IN ({placeholders})",
        normalized_ids,
    )
    conn.execute(
        f"DELETE FROM receipt_run_metrics WHERE receipt_id IN ({placeholders})",
        normalized_ids,
    )
    conn.execute(
        f"DELETE FROM openclaw_invocations WHERE receipt_id IN ({placeholders})",
        normalized_ids,
    )
    conn.execute(
        f"DELETE FROM receipts WHERE id IN ({placeholders})",
        normalized_ids,
    )

    conn.execute("DELETE FROM profile_proposals")

    conn.commit()
    conn.close()

    for artifact_path in artifact_paths:
        _safe_remove_path(artifact_path)

    for image_path in image_paths:
        _safe_remove_path(image_path)

    return len(rows)


def _fallback_media_type_from_path(path_value: str | None) -> str | None:
    suffix = Path(str(path_value or "")).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
        return f"image/{suffix.lstrip('.')}"
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".html", ".htm"}:
        return "text/html"
    return None


def build_receipt_artifact_context(receipt) -> dict:
    media_type = _fallback_media_type_from_path(receipt["image_path"] if receipt else None)
    source_type = "manual_upload"
    source_name = "manual"
    source_page_title = None
    source_url = None
    connector_skill = None
    acquisition_mode = "human_triggered"
    artifact_kind = None
    expected_layout = None

    artifact_id = receipt["artifact_id"] if receipt else None
    if artifact_id:
        try:
            _, manifest = read_manifest(ARTIFACTS_DIR, str(artifact_id))
        except Exception:
            manifest = None

        if manifest:
            source = manifest.get("source") or {}
            parser_hints = manifest.get("parser_hints") or {}
            artifacts = manifest.get("artifacts") or []
            primary_artifact = next((row for row in artifacts if row.get("role") == "primary"), artifacts[0] if artifacts else {}) or {}

            media_type = primary_artifact.get("media_type") or media_type
            source_type = source.get("type") or source_type
            source_name = source.get("name") or source_name
            source_page_title = source.get("page_title")
            source_url = source.get("source_url")
            connector_skill = source.get("connector_skill")
            acquisition_mode = source.get("acquisition_mode") or acquisition_mode
            artifact_kind = parser_hints.get("artifact_kind") or artifact_kind
            expected_layout = parser_hints.get("expected_layout") or expected_layout

    normalized_source = (source_type or "").strip().lower()
    normalized_name = (source_name or "").strip().lower()
    normalized_layout = (expected_layout or "").strip().lower()
    normalized_media = (media_type or "").strip().lower()

    if normalized_source == "merchant_saved_html" or "bjs" in normalized_name or "bjs" in normalized_layout:
        source_badge_label = "BJ's HTML Import"
    elif normalized_media.startswith("image/"):
        source_badge_label = "Image OCR Upload"
    elif normalized_media in {"text/html", "application/xhtml+xml"}:
        source_badge_label = "Structured HTML Import"
    elif normalized_media == "application/pdf":
        source_badge_label = "PDF Artifact"
    else:
        source_badge_label = (source_type or "artifact_intake").replace("_", " ").title()

    if normalized_media.startswith("image/"):
        evidence_panel_title = "Raw OCR Text"
        evidence_panel_helper = "This is the OCR output captured from an uploaded image artifact before review and learning reuse."
        evidence_empty_text = "No OCR text available."
    elif normalized_media in {"text/html", "application/xhtml+xml"}:
        evidence_panel_title = "Normalized Source Text"
        evidence_panel_helper = "This is normalized evidence text derived from the saved HTML source and used for deterministic parsing before any AI escalation."
        evidence_empty_text = "No normalized source text available."
    elif normalized_media == "application/pdf":
        evidence_panel_title = "Extracted Evidence Text"
        evidence_panel_helper = "This is extracted evidence text derived from the uploaded PDF artifact for downstream review and learning."
        evidence_empty_text = "No extracted evidence text available."
    else:
        evidence_panel_title = "Source Evidence Text"
        evidence_panel_helper = "This is the normalized source evidence currently stored for this artifact."
        evidence_empty_text = "No source evidence text available."

    return {
        "source_type": source_type,
        "source_name": source_name,
        "source_badge_label": source_badge_label,
        "source_page_title": source_page_title,
        "source_url": source_url,
        "connector_skill": connector_skill,
        "acquisition_mode": acquisition_mode,
        "artifact_kind": artifact_kind or "unknown",
        "expected_layout": expected_layout or "unknown",
        "primary_media_type": media_type or "unknown",
        "evidence_panel_title": evidence_panel_title,
        "evidence_panel_helper": evidence_panel_helper,
        "evidence_empty_text": evidence_empty_text,
    }


def render_receipt_detail(
    request: Request,
    receipt_id: int,
    suggestions_by_item_id: dict | None = None,
    suggestion_error: str | None = None,
    suggestion_notice: str | None = None,
    suggestion_stats: dict | None = None,
    review_token: str | None = None,
):
    receipt, all_items = load_receipt_with_items(receipt_id)

    if receipt is None:
        return HTMLResponse("Receipt not found", status_code=404)

    active_items, suppressed_items = split_active_and_suppressed_items(all_items)

    conn = get_db_connection()
    decision_telemetry = get_receipt_decision_telemetry(conn, receipt_id)
    conn.close()

    artifact_context = build_receipt_artifact_context(receipt)

    return templates.TemplateResponse(
        "receipt_detail.html",
        {
            "request": request,
            "receipt": receipt,
            "items": active_items,
            "suppressed_items": suppressed_items,
            "categories": CATEGORY_CHOICES,
            "suggestions_by_item_id": suggestions_by_item_id or {},
            "suggestion_error": suggestion_error,
            "suggestion_notice": suggestion_notice,
            "suggestion_stats": suggestion_stats or {},
            "decision_telemetry": decision_telemetry,
            "artifact_context": artifact_context,
            "review_token": review_token or "",
        },
    )


def render_learning_proposals_page(
    request: Request,
    *,
    notice: str | None = None,
    merchant_scope_input: str = "",
    min_confirmations: int = 2,
    status_filter: str = "all",
):
    normalized_status_filter = (status_filter or "all").strip().lower()
    if normalized_status_filter not in {"all", "pending", "approved", "rejected"}:
        normalized_status_filter = "all"

    normalized_merchant_scope_input = (merchant_scope_input or "").strip()
    normalized_min_confirmations = max(2, int(min_confirmations))

    conn = get_db_connection()
    summary = get_learning_dashboard_summary(conn)
    proposals = list_profile_proposals(
        conn,
        status_filter=None if normalized_status_filter == "all" else normalized_status_filter,
        merchant_scope=normalized_merchant_scope_input or None,
    )
    recent_observations = list_recent_learning_observations(
        conn,
        merchant_scope=normalized_merchant_scope_input or None,
        limit=40,
    )
    candidate_rollups = list_item_mapping_candidate_rollups(
        conn,
        merchant_scope=normalized_merchant_scope_input or None,
        min_confirmations=normalized_min_confirmations,
        limit=80,
    )
    recognized_repair_patterns = list_recognized_repair_patterns(
        conn,
        merchant_scope=normalized_merchant_scope_input or None,
        min_confirmations=normalized_min_confirmations,
        limit=40,
    )
    conn.close()

    candidate_summary = {
        "ready_count": sum(1 for row in candidate_rollups if row["state"] == "ready"),
        "needs_more_count": sum(1 for row in candidate_rollups if row["state"] == "needs_more_confirmations"),
        "conflict_count": sum(1 for row in candidate_rollups if row["state"] == "conflict"),
    }

    return templates.TemplateResponse(
        "learning_proposals.html",
        {
            "request": request,
            "notice": notice,
            "summary": summary,
            "proposals": proposals,
            "merchant_scope_input": normalized_merchant_scope_input,
            "min_confirmations": normalized_min_confirmations,
            "status_filter": normalized_status_filter,
            "recent_observations": recent_observations,
            "candidate_rollups": candidate_rollups,
            "candidate_summary": candidate_summary,
            "recognized_repair_patterns": recognized_repair_patterns,
        },
    )


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "message": "Receipt Tracker is running."},
    )


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, notice: str = ""):
    return templates.TemplateResponse("upload.html", {"request": request, "notice": notice})


@app.post("/upload")
async def upload_receipt(receipt_file: UploadFile | None = File(None)):
    if receipt_file is None or not (receipt_file.filename or "").strip():
        message = "Choose a file to upload. The intake router accepts receipt images, PDFs, and saved HTML receipt pages."
        return RedirectResponse(
            url=f"/upload?notice={quote(message)}",
            status_code=303,
        )

    receipt_file.file.seek(0)
    file_bytes = receipt_file.file.read()
    if not file_bytes:
        message = "The uploaded file was empty. Choose a receipt image, PDF, or saved HTML receipt page and try again."
        return RedirectResponse(
            url=f"/upload?notice={quote(message)}",
            status_code=303,
        )

    detection = detect_upload_artifact(file_bytes, receipt_file.filename)
    if not detection["is_supported"]:
        message = (
            f"{detection['detection_label']}: {detection['detection_detail']} "
            "Supported uploads include .jpg, .jpeg, .png, .webp, .bmp, .gif, .tif, .tiff, .pdf, .html, and .htm."
        )
        return RedirectResponse(
            url=f"/upload?notice={quote(message)}",
            status_code=303,
        )

    artifact_info = create_artifact_package_from_bytes(
        artifacts_root=ARTIFACTS_DIR,
        file_bytes=file_bytes,
        original_filename=detection["normalized_filename"],
        source=detection["source"],
        parser_hints=detection["parser_hints"],
        media_type_override=detection["media_type"],
    )

    intake_result = ingest_artifact_info(
        artifact_info,
        preserve_duplicate_artifact=False,
    )

    return RedirectResponse(
        url=f"/receipts/{intake_result['receipt_id']}",
        status_code=303,
    )


@app.get("/receipts", response_class=HTMLResponse)
def list_receipts(request: Request, notice: str = ""):
    conn = get_db_connection()

    rows = conn.execute(
        """
        SELECT id, artifact_id, image_path, merchant_canonical, merchant_raw, purchase_date, total, status, created_at
        FROM receipts
        ORDER BY id DESC
        """
    ).fetchall()

    summary = conn.execute(
        """
        SELECT
            COUNT(*) AS total_count,
            COALESCE(SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END), 0) AS approved_count,
            COALESCE(SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END), 0) AS rejected_count,
            COALESCE(SUM(CASE WHEN status NOT IN ('approved', 'rejected') THEN 1 ELSE 0 END), 0) AS in_review_count
        FROM receipts
        """
    ).fetchone()

    conn.close()

    return templates.TemplateResponse(
        "receipts.html",
        {
            "request": request,
            "receipts": rows,
            "notice": notice,
            "summary": dict(summary),
        },
    )


@app.post("/receipts/bulk-delete")
async def bulk_delete_receipts(request: Request):
    form = await request.form()
    raw_values = form.getlist("receipt_ids")

    normalized_ids: list[int] = []
    for raw_value in raw_values:
        try:
            receipt_id = int(str(raw_value))
        except Exception:
            continue

        if receipt_id > 0:
            normalized_ids.append(receipt_id)

    if not normalized_ids:
        message = "No receipts were selected for deletion."
        return RedirectResponse(
            url=f"/receipts?notice={quote(message)}",
            status_code=303,
        )

    deleted_count = delete_receipts_with_cleanup(normalized_ids)

    if deleted_count <= 0:
        message = "No matching receipts were deleted."
    else:
        message = (
            f"Deleted {deleted_count} receipt(s), removed associated artifacts and learning observations, "
            f"and cleared proposals so they can be regenerated from the remaining data."
        )

    return RedirectResponse(
        url=f"/receipts?notice={quote(message)}",
        status_code=303,
    )


@app.get("/receipts/{receipt_id}", response_class=HTMLResponse)
def receipt_detail(request: Request, receipt_id: int, review_token: str | None = None):
    review_state = load_review_state(review_token, receipt_id=receipt_id) if review_token else None

    return render_receipt_detail(
        request,
        receipt_id,
        suggestions_by_item_id=review_state["suggestions_by_item_id"] if review_state else None,
        suggestion_notice=review_state["suggestion_notice"] if review_state else None,
        suggestion_stats=review_state["suggestion_stats"] if review_state else None,
        review_token=review_state["token"] if review_state else None,
    )


@app.post("/receipts/{receipt_id}/suggestions", response_class=HTMLResponse)
async def receipt_suggestions(request: Request, receipt_id: int):
    receipt, all_items = load_receipt_with_items(receipt_id)

    if receipt is None:
        return HTMLResponse("Receipt not found", status_code=404)

    active_items, _ = split_active_and_suppressed_items(all_items)
    uncertain_items = [item for item in active_items if item["needs_review"]]

    if not uncertain_items:
        return render_receipt_detail(
            request,
            receipt_id,
            suggestion_notice="This receipt has no review-needed active items, so no suggestion call was made.",
        )

    try:
        suggestions_by_item_id, suggestion_stats, openclaw_invocations = fetch_suggestions_for_receipt(receipt, active_items)
    except ClawConfigError as exc:
        return render_receipt_detail(
            request,
            receipt_id,
            suggestion_error=str(exc),
        )
    except ClawRequestError as exc:
        if getattr(exc, "telemetry", None):
            conn = get_db_connection()
            record_openclaw_invocations(conn, receipt, exc.telemetry, source_run_id=None)
            conn.commit()
            conn.close()
        return render_receipt_detail(
            request,
            receipt_id,
            suggestion_error=str(exc),
        )

    conn = get_db_connection()
    metrics_row = record_receipt_suggestion_run_metrics(
        conn,
        receipt,
        active_items,
        suggestions_by_item_id,
        suggestion_stats,
        openclaw_invocations,
    )
    conn.commit()
    conn.close()

    suggestion_stats = {**suggestion_stats, "metrics_run_id": metrics_row.get("id")}

    review_token = save_review_state(
        receipt_id=receipt_id,
        suggestions_by_item_id=suggestions_by_item_id,
        suggestion_stats=suggestion_stats,
        suggestion_notice=build_suggestion_notice(suggestion_stats),
    )

    return RedirectResponse(
        url=build_receipt_detail_url(
            receipt_id,
            review_token=review_token,
            anchor="items-panel",
        ),
        status_code=303,
    )


@app.post("/receipts/{receipt_id}/items/{item_id}/apply-suggestion")
async def apply_item_suggestion(
    receipt_id: int,
    item_id: int,
    mode: str = Form(...),
    suggested_name: str = Form(""),
    suggested_category: str = Form(""),
    suggestion_source: str = Form("suggestion"),
    review_token: str = Form(""),
):
    receipt, all_items = load_receipt_with_items(receipt_id)

    if receipt is None:
        return HTMLResponse("Receipt not found", status_code=404)

    target_item = None
    for item in all_items:
        if item["id"] == item_id:
            target_item = item
            break

    if target_item is None:
        return HTMLResponse("Item not found", status_code=404)

    new_name = target_item["item_name_normalized"]
    new_category = target_item["category"]

    suggested_name = (suggested_name or "").strip()
    suggested_category = (suggested_category or "").strip()
    suggestion_source = (suggestion_source or "suggestion").strip()

    if mode in {"name", "both"} and suggested_name:
        new_name = suggested_name

    if mode in {"category", "both"} and suggested_category and suggested_category in CATEGORY_CHOICES:
        new_category = suggested_category

    notes = target_item["review_notes"] or ""
    note_parts = []
    if mode in {"name", "both"} and suggested_name:
        note_parts.append(f"name={suggested_name}")
    if mode in {"category", "both"} and suggested_category:
        note_parts.append(f"category={suggested_category}")

    updated_notes = notes.strip()
    if note_parts:
        apply_note = f"Applied suggestion ({mode}, source={suggestion_source}): {', '.join(note_parts)}."
        if apply_note not in updated_notes:
            updated_notes = f"{updated_notes} {apply_note}".strip()

    conn = get_db_connection()
    conn.execute(
        """
        UPDATE receipt_items
        SET item_name_normalized = ?,
            category = ?,
            review_notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND receipt_id = ?
        """,
        (
            new_name,
            new_category,
            updated_notes,
            item_id,
            receipt_id,
        ),
    )

    if receipt["status"] == "approved":
        capture_approved_observations_for_receipt(conn, receipt_id, item_id=item_id)

    conn.commit()
    conn.close()

    current_state = load_review_state(review_token, receipt_id=receipt_id) if review_token else None
    next_review_token = None

    if current_state:
        next_review_token = save_review_state(
            receipt_id=receipt_id,
            suggestions_by_item_id=current_state["suggestions_by_item_id"],
            suggestion_stats=current_state["suggestion_stats"],
            suggestion_notice="Applied suggestion using the current review pass only. No suggestion engine re-run occurred.",
        )

    return RedirectResponse(
        url=build_receipt_detail_url(
            receipt_id,
            review_token=next_review_token,
            anchor=f"item-{item_id}",
        ),
        status_code=303,
    )


@app.post("/receipts/{receipt_id}/items/add")
async def add_manual_item(
    receipt_id: int,
    item_text_raw: str = Form(...),
    item_name_normalized: str = Form(...),
    category: str = Form(...),
    line_total_raw: str = Form(""),
    review_notes: str = Form(""),
    review_token: str = Form(""),
):
    receipt, all_items = load_receipt_with_items(receipt_id)

    if receipt is None:
        return HTMLResponse("Receipt not found", status_code=404)

    raw_text = (item_text_raw or "").strip()
    normalized_name = (item_name_normalized or "").strip()
    category_value = (category or "").strip()
    review_notes_value = (review_notes or "").strip()

    if not raw_text:
        return HTMLResponse("Raw item text is required.", status_code=400)

    if not normalized_name:
        return HTMLResponse("Normalized item name is required.", status_code=400)

    if category_value not in CATEGORY_CHOICES:
        category_value = "Uncategorized"

    line_total = None
    line_total_value = (line_total_raw or "").strip()
    if line_total_value:
        try:
            line_total = float(line_total_value)
        except ValueError:
            return HTMLResponse("Line total must be numeric.", status_code=400)

    next_line_number = max((int(item["line_number"]) for item in all_items), default=0) + 1

    final_review_notes = "Manually added line item."
    if review_notes_value:
        final_review_notes = f"{final_review_notes} {review_notes_value}"

    conn = get_db_connection()
    cur = conn.execute(
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
            is_suppressed,
            suppression_reason,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            receipt_id,
            next_line_number,
            raw_text,
            normalized_name,
            None,
            None,
            None,
            line_total,
            category_value,
            None,
            1.0,
            0,
            final_review_notes,
        ),
    )
    new_item_id = cur.lastrowid

    capture_repair_manual_add_observation(
        conn,
        receipt,
        item_id=new_item_id,
        line_number=next_line_number,
        item_text_raw=raw_text,
        item_name_normalized=normalized_name,
        category=category_value,
        line_total=line_total,
        review_notes=final_review_notes,
    )

    if receipt["status"] == "approved":
        capture_approved_observations_for_receipt(conn, receipt_id, item_id=new_item_id)

    conn.commit()
    conn.close()

    current_state = load_review_state(review_token, receipt_id=receipt_id) if review_token else None
    next_review_token = None

    if current_state:
        next_review_token = save_review_state(
            receipt_id=receipt_id,
            suggestions_by_item_id=current_state["suggestions_by_item_id"],
            suggestion_stats=current_state["suggestion_stats"],
            suggestion_notice=(
                "Added a manual line item. Existing suggestions were preserved from the current review pass; "
                "rerun suggestions if you want the new item included in a fresh suggestion run."
            ),
        )

    return RedirectResponse(
        url=build_receipt_detail_url(
            receipt_id,
            review_token=next_review_token,
            anchor=f"item-{new_item_id}",
        ),
        status_code=303,
    )


@app.post("/receipts/{receipt_id}/items/merge-selected")
async def merge_selected_items(
    receipt_id: int,
    selected_item_ids: str = Form(""),
    item_name_normalized: str = Form(...),
    category: str = Form(...),
    line_total_raw: str = Form(""),
    review_notes: str = Form(""),
    review_token: str = Form(""),
):
    receipt, all_items = load_receipt_with_items(receipt_id)

    if receipt is None:
        return HTMLResponse("Receipt not found", status_code=404)

    selected_ids = parse_selected_item_ids_csv(selected_item_ids)
    if len(selected_ids) < 2:
        return HTMLResponse("Select at least two active lines to merge.", status_code=400)

    active_items, _ = split_active_and_suppressed_items(all_items)
    selected_rows = [
        item for item in active_items
        if int(item["id"]) in set(selected_ids)
    ]

    if len(selected_rows) != len(selected_ids):
        return HTMLResponse("One or more selected items were not found as active lines.", status_code=400)

    selected_rows = sorted(selected_rows, key=lambda row: (int(row["line_number"]), int(row["id"])))

    normalized_name = (item_name_normalized or "").strip()
    if not normalized_name:
        return HTMLResponse("Merged normalized item name is required.", status_code=400)

    category_value = (category or "").strip()
    if category_value not in CATEGORY_CHOICES:
        category_value = "Uncategorized"

    line_total = None
    line_total_value = (line_total_raw or "").strip()
    if line_total_value:
        try:
            line_total = float(line_total_value)
        except ValueError:
            return HTMLResponse("Merged line total must be numeric.", status_code=400)
    else:
        numeric_line_totals = [
            float(item["line_total"])
            for item in selected_rows
            if item["line_total"] is not None
        ]
        if numeric_line_totals:
            line_total = round(sum(numeric_line_totals), 2)

    merged_raw_text = " / ".join(
        (item["item_text_raw"] or "").strip()
        for item in selected_rows
        if (item["item_text_raw"] or "").strip()
    )

    source_line_numbers = [str(int(item["line_number"])) for item in selected_rows]
    final_review_notes = f"Merged from lines {', '.join(source_line_numbers)}."
    user_review_notes = (review_notes or "").strip()
    if user_review_notes:
        final_review_notes = f"{final_review_notes} {user_review_notes}"

    next_line_number = max((int(item["line_number"]) for item in all_items), default=0) + 1

    conn = get_db_connection()

    cur = conn.execute(
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
            is_suppressed,
            suppression_reason,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            receipt_id,
            next_line_number,
            merged_raw_text,
            normalized_name,
            None,
            None,
            None,
            line_total,
            category_value,
            None,
            1.0,
            0,
            final_review_notes,
        ),
    )
    new_item_id = cur.lastrowid

    suppression_reason = f"Merged into item {new_item_id} from selected repair flow."

    if receipt["status"] == "approved":
        for source_item in selected_rows:
            conn.execute(
                """
                DELETE FROM learning_observations
                WHERE receipt_id = ?
                  AND receipt_item_id = ?
                  AND observation_type = 'approved_item_observation'
                """,
                (receipt_id, int(source_item["id"])),
            )

    conn.execute(
        f"""
        UPDATE receipt_items
        SET is_suppressed = 1,
            suppression_reason = ?,
            needs_review = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE receipt_id = ?
          AND id IN ({",".join("?" for _ in selected_ids)})
        """,
        [suppression_reason, receipt_id] + selected_ids,
    )

    capture_repair_merge_observations(
        conn,
        receipt,
        source_rows=selected_rows,
        merged_item_id=new_item_id,
        merged_line_number=next_line_number,
        merged_item_text_raw=merged_raw_text,
        merged_item_name_normalized=normalized_name,
        merged_category=category_value,
        suppression_reason=suppression_reason,
    )

    if receipt["status"] == "approved":
        capture_approved_observations_for_receipt(conn, receipt_id, item_id=new_item_id)

    conn.commit()
    conn.close()

    current_state = load_review_state(review_token, receipt_id=receipt_id) if review_token else None
    next_review_token = None

    if current_state:
        prior_suggestions = current_state.get("suggestions_by_item_id") or {}
        pruned_suggestions = {
            key: value
            for key, value in prior_suggestions.items()
            if str(key) not in {str(item_id) for item_id in selected_ids}
        }

        next_review_token = save_review_state(
            receipt_id=receipt_id,
            suggestions_by_item_id=pruned_suggestions,
            suggestion_stats=current_state["suggestion_stats"],
            suggestion_notice=(
                "Merged selected lines into one new item. Source fragment lines were suppressed, "
                "existing suggestions were preserved for the remaining items, and no suggestion engine re-run occurred."
            ),
        )

    return RedirectResponse(
        url=build_receipt_detail_url(
            receipt_id,
            review_token=next_review_token,
            anchor=f"item-{new_item_id}",
        ),
        status_code=303,
    )


@app.post("/receipts/{receipt_id}/items/suppress-selected")
async def suppress_selected_items(
    receipt_id: int,
    selected_item_ids: str = Form(""),
    suppression_note: str = Form(""),
    review_token: str = Form(""),
):
    receipt, all_items = load_receipt_with_items(receipt_id)

    if receipt is None:
        return HTMLResponse("Receipt not found", status_code=404)

    selected_ids = parse_selected_item_ids_csv(selected_item_ids)
    if not selected_ids:
        return HTMLResponse("Select at least one active line to suppress.", status_code=400)

    active_items, _ = split_active_and_suppressed_items(all_items)
    selected_rows = [
        item for item in active_items
        if int(item["id"]) in set(selected_ids)
    ]

    if len(selected_rows) != len(selected_ids):
        return HTMLResponse("One or more selected items were not found as active lines.", status_code=400)

    source_line_numbers = [
        str(int(item["line_number"]))
        for item in sorted(selected_rows, key=lambda row: (int(row["line_number"]), int(row["id"])))
    ]

    suppression_reason = f"Suppressed from lines {', '.join(source_line_numbers)}."
    note_value = (suppression_note or "").strip()
    if note_value:
        suppression_reason = f"{suppression_reason} {note_value}"

    conn = get_db_connection()

    if receipt["status"] == "approved":
        for source_item in selected_rows:
            conn.execute(
                """
                DELETE FROM learning_observations
                WHERE receipt_id = ?
                  AND receipt_item_id = ?
                  AND observation_type = 'approved_item_observation'
                """,
                (receipt_id, int(source_item["id"])),
            )

    conn.execute(
        f"""
        UPDATE receipt_items
        SET is_suppressed = 1,
            suppression_reason = ?,
            needs_review = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE receipt_id = ?
          AND id IN ({",".join("?" for _ in selected_ids)})
        """,
        [suppression_reason, receipt_id] + selected_ids,
    )

    capture_repair_suppression_observations(
        conn,
        receipt,
        source_rows=selected_rows,
        suppression_reason=suppression_reason,
    )

    conn.commit()
    conn.close()

    current_state = load_review_state(review_token, receipt_id=receipt_id) if review_token else None
    next_review_token = None

    if current_state:
        prior_suggestions = current_state.get("suggestions_by_item_id") or {}
        pruned_suggestions = {
            key: value
            for key, value in prior_suggestions.items()
            if str(key) not in {str(item_id) for item_id in selected_ids}
        }

        next_review_token = save_review_state(
            receipt_id=receipt_id,
            suggestions_by_item_id=pruned_suggestions,
            suggestion_stats=current_state["suggestion_stats"],
            suggestion_notice=(
                "Suppressed selected lines. Existing suggestions were preserved for remaining items only, "
                "and no suggestion engine re-run occurred."
            ),
        )

    return RedirectResponse(
        url=build_receipt_detail_url(
            receipt_id,
            review_token=next_review_token,
            anchor=choose_anchor_after_item_removal(all_items, selected_ids),
        ),
        status_code=303,
    )


@app.post("/receipts/{receipt_id}/items/{item_id}/delete")
async def delete_item(
    receipt_id: int,
    item_id: int,
    review_token: str = Form(""),
):
    receipt, all_items = load_receipt_with_items(receipt_id)

    if receipt is None:
        return HTMLResponse("Receipt not found", status_code=404)

    target_item = None
    for item in all_items:
        if item["id"] == item_id:
            target_item = item
            break

    if target_item is None:
        return HTMLResponse("Item not found", status_code=404)

    conn = get_db_connection()

    conn.execute(
        """
        DELETE FROM learning_observations
        WHERE receipt_id = ?
          AND receipt_item_id = ?
        """,
        (receipt_id, item_id),
    )

    conn.execute(
        """
        DELETE FROM receipt_items
        WHERE id = ?
          AND receipt_id = ?
        """,
        (item_id, receipt_id),
    )

    conn.commit()
    conn.close()

    current_state = load_review_state(review_token, receipt_id=receipt_id) if review_token else None
    next_review_token = None

    if current_state:
        prior_suggestions = current_state.get("suggestions_by_item_id") or {}
        pruned_suggestions = {
            key: value
            for key, value in prior_suggestions.items()
            if str(key) != str(item_id)
        }

        next_review_token = save_review_state(
            receipt_id=receipt_id,
            suggestions_by_item_id=pruned_suggestions,
            suggestion_stats=current_state["suggestion_stats"],
            suggestion_notice=(
                "Deleted line item. Existing suggestions were preserved for the remaining items only. "
                "No suggestion engine re-run occurred."
            ),
        )

    return RedirectResponse(
        url=build_receipt_detail_url(
            receipt_id,
            review_token=next_review_token,
            anchor=choose_anchor_after_item_removal(all_items, [item_id]),
        ),
        status_code=303,
    )


@app.post("/receipts/{receipt_id}/approve")
async def approve_receipt(
    receipt_id: int,
    merchant_canonical: str = Form(...),
    purchase_date: str = Form(...),
    total: float = Form(...),
    review_notes: str = Form(""),
):
    conn = get_db_connection()

    receipt_row = conn.execute(
        "SELECT artifact_id FROM receipts WHERE id = ?",
        (receipt_id,),
    ).fetchone()
    artifact_id = receipt_row["artifact_id"] if receipt_row else None

    conn.execute(
        """
        UPDATE receipts
        SET merchant_canonical = ?,
            purchase_date = ?,
            total = ?,
            status = 'approved',
            review_notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            merchant_canonical,
            purchase_date,
            total,
            review_notes,
            receipt_id,
        ),
    )

    capture_approved_observations_for_receipt(conn, receipt_id)
    record_receipt_approval_outcome_metrics(conn, receipt_id)

    conn.commit()
    conn.close()

    if artifact_id:
        update_artifact_workflow(
            ARTIFACTS_DIR,
            artifact_id,
            status="approved",
            imported_receipt_id=receipt_id,
        )

    return RedirectResponse(url=f"/receipts/{receipt_id}", status_code=303)


@app.post("/receipts/{receipt_id}/reject")
async def reject_receipt(
    receipt_id: int,
    review_notes: str = Form("Rejected during review."),
):
    conn = get_db_connection()

    receipt_row = conn.execute(
        "SELECT artifact_id FROM receipts WHERE id = ?",
        (receipt_id,),
    ).fetchone()
    artifact_id = receipt_row["artifact_id"] if receipt_row else None

    conn.execute(
        """
        UPDATE receipts
        SET status = 'rejected',
            review_notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            review_notes,
            receipt_id,
        ),
    )

    remove_observations_for_receipt(conn, receipt_id)

    conn.commit()
    conn.close()

    if artifact_id:
        update_artifact_workflow(
            ARTIFACTS_DIR,
            artifact_id,
            status="rejected",
            imported_receipt_id=receipt_id,
        )

    return RedirectResponse(url=f"/receipts/{receipt_id}", status_code=303)


@app.post("/receipts/{receipt_id}/items/{item_id}/update")
async def update_item(
    receipt_id: int,
    item_id: int,
    item_name_normalized: str = Form(...),
    category: str = Form(...),
    line_total_raw: str = Form(""),
    review_notes: str = Form(""),
    review_token: str = Form(""),
):
    conn = get_db_connection()

    receipt_row = conn.execute(
        "SELECT * FROM receipts WHERE id = ?",
        (receipt_id,),
    ).fetchone()

    line_total_value = line_total_raw.strip()
    line_total = float(line_total_value) if line_total_value else None

    conn.execute(
        """
        UPDATE receipt_items
        SET item_name_normalized = ?,
            category = ?,
            line_total = ?,
            review_notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND receipt_id = ?
        """,
        (
            item_name_normalized,
            category,
            line_total,
            review_notes,
            item_id,
            receipt_id,
        ),
    )

    if receipt_row and receipt_row["status"] == "approved":
        capture_approved_observations_for_receipt(conn, receipt_id, item_id=item_id)

    conn.commit()
    conn.close()

    current_state = load_review_state(review_token, receipt_id=receipt_id) if review_token else None
    next_review_token = None

    if current_state:
        next_review_token = save_review_state(
            receipt_id=receipt_id,
            suggestions_by_item_id=current_state["suggestions_by_item_id"],
            suggestion_stats=current_state["suggestion_stats"],
            suggestion_notice="Saved item without re-running suggestion engine. Existing suggestions were preserved from the current review pass.",
        )

    return RedirectResponse(
        url=build_receipt_detail_url(
            receipt_id,
            review_token=next_review_token,
            anchor=f"item-{item_id}",
        ),
        status_code=303,
    )


@app.get("/learning/proposals", response_class=HTMLResponse)
def learning_proposals(
    request: Request,
    status: str = "all",
    merchant: str = "",
    min_confirmations: int = 2,
):
    return render_learning_proposals_page(
        request,
        merchant_scope_input=merchant,
        min_confirmations=min_confirmations,
        status_filter=status,
    )


@app.post("/learning/proposals/generate", response_class=HTMLResponse)
async def learning_proposals_generate(
    request: Request,
    min_confirmations: int = Form(2),
    merchant_scope: str = Form(""),
    status_filter: str = Form("all"),
):
    normalized_min_confirmations = max(2, int(min_confirmations))
    normalized_merchant_scope = (merchant_scope or "").strip()
    normalized_min_confirmations = max(2, int(min_confirmations))

    conn = get_db_connection()
    result = generate_learning_proposals(
        conn,
        min_confirmations=normalized_min_confirmations,
        merchant_scope=normalized_merchant_scope or None,
    )
    conn.commit()
    conn.close()

    mapping = result["mapping"]
    alias = result["alias"]
    suppression = result["suppression"]
    suppression_source_counts = suppression.get("considered_source_observation_counts", {}) or {}
    suppression_manual_count = int(suppression_source_counts.get("repair_suppression", 0) or 0)
    suppression_merge_count = int(suppression_source_counts.get("repair_merge_source", 0) or 0)

    notice = (
        "Proposal generation complete: "
        f"mapping created {mapping['created_count']}, updated {mapping['updated_count']}, "
        f"mapping conflict-skipped {mapping['skipped_conflict_count']}, "
        f"alias created {alias['created_count']}, updated {alias['updated_count']}, "
        f"alias conflict-skipped {alias['skipped_conflict_count']}, "
        f"suppression created {suppression['created_count']}, updated {suppression['updated_count']}, "
        f"suppression existing-skipped {suppression['skipped_existing_count']}, "
        f"recognized fragment evidence {suppression.get('considered_observation_count', 0)} "
        f"across {suppression.get('recognized_group_count', 0)} pattern group(s) "
        f"(auto-ready {suppression.get('ready_group_count', 0)}, seedable below-threshold {suppression.get('seedable_group_count', 0)}, "
        f"manual suppressions {suppression_manual_count}, merge-source fragments {suppression_merge_count})."
    )

    return render_learning_proposals_page(
        request,
        notice=notice,
        merchant_scope_input=normalized_merchant_scope,
        min_confirmations=normalized_min_confirmations,
        status_filter=status_filter,
    )


@app.post("/learning/proposals/suppression-patterns/seed", response_class=HTMLResponse)
async def learning_seed_suppression_pattern(
    request: Request,
    pattern_merchant_key: str = Form(""),
    pattern_key: str = Form(""),
    merchant_scope: str = Form(""),
    min_confirmations: int = Form(2),
    status_filter: str = Form("all"),
):
    normalized_pattern_merchant_key = (pattern_merchant_key or "").strip()
    normalized_pattern_key = (pattern_key or "").strip()
    normalized_view_merchant_scope = (merchant_scope or "").strip()
    normalized_min_confirmations = max(2, int(min_confirmations))

    conn = get_db_connection()
    result = seed_suppression_pattern_proposal(
        conn,
        merchant_key=normalized_pattern_merchant_key,
        pattern_key=normalized_pattern_key,
        min_confirmations=normalized_min_confirmations,
    )
    conn.commit()
    conn.close()

    group = result.get("group") or {}
    if result.get("status") in {"created", "updated"}:
        notice = (
            "Created pending analyst-seeded repair proposal: "
            f"{result.get('merchant_key', normalized_pattern_merchant_key)} / {normalized_pattern_key} "
            f"from {group.get('distinct_receipt_count', 0)} receipt(s), "
            f"{group.get('distinct_artifact_count', 0)} artifact(s), and "
            f"{group.get('evidence_observation_count', 0)} observed repair row(s). "
            "It remains inactive until approved."
        )
    elif result.get("status") == "existing":
        notice = (
            "A proposal already exists for that recognized repair pattern: "
            f"status {result.get('proposal_status', 'unknown')}"
        )
        if result.get("proposal_id"):
            notice += f", proposal #{result['proposal_id']}."
        else:
            notice += "."
    elif result.get("status") == "not_found":
        notice = "Could not find that recognized repair pattern. Refresh the learning page and try again."
    else:
        notice = "Could not seed a proposal for that recognized repair pattern."

    return render_learning_proposals_page(
        request,
        notice=notice,
        merchant_scope_input=normalized_view_merchant_scope,
        min_confirmations=normalized_min_confirmations,
        status_filter=status_filter,
    )


@app.post("/learning/proposals/{proposal_id}/approve")
async def learning_proposal_approve(proposal_id: int):
    conn = get_db_connection()
    update_profile_proposal_status(conn, proposal_id, "approved")
    conn.commit()
    conn.close()

    return RedirectResponse(
        url=f"/learning/proposals#proposal-{proposal_id}",
        status_code=303,
    )


@app.post("/learning/proposals/{proposal_id}/reject")
async def learning_proposal_reject(proposal_id: int):
    conn = get_db_connection()
    update_profile_proposal_status(conn, proposal_id, "rejected")
    conn.commit()
    conn.close()

    return RedirectResponse(
        url=f"/learning/proposals#proposal-{proposal_id}",
        status_code=303,
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request):
    conn = get_db_connection()

    monthly_spend = conn.execute(
        """
        SELECT
            substr(purchase_date, 1, 7) AS month,
            ROUND(SUM(total), 2) AS spend
        FROM receipts
        WHERE status = 'approved'
          AND purchase_date IS NOT NULL
          AND total IS NOT NULL
        GROUP BY substr(purchase_date, 1, 7)
        ORDER BY month
        """
    ).fetchall()

    by_merchant = conn.execute(
        """
        SELECT
            COALESCE(merchant_canonical, merchant_raw, 'Unknown') AS merchant,
            ROUND(SUM(total), 2) AS spend
        FROM receipts
        WHERE status = 'approved'
          AND total IS NOT NULL
        GROUP BY COALESCE(merchant_canonical, merchant_raw, 'Unknown')
        ORDER BY spend DESC
        """
    ).fetchall()

    by_category = conn.execute(
        """
        SELECT
            ri.category,
            ROUND(SUM(ri.line_total), 2) AS spend
        FROM receipt_items ri
        JOIN receipts r ON r.id = ri.receipt_id
        WHERE r.status = 'approved'
          AND ri.line_total IS NOT NULL
          AND COALESCE(ri.is_suppressed, 0) = 0
        GROUP BY ri.category
        ORDER BY spend DESC
        """
    ).fetchall()

    conn.close()

    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "monthly_spend": monthly_spend,
            "by_merchant": by_merchant,
            "by_category": by_category,
        },
    )