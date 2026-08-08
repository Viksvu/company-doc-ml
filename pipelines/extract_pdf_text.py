from pathlib import Path

import fitz
import psycopg2

from pipeline_config import get_database_url, resolve_project_path

DATABASE_URL = get_database_url()


def create_document_text_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS document_text (
                raw_document_id BIGINT PRIMARY KEY
                    REFERENCES raw_documents(id)
                    ON DELETE CASCADE,

                extracted_text TEXT NOT NULL,
                extraction_method TEXT NOT NULL,
                ocr_page_count INTEGER NOT NULL DEFAULT 0,

                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)

    conn.commit()


def get_documents_for_text_extraction(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, transaction_id, file_path
            FROM raw_documents
            WHERE download_status = 'downloaded'
              AND detected_file_type = 'pdf'
              AND processing_status = 'ready_for_text_extraction'
            ORDER BY id;
        """)

        return cur.fetchall()


def extract_native_pdf_text(file_path: str) -> dict:
    if not file_path:
        raise ValueError("Missing file path")

    path = resolve_project_path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"File does not exist: {path}")

    page_text_parts = []

    with fitz.open(path) as doc:
        page_count = doc.page_count

        for page_number, page in enumerate(doc, start=1):
            page_text = page.get_text(
                "text",
                sort=True,
            ).strip()

            page_text_parts.append(
                f"--- Page {page_number} ---\n{page_text}"
            )

    extracted_text = "\n\n".join(page_text_parts).strip()

    if not extracted_text:
        raise ValueError("PDF extraction produced no text")

    return {
        "extracted_text": extracted_text,
        "extraction_method": "native_text",
        "ocr_page_count": 0,
        "page_count": page_count,
    }


def save_extracted_text(
    conn,
    document_id: int,
    result: dict,
):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO document_text (
                    raw_document_id,
                    extracted_text,
                    extraction_method,
                    ocr_page_count,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (raw_document_id)
                DO UPDATE SET
                    extracted_text = EXCLUDED.extracted_text,
                    extraction_method = EXCLUDED.extraction_method,
                    ocr_page_count = EXCLUDED.ocr_page_count,
                    updated_at = NOW();
            """, (
                document_id,
                result["extracted_text"],
                result["extraction_method"],
                result["ocr_page_count"],
            ))

            cur.execute("""
                UPDATE raw_documents
                SET processing_status = 'ready_for_parsing',
                    page_count = %s,
                    error_message = NULL,
                    updated_at = NOW()
                WHERE id = %s;
            """, (
                result["page_count"],
                document_id,
            ))

        conn.commit()

    except Exception:
        conn.rollback()
        raise


def mark_text_extraction_failed(
    conn,
    document_id: int,
    error: Exception,
):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE raw_documents
                SET processing_status = 'failed_text_extraction',
                    error_message = %s,
                    updated_at = NOW()
                WHERE id = %s;
            """, (
                str(error)[:2000],
                document_id,
            ))

        conn.commit()

    except Exception:
        conn.rollback()
        raise


def main():
    conn = psycopg2.connect(DATABASE_URL)

    try:
        create_document_text_table(conn)

        documents = get_documents_for_text_extraction(conn)

        print(
            f"Found {len(documents)} PDFs "
            "ready for native text extraction"
        )

        for document_id, transaction_id, file_path in documents:
            print(f"Extracting text: {transaction_id}")

            try:
                result = extract_native_pdf_text(file_path)

                save_extracted_text(
                    conn=conn,
                    document_id=document_id,
                    result=result,
                )

                print(
                    f"{transaction_id} -> ready_for_parsing | "
                    f"pages={result['page_count']} | "
                    f"characters={len(result['extracted_text'])}"
                )

            except Exception as error:
                conn.rollback()

                mark_text_extraction_failed(
                    conn=conn,
                    document_id=document_id,
                    error=error,
                )

                print(
                    f"{transaction_id} -> "
                    f"failed_text_extraction | {error}"
                )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
