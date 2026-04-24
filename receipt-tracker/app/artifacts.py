from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import mimetypes
import re
import secrets
import shutil
import socket
import hashlib


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _slugify(value: str | None, default: str = "artifact") -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or default


def _safe_filename(filename: str | None, default_name: str = "artifact.bin") -> str:
    name = (filename or "").strip()
    if not name:
        name = default_name

    original = Path(name)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", original.stem).strip("._-")
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", original.suffix)

    if not stem:
        stem = "artifact"

    if not suffix:
        suffix = ".bin"

    return f"{stem}{suffix}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _guess_media_type(filename: str) -> str:
    media_type, _ = mimetypes.guess_type(filename)
    return media_type or "application/octet-stream"


def _default_parser_hints(media_type: str) -> dict:
    if media_type.startswith("image/"):
        artifact_kind = "image_receipt"
        needs_ocr = True
    elif media_type == "application/pdf":
        artifact_kind = "pdf_receipt"
        needs_ocr = True
    elif media_type in {"text/html", "application/xhtml+xml"}:
        artifact_kind = "html_receipt_page"
        needs_ocr = False
    else:
        artifact_kind = "unknown"
        needs_ocr = False

    return {
        "merchant_hint": None,
        "artifact_kind": artifact_kind,
        "needs_ocr": needs_ocr,
        "likely_language": "en",
        "expected_layout": "unknown",
    }


def _build_artifact_id(source_name: str, original_filename: str) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    source_slug = _slugify(source_name, "source")
    file_slug = _slugify(Path(original_filename).stem, "artifact")
    rand = secrets.token_hex(3)
    return f"art_{timestamp}_{source_slug}_{file_slug}_{rand}"


def _write_manifest(manifest_path: Path, manifest: dict) -> None:
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def read_manifest(artifacts_root: Path, artifact_id: str) -> tuple[Path, dict]:
    manifest_path = Path(artifacts_root) / artifact_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest_path, manifest


def create_artifact_package_from_bytes(
    *,
    artifacts_root: Path,
    file_bytes: bytes,
    original_filename: str,
    source: dict,
    parser_hints: dict | None = None,
    media_type_override: str | None = None,
) -> dict:
    artifacts_root = Path(artifacts_root)
    artifacts_root.mkdir(parents=True, exist_ok=True)

    safe_filename = _safe_filename(original_filename)
    media_type = media_type_override or _guess_media_type(safe_filename)
    artifact_id = _build_artifact_id(source.get("name", "source"), safe_filename)

    artifact_dir = artifacts_root / artifact_id
    raw_dir = artifact_dir / "raw"
    derived_dir = artifact_dir / "derived"
    raw_dir.mkdir(parents=True, exist_ok=True)
    derived_dir.mkdir(parents=True, exist_ok=True)

    primary_path = raw_dir / safe_filename
    primary_path.write_bytes(file_bytes)

    primary_sha256 = _sha256_bytes(file_bytes)
    primary_bytes = len(file_bytes)
    primary_relative_path = f"raw/{safe_filename}"
    now = _utc_now_iso()

    manifest = {
        "schema_version": "artifact-manifest.v1",
        "artifact_id": artifact_id,
        "created_at": now,
        "source": {
            "type": source.get("type", "manual_upload"),
            "name": source.get("name", "manual"),
            "connector_skill": source.get("connector_skill"),
            "acquisition_mode": source.get("acquisition_mode", "human_triggered"),
            "operator_present": bool(source.get("operator_present", True)),
            "account_label": source.get("account_label"),
            "source_url": source.get("source_url"),
            "page_title": source.get("page_title"),
            "original_filename": original_filename,
        },
        "artifacts": [
            {
                "role": "primary",
                "path": primary_relative_path,
                "media_type": media_type,
                "sha256": primary_sha256,
                "bytes": primary_bytes,
            }
        ],
        "primary_renderable_path": primary_relative_path,
        "parser_hints": dict(parser_hints or _default_parser_hints(media_type)),
        "integrity": {
            "immutable": True,
            "captured_by": source.get("connector_skill") or source.get("name") or "artifact-intake",
            "host": socket.gethostname(),
        },
        "workflow": {
            "status": "fetched",
            "imported_receipt_id": None,
            "notes": None,
            "updated_at": now,
        },
    }

    manifest_path = artifact_dir / "manifest.json"
    _write_manifest(manifest_path, manifest)

    return {
        "artifact_id": artifact_id,
        "artifact_dir": artifact_dir,
        "manifest_path": manifest_path,
        "primary_path": primary_path,
        "primary_relative_path": primary_relative_path,
        "primary_sha256": primary_sha256,
        "primary_bytes": primary_bytes,
        "primary_media_type": media_type,
        "source": manifest.get("source", {}),
        "parser_hints": manifest.get("parser_hints", {}),
    }


def create_artifact_package_from_file(
    *,
    source_file_path: str | Path,
    artifacts_root: Path,
    source: dict,
    parser_hints: dict | None = None,
    original_filename: str | None = None,
    media_type_override: str | None = None,
) -> dict:
    source_path = Path(source_file_path)
    if not source_path.exists():
        raise FileNotFoundError(f"Source file does not exist: {source_path}")

    source_bytes = source_path.read_bytes()
    final_original_filename = original_filename or source_path.name

    artifact_info = create_artifact_package_from_bytes(
        artifacts_root=artifacts_root,
        file_bytes=source_bytes,
        original_filename=final_original_filename,
        source=source,
        parser_hints=parser_hints,
        media_type_override=media_type_override,
    )

    try:
        shutil.copystat(source_path, artifact_info["primary_path"], follow_symlinks=True)
    except Exception:
        pass

    artifact_info["primary_sha256"] = _sha256_file(artifact_info["primary_path"])
    return artifact_info


def create_manual_upload_artifact(upload_file, artifacts_root: Path) -> dict:
    upload_file.file.seek(0)
    file_bytes = upload_file.file.read()
    original_filename = upload_file.filename or "upload.bin"

    source = {
        "type": "manual_upload",
        "name": "manual",
        "connector_skill": None,
        "acquisition_mode": "human_triggered",
        "operator_present": True,
        "account_label": None,
        "source_url": None,
        "page_title": None,
    }

    return create_artifact_package_from_bytes(
        artifacts_root=artifacts_root,
        file_bytes=file_bytes,
        original_filename=original_filename,
        source=source,
    )


def create_connector_fetched_artifact(
    *,
    source_file_path: str | Path,
    artifacts_root: Path,
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
) -> dict:
    source_path = Path(source_file_path)
    safe_filename = _safe_filename(original_filename or source_path.name)
    media_type = _guess_media_type(safe_filename)

    parser_hints = _default_parser_hints(media_type)
    if artifact_kind is not None:
        parser_hints["artifact_kind"] = artifact_kind
    if needs_ocr is not None:
        parser_hints["needs_ocr"] = needs_ocr

    parser_hints["likely_language"] = likely_language
    parser_hints["expected_layout"] = expected_layout

    source = {
        "type": source_type,
        "name": source_name,
        "connector_skill": connector_skill,
        "acquisition_mode": "human_triggered",
        "operator_present": True,
        "account_label": account_label,
        "source_url": source_url,
        "page_title": page_title,
    }

    return create_artifact_package_from_file(
        source_file_path=source_path,
        artifacts_root=artifacts_root,
        source=source,
        parser_hints=parser_hints,
        original_filename=safe_filename,
    )


def update_artifact_workflow(
    artifacts_root: Path,
    artifact_id: str,
    *,
    status: str | None = None,
    imported_receipt_id: int | None = None,
    notes: str | None = None,
) -> dict:
    manifest_path, manifest = read_manifest(artifacts_root, artifact_id)

    workflow = manifest.setdefault("workflow", {})
    if status is not None:
        workflow["status"] = status
    if imported_receipt_id is not None:
        workflow["imported_receipt_id"] = imported_receipt_id
    if notes is not None:
        workflow["notes"] = notes
    workflow["updated_at"] = _utc_now_iso()

    _write_manifest(manifest_path, manifest)
    return manifest