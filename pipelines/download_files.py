import requests
import time
import csv
import hashlib
from datetime import datetime
from pathlib import Path
import psycopg2
APP_DIR= Path(__file__).resolve().parent.parent
RAW_DATA_DIR= APP_DIR/"data"/"raw" 
DATABASE_URL = "postgresql://postgres:2219@localhost:5432/company_app"
def calculate_sha256(content: bytes)-> str:
    return hashlib.sha256(content).hexdigest()

def get_file_suffix(content_type:str|None)->str:
    if content_type and "pdf" in content_type:
        return ".pdf"
    if content_type and "jpeg" in content_type:
        return ".jpg"
    if content_type and "png" in content_type:
        return ".png"
    return ".bin"
def create_table(conn,):
    with conn.cursor() as cur:
        cur.execute("""
                CREATE TABLE IF NOT EXISTS raw_documents (
                id SERIAL PRIMARY KEY,
                transaction_id TEXT UNIQUE NOT NULL,
                source TEXT NOT NULL,
                file_path TEXT,
                content_type TEXT,
                file_size_bytes BIGINT,
                sha256_hash TEXT,
                download_status TEXT NOT NULL,
                processing_status TEXT NOT NULL,
                error_message TEXT,
                downloaded_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
                    )
                    """)

def save_metadata(conn, metadata: dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO raw_documents (
                transaction_id,
                source,
                file_path,
                content_type,
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
                file_size_bytes = EXCLUDED.file_size_bytes,
                sha256_hash = EXCLUDED.sha256_hash,
                download_status = EXCLUDED.download_status,
                processing_status = EXCLUDED.processing_status,
                error_message = EXCLUDED.error_message,
                downloaded_at = EXCLUDED.downloaded_at,
                updated_at = NOW();
        """, metadata)
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

def download_file(
        transaction_id:str,
        conn,):
    if(already_downloaded(conn, transaction_id)):
        print(f"Already downloaded {transaction_id}")
        return
    url = f"https://api.companycentral.co.uk/api/companies/tabs/finance/historical-info/document/{transaction_id}"
    try:
      response = requests.get(url, timeout=30)
      content_type= response.headers.get("content-type")
      file_size=len(response.content)

      if response.status_code!=200:
          save_metadata(conn, {
                "transaction_id": transaction_id,
                "source": "companycentral",
                "file_path": None,
                "content_type": content_type,
                "file_size_bytes": file_size,
                "sha256_hash": None,
                "download_status": "failed",
                "processing_status": "not_started",
                "error_message": response.text[:1000],
                "downloaded_at": None,
            })
          print(f"Failed, {transaction_id}")
          return
      file_hash=calculate_sha256(response.content)
      extension=get_file_suffix(content_type)
      folder_path=RAW_DATA_DIR/transaction_id[:2]
      
      folder_path.mkdir(parents=True, exist_ok=True)
      file_path=folder_path/f"{transaction_id}{extension}"
      with file_path.open("wb") as file:
        file.write(response.content)
      save_metadata(conn, {
            "transaction_id": transaction_id,
            "source": "companycentral",
            "file_path": str(file_path),
            "content_type": content_type,
            "file_size_bytes": file_size,
            "sha256_hash": file_hash,
            "download_status": "downloaded",
            "processing_status": "ocr_pending",
            "error_message": None,
            "downloaded_at": datetime.utcnow(),
        })

      print(f"Downloaded: {transaction_id}")
      print(f"Saved to: {file_path}")
    except requests.RequestException as error:
        save_metadata(conn, {
            "transaction_id": transaction_id,
            "source": "companycentral",
            "file_path": None,
            "content_type": None,
            "file_size_bytes": None,
            "sha256_hash": None,
            "download_status": "failed",
            "processing_status": "not_started",
            "error_message": str(error),
            "downloaded_at": None,
        })
        print(f"Request failed: {transaction_id}")


def main():
    input_path = APP_DIR / "data" / "input" / "transaction_ids_sample.csv"
    conn=psycopg2.connect(DATABASE_URL)
    transaction_ids=read_transaction_ids(input_path)
    
    print(f"{len(transaction_ids)} transaction IDs")   
    try:
        create_table(conn)
        for transaction_id in transaction_ids:
          download_file(transaction_id, conn)
          time.sleep(2)
    finally:
        conn.close()



 
def read_transaction_ids(csv_path: Path)->list[str]:
    transaction_ids=[]

    with csv_path.open ("r", newline="") as file:
        reader = csv.DictReader(file)
        
        for row in reader:
            transaction_id= row["transaction_id"].strip()

            if transaction_id:
                transaction_ids.append(transaction_id)
    return transaction_ids 

if __name__ == "__main__":
    main()