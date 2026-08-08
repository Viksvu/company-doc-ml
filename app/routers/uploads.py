import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status as http_status,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import DATABASE_URL
from app.database import get_db
from app.models import UploadedFile


APP_DIR = Path(__file__).resolve().parents[2]
PIPELINES_DIR = APP_DIR / "pipelines"
UPLOAD_DIR = APP_DIR / "data" / "raw" / "uploads"

SUPPORTED_FILE_TYPES = {"pdf", "html"}

if str(PIPELINES_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINES_DIR))

router = APIRouter(
    prefix="/uploads",
    tags=["uploads"],
)


def calculate_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def detect_file_type(content: bytes) -> str:
    start = content[:300].lstrip()
    lower = start.lower()

    if start.startswith(b"%PDF-"):
        return "pdf"

    if (
        lower.startswith(b"<!doctype html")
        or lower.startswith(b"<html")
        or b"<html" in lower
    ):
        return "html"

    return "unknown"


def get_file_suffix(detected_file_type: str, filename: str | None) -> str:
    if detected_file_type == "pdf":
        return ".pdf"

    if detected_file_type == "html":
        return ".html"

    suffix = Path(filename or "").suffix.lower()

    return suffix or ".bin"


def get_initial_processing_status(detected_file_type: str) -> str:
    if detected_file_type == "pdf":
        return "pdf_downloaded"

    if detected_file_type == "html":
        return "html_detected"

    return "unknown_file_type"


def normalise_company_number(value: str | None) -> str | None:
    if not value:
        return None

    value = "".join(value.split()).upper()

    return value or None


def project_relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(APP_DIR))
    except ValueError:
        return str(path)


def resolve_project_path(value: str | None) -> Path | None:
    if not value:
        return None

    path = Path(value)

    if path.is_absolute():
        return path

    return APP_DIR / path


def ensure_upload_columns(db: Session) -> None:
    db.execute(text("""
        ALTER TABLE uploaded_files
        ADD COLUMN IF NOT EXISTS raw_document_id INTEGER,
        ADD COLUMN IF NOT EXISTS upload_batch_id VARCHAR,
        ADD COLUMN IF NOT EXISTS upload_mode VARCHAR DEFAULT 'blind',
        ADD COLUMN IF NOT EXISTS selected_company_number VARCHAR,
        ADD COLUMN IF NOT EXISTS selected_company_name VARCHAR,
        ADD COLUMN IF NOT EXISTS parse_requested BOOLEAN DEFAULT FALSE;
    """))
    db.commit()


def table_exists(db: Session, table_name: str) -> bool:
    result = db.execute(
        text("SELECT to_regclass(:table_name);"),
        {"table_name": f"public.{table_name}"},
    ).scalar_one()

    return result is not None


def ensure_raw_documents_table(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS raw_documents (
            id BIGSERIAL PRIMARY KEY,
            transaction_id TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL,

            file_path TEXT,
            content_type TEXT,
            detected_file_type TEXT,
            file_size_bytes BIGINT,
            sha256_hash TEXT,

            download_status TEXT NOT NULL,
            processing_status TEXT NOT NULL,
            error_message TEXT,

            is_valid_pdf BOOLEAN,
            page_count INTEGER,
            inspection_error TEXT,
            pdf_metadata JSONB NOT NULL DEFAULT '{}',
            pdf_metadata_company_number TEXT,
            pdf_metadata_company_name TEXT,

            downloaded_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    """))
    db.execute(text("""
        ALTER TABLE raw_documents
        ADD COLUMN IF NOT EXISTS pdf_metadata JSONB NOT NULL DEFAULT '{}',
        ADD COLUMN IF NOT EXISTS pdf_metadata_company_number TEXT,
        ADD COLUMN IF NOT EXISTS pdf_metadata_company_name TEXT;
    """))
    db.commit()


def find_company_by_number(
    db: Session,
    company_number: str | None,
) -> dict | None:
    company_number = normalise_company_number(company_number)

    if not company_number:
        return None

    result = db.execute(
        text("""
            SELECT company_number, official_company_name
            FROM companies
            WHERE company_number = :company_number
            LIMIT 1;
        """),
        {"company_number": company_number},
    ).mappings().first()

    return dict(result) if result else None


def create_raw_document(
    db: Session,
    *,
    file_path: Path,
    file: UploadFile,
    content: bytes,
    detected_file_type: str,
) -> int:
    transaction_id = f"upload_{uuid4().hex}"

    result = db.execute(
        text("""
            INSERT INTO raw_documents (
                transaction_id,
                source,
                file_path,
                content_type,
                detected_file_type,
                file_size_bytes,
                sha256_hash,
                download_status,
                processing_status,
                error_message,
                pdf_metadata,
                pdf_metadata_company_number,
                pdf_metadata_company_name,
                downloaded_at,
                updated_at
            )
            VALUES (
                :transaction_id,
                'user_upload',
                :file_path,
                :content_type,
                :detected_file_type,
                :file_size_bytes,
                :sha256_hash,
                'downloaded',
                :processing_status,
                NULL,
                CAST(:pdf_metadata AS jsonb),
                :pdf_metadata_company_number,
                :pdf_metadata_company_name,
                :downloaded_at,
                NOW()
            )
            RETURNING id;
        """),
        {
            "transaction_id": transaction_id,
            "file_path": project_relative_path(file_path),
            "content_type": file.content_type,
            "detected_file_type": detected_file_type,
            "file_size_bytes": len(content),
            "sha256_hash": calculate_sha256(content),
            "processing_status": get_initial_processing_status(detected_file_type),
            "pdf_metadata": json.dumps({
                "source": "user_upload",
            }),
            "pdf_metadata_company_number": None,
            "pdf_metadata_company_name": None,
            "downloaded_at": datetime.now(UTC),
        },
    )

    return result.scalar_one()


def get_raw_document(conn, raw_document_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                id,
                transaction_id,
                file_path,
                detected_file_type,
                processing_status,
                pdf_metadata_company_number,
                pdf_metadata_company_name
            FROM raw_documents
            WHERE id = %s;
        """, (raw_document_id,))

        row = cur.fetchone()

    if not row:
        raise RuntimeError(f"Raw document not found: {raw_document_id}")

    return {
        "id": row[0],
        "transaction_id": row[1],
        "file_path": row[2],
        "detected_file_type": row[3],
        "processing_status": row[4],
        "pdf_metadata_company_number": row[5],
        "pdf_metadata_company_name": row[6],
    }


def refresh_raw_document_source_metadata(
    conn,
    raw_document_id: int,
) -> tuple[str | None, str | None]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                pdf_metadata_company_number,
                pdf_metadata_company_name
            FROM raw_documents
            WHERE id = %s;
        """, (raw_document_id,))

        row = cur.fetchone()

    if not row:
        return None, None

    return row[0], row[1]


def process_uploaded_pdf(
    conn,
    raw_document: dict,
) -> None:
    import inspect_pdfs
    import ocr_documents
    import extract_pdf_text

    ocr_documents.create_document_text_table(conn)
    file_path = resolve_project_path(raw_document["file_path"])
    inspection = inspect_pdfs.inspect_pdf(str(file_path))
    inspect_pdfs.update_pdf_status(
        conn=conn,
        document_id=raw_document["id"],
        is_valid_pdf=inspection["is_valid_pdf"],
        page_count=inspection["page_count"],
        processing_status=inspection["processing_status"],
        pdf_metadata=inspection["pdf_metadata"],
        pdf_metadata_company_number=inspection["pdf_metadata_company_number"],
        pdf_metadata_company_name=inspection["pdf_metadata_company_name"],
        inspection_error=inspection["inspection_error"],
    )

    raw_document = get_raw_document(conn, raw_document["id"])

    if raw_document["processing_status"] == "ready_for_text_extraction":
        try:
            result = extract_pdf_text.extract_native_pdf_text(
                str(file_path)
            )
            extract_pdf_text.save_extracted_text(
                conn=conn,
                document_id=raw_document["id"],
                result=result,
            )
        except Exception as error:
            conn.rollback()
            extract_pdf_text.mark_text_extraction_failed(
                conn=conn,
                document_id=raw_document["id"],
                error=error,
            )

    elif raw_document["processing_status"] == "ready_for_ocr":
        try:
            result = ocr_documents.extract_pdf_text_with_ocr(
                str(file_path)
            )
            ocr_documents.save_ocr_result(
                conn=conn,
                document_id=raw_document["id"],
                result=result,
            )
        except Exception as error:
            conn.rollback()
            ocr_documents.mark_ocr_failed(
                conn=conn,
                document_id=raw_document["id"],
                error=error,
            )


def process_uploaded_html(
    conn,
    raw_document: dict,
) -> None:
    import extract_html_text

    extract_html_text.create_document_text_table(conn)

    try:
        result = extract_html_text.extract_html_text(
            str(resolve_project_path(raw_document["file_path"]))
        )

        if result["is_error_page"]:
            extract_html_text.mark_html_for_retry(
                conn=conn,
                document_id=raw_document["id"],
                reason=(
                    "HTML response appears to be an error "
                    "or placeholder page"
                ),
            )
            return

        extract_html_text.save_extracted_text(
            conn=conn,
            document_id=raw_document["id"],
            result=result,
        )
    except Exception as error:
        conn.rollback()
        extract_html_text.mark_html_extraction_failed(
            conn=conn,
            document_id=raw_document["id"],
            error=error,
        )


def process_uploaded_metadata(
    conn,
    raw_document_id: int,
) -> None:
    import extract_metadata

    extract_metadata.create_source_metadata_columns(conn)
    extract_metadata.create_metadata_table(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT extracted_text
            FROM document_text
            WHERE raw_document_id = %s
              AND extracted_text IS NOT NULL
              AND LENGTH(TRIM(extracted_text)) > 0;
        """, (raw_document_id,))

        row = cur.fetchone()

    if not row:
        return

    known_company_number, known_company_name = (
        refresh_raw_document_source_metadata(conn, raw_document_id)
    )

    try:
        metadata = extract_metadata.extract_metadata(
            row[0],
            known_company_number=known_company_number,
            known_company_name=known_company_name,
        )
        extract_metadata.save_metadata(
            conn=conn,
            raw_document_id=raw_document_id,
            metadata=metadata,
        )
    except Exception as error:
        conn.rollback()
        extract_metadata.save_extraction_failure(
            conn=conn,
            raw_document_id=raw_document_id,
            error=error,
        )


def run_uploaded_document_pipeline(raw_document_ids: list[int]) -> None:
    import psycopg2
    import match_document_companies

    raw_document_ids = list(dict.fromkeys(raw_document_ids))

    if not raw_document_ids:
        return

    conn = psycopg2.connect(DATABASE_URL)

    try:
        for raw_document_id in raw_document_ids:
            raw_document = get_raw_document(conn, raw_document_id)

            if raw_document["detected_file_type"] == "pdf":
                process_uploaded_pdf(conn, raw_document)
            elif raw_document["detected_file_type"] == "html":
                process_uploaded_html(conn, raw_document)

            process_uploaded_metadata(conn, raw_document_id)

        match_document_companies.run_matching(
            candidate_threshold=match_document_companies.DEFAULT_CANDIDATE_THRESHOLD,
            auto_accept_threshold=(
                match_document_companies.DEFAULT_AUTO_ACCEPT_THRESHOLD
            ),
            exact_only=False,
            dry_run=False,
            raw_document_ids=raw_document_ids,
        )

    finally:
        conn.close()


@router.get("/companies")
def search_companies(
    q: str,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    query = q.strip()

    if len(query) < 2:
        return []

    limit = max(1, min(limit, 50))
    normalised_number = normalise_company_number(query)

    db.execute(text("SET LOCAL statement_timeout = '3s';"))
    rows = db.execute(
        text("""
            SELECT
                company_number,
                official_company_name,
                company_status
            FROM companies
            WHERE company_number = :exact_number
               OR company_number LIKE :prefix_number
               OR lower(official_company_name) LIKE lower(:name_prefix)
            ORDER BY
                CASE WHEN company_number = :exact_number THEN 0 ELSE 1 END,
                CASE WHEN company_status = 'Active' THEN 0 ELSE 1 END,
                official_company_name
            LIMIT :limit;
        """),
        {
            "exact_number": normalised_number,
            "prefix_number": f"{normalised_number}%",
            "name_prefix": f"{query}%",
            "limit": limit,
        },
    ).mappings().all()

    return [dict(row) for row in rows]


def get_file_pipeline_status(row: dict) -> dict:
    raw_status = row["raw_processing_status"]
    match_method = row["match_method"]

    failed_statuses = {
        "failed_ocr",
        "failed_text_extraction",
        "failed_html_extraction",
        "failed_pdf_validation",
        "metadata_failed",
        "unknown_file_type",
    }

    if raw_status in failed_statuses or row["extraction_error"]:
        status = "failed"
        message = (
            row["extraction_error"]
            or row["raw_error_message"]
            or row["inspection_error"]
            or "Document processing failed."
        )
    elif match_method == "selected_company_conflict":
        status = "conflict"
        message = "Selected company differs from document company metadata."
    elif row["review_required"]:
        status = "needs_review"
        message = "Document matched, but review is required."
    elif row["is_accepted"]:
        status = "matched"
        message = "Document matched successfully."
    else:
        status = "processing"
        message = "Document is still being processed."

    return {
        "uploaded_file_id": row["uploaded_file_id"],
        "raw_document_id": row["raw_document_id"],
        "filename": row["filename"],
        "status": status,
        "message": message,
        "raw_processing_status": raw_status,
        "selected_company_number": row["selected_company_number"],
        "selected_company_name": row["selected_company_name"],
        "extracted_company_number": row["extracted_company_number"],
        "extracted_company_name": row["extracted_company_name"],
        "pdf_metadata_company_number": row["pdf_metadata_company_number"],
        "pdf_metadata_company_name": row["pdf_metadata_company_name"],
        "matched_company_number": row["matched_company_number"],
        "matched_company_name": row["matched_company_name"],
        "match_method": match_method,
        "match_score": (
            float(row["match_score"])
            if row["match_score"] is not None
            else None
        ),
        "review_required": row["review_required"],
    }


@router.get("/batches/{upload_batch_id}/status")
def get_upload_batch_status(
    upload_batch_id: str,
    db: Session = Depends(get_db),
):
    ensure_upload_columns(db)
    has_document_metadata = table_exists(db, "document_metadata")
    has_document_matches = table_exists(db, "document_company_matches")

    metadata_join = """
            LEFT JOIN document_metadata dm
                ON dm.raw_document_id = uf.raw_document_id
    """ if has_document_metadata else ""

    match_join = """
            LEFT JOIN document_company_matches dcm
                ON dcm.raw_document_id = uf.raw_document_id
    """ if has_document_matches else ""

    metadata_columns = """
                dm.company_number AS extracted_company_number,
                dm.company_name AS extracted_company_name,
                dm.extraction_error,
    """ if has_document_metadata else """
                NULL AS extracted_company_number,
                NULL AS extracted_company_name,
                NULL AS extraction_error,
    """

    match_columns = """
                dcm.match_method,
                dcm.match_score,
                dcm.is_accepted,
                dcm.review_required,
                dcm.matched_company_number,
                dcm.matched_company_name
    """ if has_document_matches else """
                NULL AS match_method,
                NULL AS match_score,
                NULL AS is_accepted,
                NULL AS review_required,
                NULL AS matched_company_number,
                NULL AS matched_company_name
    """

    rows = db.execute(
        text(f"""
            SELECT
                uf.id AS uploaded_file_id,
                uf.raw_document_id,
                uf.filename,
                uf.selected_company_number,
                uf.selected_company_name,
                rd.processing_status AS raw_processing_status,
                rd.error_message AS raw_error_message,
                rd.inspection_error,
                rd.pdf_metadata_company_number,
                rd.pdf_metadata_company_name,
{metadata_columns}
{match_columns}
            FROM uploaded_files uf
            LEFT JOIN raw_documents rd
                ON rd.id = uf.raw_document_id
{metadata_join}
{match_join}
            WHERE uf.upload_batch_id = :upload_batch_id
            ORDER BY uf.id;
        """),
        {"upload_batch_id": upload_batch_id},
    ).mappings().all()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="Upload batch was not found",
        )

    files = [
        get_file_pipeline_status(dict(row))
        for row in rows
    ]
    statuses = {file["status"] for file in files}

    if "failed" in statuses:
        batch_status = "completed_with_errors"
    elif "conflict" in statuses or "needs_review" in statuses:
        batch_status = "completed_with_review"
    elif statuses == {"matched"}:
        batch_status = "completed"
    else:
        batch_status = "processing"

    return {
        "upload_batch_id": upload_batch_id,
        "status": batch_status,
        "has_failures": "failed" in statuses,
        "has_conflicts": "conflict" in statuses,
        "files": files,
    }


@router.post("/", status_code=http_status.HTTP_202_ACCEPTED)
def upload_files(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    upload_mode: str = Form("blind"),
    selected_company_number: str | None = Form(None),
    db: Session = Depends(get_db),
):
    if upload_mode not in {"blind", "selected_company"}:
        raise HTTPException(
            status_code=400,
            detail="upload_mode must be either blind or selected_company",
        )

    ensure_upload_columns(db)
    ensure_raw_documents_table(db)

    selected_company = None
    upload_batch_id = uuid4().hex

    if upload_mode == "selected_company":
        selected_company = find_company_by_number(db, selected_company_number)

        if not selected_company:
            raise HTTPException(
                status_code=400,
                detail="Selected company number was not found in companies",
            )

    uploaded_results = []
    raw_document_ids = []
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    for file in files:
        content = file.file.read()

        if not content:
            raise HTTPException(
                status_code=400,
                detail=f"{file.filename} is empty",
            )

        detected_file_type = detect_file_type(content)

        if detected_file_type not in SUPPORTED_FILE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{file.filename} is not supported. Upload PDF or HTML "
                    "documents for this pipeline."
                ),
            )

        suffix = get_file_suffix(detected_file_type, file.filename)
        unique_name = f"{uuid4().hex}{suffix}"
        file_path = UPLOAD_DIR / unique_name

        with file_path.open("wb") as buffer:
            buffer.write(content)

        raw_document_id = create_raw_document(
            db,
            file_path=file_path,
            file=file,
            content=content,
            detected_file_type=detected_file_type,
        )
        raw_document_ids.append(raw_document_id)

        uploaded_file = UploadedFile(
            filename=file.filename,
            content_type=file.content_type,
            file_path=project_relative_path(file_path),
            upload_batch_id=upload_batch_id,
            raw_document_id=raw_document_id,
            upload_mode=upload_mode,
            selected_company_number=(
                selected_company["company_number"]
                if selected_company
                else None
            ),
            selected_company_name=(
                selected_company["official_company_name"]
                if selected_company
                else None
            ),
            parse_requested=True,
        )

        db.add(uploaded_file)
        db.flush()

        uploaded_results.append({
            "id": uploaded_file.id,
            "upload_batch_id": upload_batch_id,
            "raw_document_id": raw_document_id,
            "filename": uploaded_file.filename,
            "content_type": uploaded_file.content_type,
            "file_path": uploaded_file.file_path,
            "detected_file_type": detected_file_type,
            "processing_status": get_initial_processing_status(detected_file_type),
            "selected_company_number": uploaded_file.selected_company_number,
            "selected_company_name": uploaded_file.selected_company_name,
        })

    db.commit()

    background_tasks.add_task(
        run_uploaded_document_pipeline,
        raw_document_ids,
    )

    return {
        "upload_batch_id": upload_batch_id,
        "status_url": f"/uploads/batches/{upload_batch_id}/status",
        "uploaded": uploaded_results,
        "upload_mode": upload_mode,
        "process_now": True,
        "next_step": "Pipeline started in the background.",
    }
