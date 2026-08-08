import os
from pathlib import Path

import fitz
import psycopg2

from pipeline_config import get_database_url, resolve_project_path
from paddle_ocr_utils import paddle_image_to_text


DATABASE_URL = get_database_url()

OCR_DPI = int(os.getenv("BASIC_OCR_DPI", "180"))
BASIC_OCR_MAX_IMAGE_SIDE = int(
    os.getenv("BASIC_OCR_MAX_IMAGE_SIDE", "1800")
)
BASIC_OCR_MAX_SCANNED_PAGES = int(
    os.getenv("BASIC_OCR_MAX_SCANNED_PAGES", "0")
)

MIN_NATIVE_TEXT_PER_PAGE = 50


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

                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """)

    conn.commit()


def get_documents_for_ocr(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, transaction_id, file_path
            FROM raw_documents
            WHERE download_status = 'downloaded'
              AND detected_file_type = 'pdf'
              AND processing_status IN ('ready_for_ocr', 'failed_ocr')
            ORDER BY id;
        """)

        return cur.fetchall()


def render_page_to_image(page):
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "Missing Pillow dependency. Install Pillow in the pipeline "
            "environment before running PaddleOCR."
        ) from error

    pixmap = page.get_pixmap(
        dpi=OCR_DPI,
        alpha=False,
    )

    mode = "RGB" if pixmap.n < 4 else "RGBA"

    return Image.frombytes(
        mode,
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )


def extract_pdf_text_with_ocr(file_path: str) -> dict:
    if not file_path:
        raise ValueError("Missing file path")

    path = resolve_project_path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"File does not exist: {path}")

    page_text_parts = []
    ocr_page_count = 0
    skipped_ocr_page_count = 0
    scanned_page_count = 0

    with fitz.open(path) as doc:
        page_count = doc.page_count

        for page_number, page in enumerate(doc, start=1):
            native_text = page.get_text(
                "text",
                sort=True,
            ).strip()

            if len(native_text) >= MIN_NATIVE_TEXT_PER_PAGE:
                page_text = native_text

            else:
                scanned_page_count += 1

                if (
                    BASIC_OCR_MAX_SCANNED_PAGES > 0
                    and scanned_page_count > BASIC_OCR_MAX_SCANNED_PAGES
                ):
                    page_text = (
                        "[OCR skipped by BASIC_OCR_MAX_SCANNED_PAGES]"
                    )
                    skipped_ocr_page_count += 1
                    page_text_parts.append(
                        f"--- Page {page_number} ---\n{page_text}"
                    )
                    continue

                image = render_page_to_image(page)
                print(
                    f"OCR page {page_number}/{page_count} with PaddleOCR "
                    f"({image.width}x{image.height})",
                    flush=True,
                )
                page_text = paddle_image_to_text(
                    image,
                    max_side=BASIC_OCR_MAX_IMAGE_SIDE,
                )

                ocr_page_count += 1

            page_text_parts.append(
                f"--- Page {page_number} ---\n{page_text}"
            )

    extracted_text = "\n\n".join(page_text_parts).strip()

    if not extracted_text:
        raise ValueError("OCR completed but produced no text")

    if skipped_ocr_page_count > 0:
        extraction_method = "partial_paddle_ocr"
    elif ocr_page_count == page_count:
        extraction_method = "paddle_ocr"
    elif ocr_page_count > 0:
        extraction_method = "hybrid_paddle_ocr"
    else:
        extraction_method = "native_text"

    return {
        "extracted_text": extracted_text,
        "page_count": page_count,
        "ocr_page_count": ocr_page_count,
        "extraction_method": extraction_method,
    }


def save_ocr_result(
    conn,
    document_id: int,
    result: dict,
):
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


def mark_ocr_failed(
    conn,
    document_id: int,
    error: Exception,
):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE raw_documents
            SET processing_status = 'failed_ocr',
                error_message = %s,
                updated_at = NOW()
            WHERE id = %s;
        """, (
            str(error)[:2000],
            document_id,
        ))

    conn.commit()


def main():
    conn = psycopg2.connect(DATABASE_URL)

    try:
        create_document_text_table(conn)

        documents = get_documents_for_ocr(conn)

        print(f"Found {len(documents)} documents requiring OCR")

        for document_id, transaction_id, file_path in documents:
            print(f"OCR processing: {transaction_id}")

            try:
                result = extract_pdf_text_with_ocr(file_path)

                save_ocr_result(
                    conn=conn,
                    document_id=document_id,
                    result=result,
                )

                print(
                    f"{transaction_id} -> ready_for_parsing | "
                    f"pages={result['page_count']} | "
                    f"ocr_pages={result['ocr_page_count']} | "
                    f"method={result['extraction_method']}"
                )

            except Exception as error:
                conn.rollback()

                mark_ocr_failed(
                    conn=conn,
                    document_id=document_id,
                    error=error,
                )

                print(f"{transaction_id} -> failed_ocr | {error}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
