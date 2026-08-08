import csv
import hashlib
from datetime import datetime, UTC
from pathlib import Path

import psycopg2
import requests

from pipeline_config import get_database_url, project_relative_path


APP_DIR = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = APP_DIR / "data" / "raw"
INPUT_CSV_PATH = APP_DIR / "data" / "input" / "transaction_ids_sample.csv"

DATABASE_URL = get_database_url()


def calculate_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def detect_file_type(content: bytes) -> str:
    start = content[:300].lstrip()
    lower = start.lower()

    if start.startswith(b"%PDF-"):
        return "pdf"

    if lower.startswith(b"<!doctype html") or lower.startswith(b"<html") or b"<html" in lower:
        return "html"

    if start.startswith(b"\xff\xd8\xff"):
        return "jpg"

    if start.startswith(b"\x89PNG"):
        return "png"

    return "unknown"


def get_file_suffix_from_detected_type(detected_type: str) -> str:
    if detected_type == "pdf":
        return ".pdf"
    if detected_type == "html":
        return ".html"
    if detected_type == "jpg":
        return ".jpg"
    if detected_type == "png":
        return ".png"

    return ".bin"

def get_processing_status_from_detected_type(detected_type: str) -> str:
    if detected_type == "pdf":
        return "pdf_downloaded"

    if detected_type == "html":
        return "html_detected"

    if detected_type in {"jpg", "png"}:
        return "ready_for_ocr"

    return "unknown_file_type"

def read_transaction_ids(csv_path: Path) -> list[str]:
    transaction_ids = []

    with csv_path.open("r", newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            transaction_id = row["transaction_id"].strip()

            if transaction_id:
                transaction_ids.append(transaction_id)

    return transaction_ids


def create_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
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
        """)
        cur.execute("""
            ALTER TABLE raw_documents
            ADD COLUMN IF NOT EXISTS pdf_metadata JSONB NOT NULL DEFAULT '{}',
            ADD COLUMN IF NOT EXISTS pdf_metadata_company_number TEXT,
            ADD COLUMN IF NOT EXISTS pdf_metadata_company_name TEXT;
        """)

    conn.commit()


def already_downloaded(conn, transaction_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT download_status
            FROM raw_documents
            WHERE transaction_id = %s;
        """, (transaction_id,))

        row = cur.fetchone()

    return row is not None and row[0] == "downloaded"


def save_metadata(conn, metadata: dict):
    with conn.cursor() as cur:
        cur.execute("""
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
                downloaded_at,
                updated_at
            )
            VALUES (
                %(transaction_id)s,
                %(source)s,
                %(file_path)s,
                %(content_type)s,
                %(detected_file_type)s,
                %(file_size_bytes)s,
                %(sha256_hash)s,
                %(download_status)s,
                %(processing_status)s,
                %(error_message)s,
                %(downloaded_at)s,
                NOW()
            )
            ON CONFLICT (transaction_id)
            DO UPDATE SET
                file_path = EXCLUDED.file_path,
                content_type = EXCLUDED.content_type,
                detected_file_type = EXCLUDED.detected_file_type,
                file_size_bytes = EXCLUDED.file_size_bytes,
                sha256_hash = EXCLUDED.sha256_hash,
                download_status = EXCLUDED.download_status,
                processing_status = EXCLUDED.processing_status,
                error_message = EXCLUDED.error_message,
                downloaded_at = EXCLUDED.downloaded_at,
                updated_at = NOW();
        """, metadata)

    conn.commit()


def download_file(transaction_id: str, conn):
    if already_downloaded(conn, transaction_id):
        print(f"Already downloaded, skipping: {transaction_id}")
        return

    url = (
        "https://api.companycentral.co.uk/api/companies/tabs/finance/"
        f"historical-info/document/{transaction_id}"
    )

    try:
        response = requests.get(url, timeout=30)

        content_type = response.headers.get("content-type")
        file_size = len(response.content)

        if response.status_code != 200:
            save_metadata(
                conn,
                {
                    "transaction_id": transaction_id,
                    "source": "companycentral",
                    "file_path": None,
                    "content_type": content_type,
                    "detected_file_type": None,
                    "file_size_bytes": file_size,
                    "sha256_hash": None,
                    "download_status": "failed",
                    "processing_status": "not_started",
                    "error_message": response.text[:1000],
                    "downloaded_at": None,
                },
            )

            print(f"Failed: {transaction_id} | status={response.status_code}")
            return

        detected_type = detect_file_type(response.content)
        extension = get_file_suffix_from_detected_type(detected_type)
        processing_status = get_processing_status_from_detected_type(detected_type)

        file_hash = calculate_sha256(response.content)

        folder_path = RAW_DATA_DIR / transaction_id[:2]
        folder_path.mkdir(parents=True, exist_ok=True)

        file_path = folder_path / f"{transaction_id}{extension}"

        with file_path.open("wb") as file:
            file.write(response.content)

        save_metadata(
            conn,
            {
                "transaction_id": transaction_id,
                "source": "companycentral",
                "file_path": project_relative_path(file_path),
                "content_type": content_type,
                "detected_file_type": detected_type,
                "file_size_bytes": file_size,
                "sha256_hash": file_hash,
                "download_status": "downloaded",
                "processing_status": processing_status,
                "error_message": None,
                "downloaded_at": datetime.now(UTC),
            },
        )

        print(f"Downloaded: {transaction_id}")
        print(f"Detected type: {detected_type}")
        print(f"Status: {processing_status}")
        print(f"Saved to: {file_path}")
        print(f"Transaction: {transaction_id}")
        print(f"Requested URL: {url}")
        print(f"Final URL: {response.url}")
        print(f"Status code: {response.status_code}")
        print(f"Content-Type: {response.headers.get('content-type')}")
        print(f"Content-Length: {len(response.content)}")
        print(f"Redirects: {[r.status_code for r in response.history]}")
        print(f"First bytes: {response.content[:100]!r}")

    except requests.RequestException as error:
        save_metadata(
            conn,
            {
                "transaction_id": transaction_id,
                "source": "companycentral",
                "file_path": None,
                "content_type": None,
                "detected_file_type": None,
                "file_size_bytes": None,
                "sha256_hash": None,
                "download_status": "failed",
                "processing_status": "not_started",
                "error_message": str(error),
                "downloaded_at": None,
            },
        )

        print(f"Request failed: {transaction_id} | {error}")


def main():
    conn = psycopg2.connect(DATABASE_URL)

    try:
        create_table(conn)

        transaction_ids = read_transaction_ids(INPUT_CSV_PATH)
        print(f"Found {len(transaction_ids)} transaction IDs")

        for transaction_id in transaction_ids:
            download_file(transaction_id, conn)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
