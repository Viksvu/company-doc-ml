from pathlib import Path
import re

import fitz
import psycopg2
from psycopg2.extras import Json

from pipeline_config import get_database_url, resolve_project_path

DATABASE_URL = get_database_url()

TEXT_LENGTH_THRESHOLD = 100


def create_pdf_metadata_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE raw_documents
            ADD COLUMN IF NOT EXISTS pdf_metadata JSONB NOT NULL DEFAULT '{}',
            ADD COLUMN IF NOT EXISTS pdf_metadata_company_number TEXT,
            ADD COLUMN IF NOT EXISTS pdf_metadata_company_name TEXT;
        """)

    conn.commit()


def extract_pdf_metadata_company_number(metadata: dict) -> str | None:
    metadata_text = " ".join(
        str(value or "")
        for value in metadata.values()
    )

    match = re.search(
        r"/company/([A-Z]{0,2}\d{6,8})\b",
        metadata_text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    value = match.group(1).upper()

    if value.isdigit():
        value = value.zfill(8)

    return value


def extract_pdf_metadata_company_name(metadata: dict) -> str | None:
    for key in ("title", "subject", "keywords"):
        value = str(metadata.get(key) or "").strip()

        if not value:
            continue

        if re.fullmatch(r"/company/[A-Z]{0,2}\d{6,8}", value, flags=re.IGNORECASE):
            continue

        value = re.sub(r"/company/[A-Z]{0,2}\d{6,8}", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s+", " ", value).strip(" -:_|")

        if not value:
            continue

        if value.lower() in {"companies house", "document"}:
            continue

        if 2 <= len(value) <= 255:
            return value

    return None


def get_pdf_documents(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, transaction_id, file_path, processing_status
            FROM raw_documents
            WHERE download_status = 'downloaded'
              AND detected_file_type = 'pdf'
              AND (
                    processing_status = 'pdf_downloaded'
                    OR pdf_metadata = '{}'::jsonb
              )
        """)
        return cur.fetchall()


def update_pdf_status(
    conn,
    document_id: int,
    is_valid_pdf: bool,
    page_count: int | None,
    processing_status: str,
    pdf_metadata: dict | None = None,
    pdf_metadata_company_number: str | None = None,
    pdf_metadata_company_name: str | None = None,
    inspection_error: str | None = None,
):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE raw_documents
            SET is_valid_pdf = %s,
                page_count = %s,
                processing_status = %s,
                pdf_metadata = %s,
                pdf_metadata_company_number = %s,
                pdf_metadata_company_name = %s,
                inspection_error = %s,
                updated_at = NOW()
            WHERE id = %s;
        """, (
            is_valid_pdf,
            page_count,
            processing_status,
            Json(pdf_metadata or {}),
            pdf_metadata_company_number,
            pdf_metadata_company_name,
            inspection_error,
            document_id,
        ))

    conn.commit()


def inspect_pdf(file_path: str):
    if not file_path:
        return {
            "is_valid_pdf": False,
            "page_count": None,
            "processing_status": "failed_pdf_validation",
            "pdf_metadata": {},
            "pdf_metadata_company_number": None,
            "pdf_metadata_company_name": None,
            "inspection_error": "Missing file path",
        }

    path = resolve_project_path(file_path)

    if not path.exists():
        return {
            "is_valid_pdf": False,
            "page_count": None,
            "processing_status": "failed_pdf_validation",
            "pdf_metadata": {},
            "pdf_metadata_company_number": None,
            "pdf_metadata_company_name": None,
            "inspection_error": "File does not exist",
        }

    try:
        text_parts = []

        with fitz.open(path) as doc:
            page_count = doc.page_count
            pdf_metadata = dict(doc.metadata or {})

            for page in doc:
                text_parts.append(page.get_text())

        extracted_text = "\n".join(text_parts).strip()
        minimum_expected_text = page_count * 50

        if len(extracted_text) > minimum_expected_text:
            processing_status = "ready_for_text_extraction"
        else:
            processing_status = "ready_for_ocr"

        return {
            "is_valid_pdf": True,
            "page_count": page_count,
            "processing_status": processing_status,
            "pdf_metadata": pdf_metadata,
            "pdf_metadata_company_number": extract_pdf_metadata_company_number(
                pdf_metadata
            ),
            "pdf_metadata_company_name": extract_pdf_metadata_company_name(
                pdf_metadata
            ),
            "inspection_error": None,
        }

    except Exception as error:
        return {
            "is_valid_pdf": False,
            "page_count": None,
            "processing_status": "failed_pdf_validation",
            "pdf_metadata": {},
            "pdf_metadata_company_number": None,
            "pdf_metadata_company_name": None,
            "inspection_error": str(error),
        }


def main():
    conn = psycopg2.connect(DATABASE_URL)

    try:
        create_pdf_metadata_columns(conn)
        documents = get_pdf_documents(conn)
        print(f"Found {len(documents)} PDF documents to inspect")

        for document_id, transaction_id, file_path, current_status in documents:
            print(f"Inspecting {transaction_id}")

            result = inspect_pdf(file_path)
            processing_status = result["processing_status"]

            if current_status != "pdf_downloaded":
                processing_status = current_status

            update_pdf_status(
                conn=conn,
                document_id=document_id,
                is_valid_pdf=result["is_valid_pdf"],
                page_count=result["page_count"],
                processing_status=processing_status,
                pdf_metadata=result["pdf_metadata"],
                pdf_metadata_company_number=result["pdf_metadata_company_number"],
                pdf_metadata_company_name=result["pdf_metadata_company_name"],
                inspection_error=result["inspection_error"],
            )

            print(
                f"{transaction_id} -> "
                f"{processing_status} | "
                f"pages={result['page_count']}"
            )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
