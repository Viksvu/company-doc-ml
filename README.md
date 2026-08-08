Historical Companies House filing processor that ingests company documents,
extracts filing data, matches documents to company records, and supports company
search and document review through a lightweight FastAPI app.

<img width="2717" height="1499" alt="image" src="https://github.com/user-attachments/assets/c5fb0239-922c-481c-8fdf-f5bcfd702f5a" />


## Repository Contents

- `app/` - FastAPI app, upload workflow, company search, company pages, matched document viewing.
- `pipelines/` - import, download, OCR/text extraction, metadata parsing, and company matching scripts.
- `scripts/` - database export/import helpers.
- `data/input/` - input CSVs such as transaction IDs.
- `data/raw/` - local downloaded or imported documents. This is ignored by Git.

## Environment Setup

Create a virtual environment from the project root:

```bash
cd company-doc-parser
python3 -m venv ../venv
source ../venv/bin/activate
pip install -r requirements.txt
```

Create your local environment file:

```bash
cp .env.example .env
```

Edit `.env` if your PostgreSQL credentials differ:

```bash
DATABASE_URL=postgresql://postgres:1234@localhost:5432/company_app
BASIC_OCR_DPI=180
BASIC_OCR_MAX_IMAGE_SIDE=1800
BASIC_OCR_MAX_SCANNED_PAGES=0
REOCR_DPI=320
PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
```

Load it into your shell before running scripts:

```bash
set -a
source .env
set +a
```

The app and pipeline scripts also attempt to read `.env` automatically, but
loading it in your shell makes `psql`, `pg_dump`, and helper scripts use the
same settings.

## Database Setup

Create the PostgreSQL database:

```bash
createdb company_app
```

If the database already exists:

```bash
psql "$DATABASE_URL" -c "SELECT 1;"
```

## Companies House Company Metadata

This project uses the Companies House Free Company Data Product:

```text
https://download.companieshouse.gov.uk/en_output.html
```

Download the latest `BasicCompanyData` ZIP from that page, then import it:

```bash
python pipelines/import_company_data.py /path/to/BasicCompanyData.zip
```

The importer:

- opens the ZIP with `zipfile`
- processes all CSV files inside it
- streams rows with `csv.DictReader`
- stores company numbers as text
- stores registered address as JSONB
- stores SIC codes as a PostgreSQL text array
- upserts into `companies` with `ON CONFLICT (company_number) DO UPDATE`

Verify import:

```bash
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM companies;"
```

## Running The App

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000/companies/
```

The app lets you:

- search companies by number or name
- open a company page
- view company metadata
- query/filter matched documents for that company
- open locally stored matched PDFs/HTML files
- upload documents and run them through the extraction pipeline

## Document Pipeline Overview

The normal pipeline order is:

```bash
python pipelines/download_files.py
python pipelines/inspect_pdfs.py
python pipelines/ocr_documents.py
python pipelines/extract_pdf_text.py
python pipelines/extract_html_text.py
python pipelines/extract_metadata.py
python pipelines/match_document_companies.py
```

Or run the combined extraction steps:

```bash
python pipelines/full_extraction.py
```

`full_extraction.py` runs download, PDF inspection, OCR, native PDF text
extraction, HTML text extraction, and metadata extraction. Run matching after it:

```bash
python pipelines/match_document_companies.py
```

## Downloading Documents With This Project

`download_files.py` reads transaction IDs from:

```text
data/input/transaction_ids_sample.csv
```

Expected CSV column:

```text
transaction_id
```

Run:

```bash
python pipelines/download_files.py
```

Downloaded files are stored under:

```text
data/raw/
```

and registered in `raw_documents`.

## Running Without `download_files.py`

If another user obtains documents another way, they can still use the parser and
matching pipeline. They must first insert rows into `raw_documents` with valid
local `file_path`, `detected_file_type`, `download_status`, and
`processing_status` values.

Use these initial statuses:

- PDF - `processing_status = 'pdf_downloaded'`
- HTML - `processing_status = 'html_detected'`

Then run the rest of the pipeline without the download step:

```bash
python pipelines/inspect_pdfs.py
python pipelines/ocr_documents.py
python pipelines/extract_pdf_text.py
python pipelines/extract_html_text.py
python pipelines/extract_metadata.py
python pipelines/match_document_companies.py
```

## Parser And Metadata Extraction

The parser lives in:

```text
pipelines/extract_metadata.py
```

Run it:

```bash
python pipelines/extract_metadata.py
```

It reads documents from `document_text` and writes to `document_metadata`.

It only processes rows where:

- extracted text exists, and
- no metadata row exists, or
- `document_metadata.parser_version` differs from `PARSER_VERSION`

To force existing rows through the parser again after changing extraction rules,
increment:

```python
PARSER_VERSION = "v12"
```

Then rerun:

```bash
python pipelines/extract_metadata.py
```

The parser extracts fields such as:

- `company_number`
- `company_name`
- `form_type`
- `document_type`
- `filing_date`
- `extra_metadata`
- `confidence_score`

Company numbers are stored as text so leading zeros are preserved.

## Matching Documents To Companies

Run:

```bash
python pipelines/match_document_companies.py
```

Arguments:

```bash
python pipelines/match_document_companies.py --exact-only
python pipelines/match_document_companies.py --dry-run
python pipelines/match_document_companies.py --candidate-threshold 0.75
python pipelines/match_document_companies.py --auto-accept-threshold 0.90
python pipelines/match_document_companies.py --ambiguity-margin 0.03
```

Options:

- `--exact-only` - only match by exact company number.
- `--dry-run` - run matching and roll back changes at the end.
- `--candidate-threshold` - minimum trigram similarity for fuzzy candidates.
- `--auto-accept-threshold` - fuzzy matches at or above this score can be accepted automatically.
- `--ambiguity-margin` - send fuzzy matches to review when the best and second-best company-name scores are this close or closer.

Exact company-number matches are accepted first. Fuzzy company-name matches are
auto-accepted only when the best score is above the auto-accept threshold and is
not too close to the second-best candidate.

Matching writes to:

```text
document_company_matches
```

## OCR

Basic OCR:

```bash
python pipelines/ocr_documents.py
```

This processes documents with status `ready_for_ocr` or `failed_ocr`.

Useful environment variables:

```bash
BASIC_OCR_DPI=180
BASIC_OCR_MAX_IMAGE_SIDE=1800
BASIC_OCR_MAX_SCANNED_PAGES=0
```

Higher-quality re-OCR for low-confidence documents:

```bash
python pipelines/reocr_low_confidence.py --dry-run --limit 10
python pipelines/reocr_low_confidence.py --threshold 0.6 --limit 5
python pipelines/reocr_low_confidence.py --threshold 0.9 --dpi 350 --max-pages 2
```

Arguments:

- `--threshold` - re-OCR documents below this confidence score.
- `--limit` - maximum number of documents to process.
- `--dry-run` - list matching documents without writing updated text.
- `--keep-metadata` - replace text but keep existing metadata rows.
- `--crop-profile` - use a legacy crop profile, or `auto`.
- `--dpi` - render DPI for re-OCR.
- `--max-pages` - only re-OCR the first N pages.
- `--max-image-side` - cap image size sent into PaddleOCR.

If you do not pass `--keep-metadata`, existing metadata is deleted for re-OCRed
documents so `extract_metadata.py` can parse the new text.

## Tests

Run the unit test suite from the repository root:

```bash
python -m pytest
```

The tests use synthetic fixtures and do not require OCR binaries, network
access, external APIs, or a running PostgreSQL instance.
