import argparse
import csv
import zipfile
from datetime import datetime
from io import TextIOWrapper
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, execute_values

from pipeline_config import get_database_url

DATABASE_URL = get_database_url()

BATCH_SIZE = 5000
PROGRESS_INTERVAL = 10000

ADDRESS_COLUMNS = {
    "care_of": "RegAddress.CareOf",
    "po_box": "RegAddress.POBox",
    "address_line_1": "RegAddress.AddressLine1",
    "address_line_2": "RegAddress.AddressLine2",
    "post_town": "RegAddress.PostTown",
    "county": "RegAddress.County",
    "country": "RegAddress.Country",
    "postcode": "RegAddress.PostCode",
}

SIC_COLUMNS = [
    "SICCode.SicText_1",
    "SICCode.SicText_2",
    "SICCode.SicText_3",
    "SICCode.SicText_4",
]


def blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.strip()

    return value or None


def parse_companies_house_date(value: str | None):
    value = blank_to_none(value)

    if not value:
        return None

    supported_formats = [
        "%d/%m/%Y",
        "%Y-%m-%d",
    ]

    for date_format in supported_formats:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue

    return None


def clean_company_number(value: str | None) -> str | None:
    value = blank_to_none(value)

    if not value:
        return None

    return "".join(value.split()).upper()


def build_registered_address(row: dict) -> dict | None:
    address = {
        key: blank_to_none(row.get(column_name))
        for key, column_name in ADDRESS_COLUMNS.items()
    }
    address = {
        key: value
        for key, value in address.items()
        if value is not None
    }

    return address or None


def build_sic_codes(row: dict) -> list[str] | None:
    sic_codes = [
        blank_to_none(row.get(column_name))
        for column_name in SIC_COLUMNS
    ]
    sic_codes = [
        sic_code
        for sic_code in sic_codes
        if sic_code is not None
    ]

    return sic_codes or None


def build_official_data(row: dict, csv_name: str) -> dict:
    return {
        "source": "companies_house_basic_company_data",
        "source_file": csv_name,
        "country_of_origin": blank_to_none(row.get("CountryOfOrigin")),
        "company_category": blank_to_none(row.get("CompanyCategory")),
    }


def row_to_company_record(row: dict, csv_name: str) -> tuple | None:
    company_number = clean_company_number(row.get("CompanyNumber"))
    company_name = blank_to_none(row.get("CompanyName"))

    if not company_number or not company_name:
        return None

    registered_address = build_registered_address(row)

    return (
        company_number,
        company_name,
        blank_to_none(row.get("CompanyStatus")),
        blank_to_none(row.get("CompanyCategory")),
        parse_companies_house_date(row.get("IncorporationDate")),
        parse_companies_house_date(row.get("DissolutionDate")),
        Json(registered_address) if registered_address else None,
        build_sic_codes(row),
        Json(build_official_data(row, csv_name)),
    )


def create_companies_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                id BIGSERIAL PRIMARY KEY,
                company_number TEXT UNIQUE NOT NULL,
                official_company_name TEXT NOT NULL,
                company_status TEXT,
                company_type TEXT,
                incorporation_date DATE,
                dissolution_date DATE,
                registered_address JSONB,
                sic_codes TEXT[],
                official_data JSONB,
                fetched_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

    conn.commit()


def upsert_company_batch(
    conn,
    batch: list[tuple],
    csv_name: str,
    batch_start_row: int,
) -> None:
    if not batch:
        return

    query = """
        INSERT INTO companies (
            company_number,
            official_company_name,
            company_status,
            company_type,
            incorporation_date,
            dissolution_date,
            registered_address,
            sic_codes,
            official_data,
            fetched_at,
            updated_at
        )
        VALUES %s
        ON CONFLICT (company_number)
        DO UPDATE SET
            official_company_name = EXCLUDED.official_company_name,
            company_status = EXCLUDED.company_status,
            company_type = EXCLUDED.company_type,
            incorporation_date = EXCLUDED.incorporation_date,
            dissolution_date = EXCLUDED.dissolution_date,
            registered_address = EXCLUDED.registered_address,
            sic_codes = EXCLUDED.sic_codes,
            official_data = EXCLUDED.official_data,
            fetched_at = EXCLUDED.fetched_at,
            updated_at = NOW();
    """

    template = """
        (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW()
        )
    """

    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                query,
                batch,
                template=template,
                page_size=len(batch),
            )

        conn.commit()

    except Exception as error:
        conn.rollback()
        batch_end_row = batch_start_row + len(batch) - 1
        raise RuntimeError(
            f"Failed importing {csv_name} rows "
            f"{batch_start_row}-{batch_end_row}: {error}"
        ) from error


def iter_csv_names(zip_file: zipfile.ZipFile) -> list[str]:
    return [
        name
        for name in zip_file.namelist()
        if name.lower().endswith(".csv") and not name.endswith("/")
    ]


def import_csv_member(
    conn,
    zip_file: zipfile.ZipFile,
    csv_name: str,
) -> tuple[int, int]:
    imported_rows = 0
    skipped_rows = 0
    batch = []
    batch_start_row = 1

    print(f"Importing CSV: {csv_name}")

    with zip_file.open(csv_name) as binary_file:
        text_file = TextIOWrapper(
            binary_file,
            encoding="utf-8-sig",
            newline="",
        )
        reader = csv.DictReader(text_file)

        if reader.fieldnames:
            reader.fieldnames = [
                field_name.strip()
                for field_name in reader.fieldnames
            ]

        for source_row_number, row in enumerate(reader, start=1):
            record = row_to_company_record(row, csv_name)

            if record is None:
                skipped_rows += 1
                continue

            if not batch:
                batch_start_row = source_row_number

            batch.append(record)

            if len(batch) >= BATCH_SIZE:
                upsert_company_batch(
                    conn,
                    batch,
                    csv_name,
                    batch_start_row,
                )
                imported_rows += len(batch)
                batch = []

                if imported_rows % PROGRESS_INTERVAL == 0:
                    print(f"Imported {imported_rows} rows from {csv_name}")

        if batch:
            upsert_company_batch(
                conn,
                batch,
                csv_name,
                batch_start_row,
            )
            imported_rows += len(batch)

    print(
        f"Finished {csv_name}: "
        f"imported={imported_rows}, skipped={skipped_rows}"
    )

    return imported_rows, skipped_rows


def import_company_data(zip_path: Path) -> None:
    if not zip_path.exists():
        raise FileNotFoundError(f"ZIP file does not exist: {zip_path}")

    if not zip_path.is_file():
        raise ValueError(f"ZIP path is not a file: {zip_path}")

    conn = psycopg2.connect(DATABASE_URL)

    try:
        create_companies_table(conn)

        total_imported = 0
        total_skipped = 0

        with zipfile.ZipFile(zip_path) as zip_file:
            csv_names = iter_csv_names(zip_file)

            if not csv_names:
                raise ValueError(f"No CSV files found inside {zip_path}")

            print(f"Found {len(csv_names)} CSV file(s) in {zip_path}")

            for csv_name in csv_names:
                imported_rows, skipped_rows = import_csv_member(
                    conn,
                    zip_file,
                    csv_name,
                )
                total_imported += imported_rows
                total_skipped += skipped_rows

        print("")
        print("Company data import complete")
        print(f"Imported rows: {total_imported}")
        print(f"Skipped rows: {total_skipped}")

    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import Companies House Basic Company Data ZIP files into "
            "the companies table."
        ),
    )
    parser.add_argument(
        "zip_path",
        help="Path to a Companies House BasicCompanyData ZIP file.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    import_company_data(Path(args.zip_path))


if __name__ == "__main__":
    main()
