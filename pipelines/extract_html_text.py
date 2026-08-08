from pathlib import Path

import psycopg2
from bs4 import BeautifulSoup

from pipeline_config import get_database_url, resolve_project_path

DATABASE_URL = get_database_url()

MIN_HTML_TEXT_LENGTH = 50


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


def get_html_documents(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, transaction_id, file_path
            FROM raw_documents
            WHERE download_status = 'downloaded'
              AND detected_file_type = 'html'
              AND processing_status = 'html_detected'
            ORDER BY id;
        """)

        return cur.fetchall()


def looks_like_error_page(
    extracted_text: str,
    title: str,
) -> bool:
    combined_text = f"{title}\n{extracted_text}".lower()

    error_phrases = [
        "access denied",
        "document unavailable",
        "document not found",
        "page not found",
        "internal server error",
        "service unavailable",
        "please try again",
        "error generating document",
        "request failed",
        "unauthorized",
        "forbidden",
        "sign in",
        "log in",
    ]

    return any(
        phrase in combined_text
        for phrase in error_phrases
    )


def extract_html_text(file_path: str) -> dict:
    if not file_path:
        raise ValueError("Missing file path")

    path = resolve_project_path(file_path)

    if not path.exists():
        raise FileNotFoundError(
            f"File does not exist: {path}"
        )

    html_content = path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    soup = BeautifulSoup(
        html_content,
        "html.parser",
    )

    title = ""

    if soup.title:
        title = soup.title.get_text(
            separator=" ",
            strip=True,
        )

    # Remove content that should not become document text.
    for element in soup([
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
    ]):
        element.decompose()

    extracted_text = soup.get_text(
        separator="\n",
        strip=True,
    )

    if not extracted_text:
        raise ValueError(
            "HTML extraction produced no text"
        )

    if len(extracted_text) < MIN_HTML_TEXT_LENGTH:
        raise ValueError(
            "HTML contains too little usable text"
        )

    if looks_like_error_page(
        extracted_text=extracted_text,
        title=title,
    ):
        return {
            "is_error_page": True,
            "extracted_text": extracted_text,
            "title": title,
        }

    return {
        "is_error_page": False,
        "extracted_text": extracted_text,
        "title": title,
        "extraction_method": "html",
        "ocr_page_count": 0,
        "page_count": None,
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
                    extracted_text =
                        EXCLUDED.extracted_text,
                    extraction_method =
                        EXCLUDED.extraction_method,
                    ocr_page_count =
                        EXCLUDED.ocr_page_count,
                    updated_at = NOW();
            """, (
                document_id,
                result["extracted_text"],
                result["extraction_method"],
                result["ocr_page_count"],
            ))

            cur.execute("""
                UPDATE raw_documents
                SET processing_status =
                        'ready_for_parsing',
                    error_message = NULL,
                    updated_at = NOW()
                WHERE id = %s;
            """, (
                document_id,
            ))

        conn.commit()

    except Exception:
        conn.rollback()
        raise


def mark_html_for_retry(
    conn,
    document_id: int,
    reason: str,
):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE raw_documents
                SET processing_status =
                        'download_retry_required',
                    error_message = %s,
                    updated_at = NOW()
                WHERE id = %s;
            """, (
                reason[:2000],
                document_id,
            ))

        conn.commit()

    except Exception:
        conn.rollback()
        raise


def mark_html_extraction_failed(
    conn,
    document_id: int,
    error: Exception,
):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE raw_documents
                SET processing_status =
                        'failed_html_extraction',
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

        documents = get_html_documents(conn)

        print(
            f"Found {len(documents)} HTML documents "
            "ready for extraction"
        )

        for document_id, transaction_id, file_path in documents:
            print(
                f"Extracting HTML: {transaction_id}"
            )

            try:
                result = extract_html_text(file_path)

                if result["is_error_page"]:
                    mark_html_for_retry(
                        conn=conn,
                        document_id=document_id,
                        reason=(
                            "HTML response appears to be "
                            "an error or placeholder page"
                        ),
                    )

                    print(
                        f"{transaction_id} -> "
                        "download_retry_required"
                    )

                    continue

                save_extracted_text(
                    conn=conn,
                    document_id=document_id,
                    result=result,
                )

                print(
                    f"{transaction_id} -> "
                    "ready_for_parsing | "
                    f"characters="
                    f"{len(result['extracted_text'])}"
                )

            except Exception as error:
                conn.rollback()

                mark_html_extraction_failed(
                    conn=conn,
                    document_id=document_id,
                    error=error,
                )

                print(
                    f"{transaction_id} -> "
                    f"failed_html_extraction | {error}"
                )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
