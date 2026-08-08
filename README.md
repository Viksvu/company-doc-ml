# CompanyCentral

FastAPI app and extraction pipeline for Companies House document processing,
metadata extraction, OCR, company matching, and company-level document viewing.

## What Goes In Git

Commit source code, templates, pipeline scripts, docs, and dependency files.

Do not commit local runtime data:

- `data/raw/`
- `data/raw/uploads/`
- `.env`
- virtual environments
- database dumps

## Setup On A New Laptop

```bash
git clone <your-repo-url>
cd company-app

python3 -m venv ../venv
source ../venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Edit `.env` if your PostgreSQL URL differs:

```bash
DATABASE_URL=postgresql://postgres:2219@localhost:5432/company_app
```

Load environment variables in your shell:

```bash
set -a
source .env
set +a
```

Create the database if needed:

```bash
createdb company_app
```

Import Companies House bulk company data:

```bash
python pipelines/import_company_data.py /path/to/BasicCompanyData.zip
```

Run the app:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000/companies/
```

## Export Current Database

From the project root:

```bash
set -a
source .env
set +a

bash scripts/export_database.sh
```

This writes a dump under `exports/`.

Do not commit `exports/` to Git. Copy it separately if you need to move the
current database state.

## Restore Database On Another Laptop

```bash
set -a
source .env
set +a

createdb company_app
bash scripts/import_database.sh /path/to/company_app_YYYYMMDD_HHMMSS.dump
```

If the restored database contains old absolute file paths from another machine,
normalise them:

```bash
psql "$DATABASE_URL" -f scripts/normalise_file_paths.sql
```

The database alone does not include downloaded PDFs/HTML. Copy `data/raw/`
separately if you want existing `View` links to work without redownloading.

## Commit And Push

Stage code only:

```bash
git add app pipelines requirements.txt README.md .gitignore .env.example scripts
git status --short
git commit -m "Add portable company document matching app setup"
git push origin main
```

If Git already tracks raw documents, remove them from Git tracking while keeping
local files:

```bash
git rm -r --cached data/raw
git commit -m "Stop tracking downloaded raw documents"
git push origin main
```
