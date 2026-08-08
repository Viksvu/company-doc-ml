from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db


APP_DIR = Path(__file__).resolve().parents[2]

router = APIRouter(
    prefix="/companies",
    tags=["companies"],
)

templates = Jinja2Templates(directory="app/templates")


def isoformat_date(value):
    return value.isoformat() if value else None


def clean_query_value(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.strip()

    return value or None


def normalise_company_number(value: str | None) -> str | None:
    if not value:
        return None

    value = "".join(value.split()).upper()

    return value or None


def resolve_project_path(value: str | None) -> Path | None:
    if not value:
        return None

    path = Path(value)

    if path.is_absolute():
        return path

    return APP_DIR / path


def ensure_company_search_objects(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE INDEX IF NOT EXISTS companies_company_number_prefix_idx
            ON companies (company_number text_pattern_ops);
        """))
        db.execute(text("""
            CREATE INDEX IF NOT EXISTS companies_official_name_lower_prefix_idx
            ON companies (lower(official_company_name) text_pattern_ops);
        """))
        db.commit()
    except IntegrityError:
        db.rollback()


@router.get("/", response_class=HTMLResponse)
def companies_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="companies.html",
        context={},
    )


def serialize_company(row) -> dict:
    company = dict(row)
    company["incorporation_date"] = isoformat_date(
        company["incorporation_date"]
    )
    company["dissolution_date"] = isoformat_date(
        company["dissolution_date"]
    )

    return company


def serialize_document(row) -> dict:
    document = dict(row)
    document["filing_date"] = isoformat_date(document["filing_date"])
    document["created_at"] = (
        document["created_at"].isoformat()
        if document["created_at"]
        else None
    )
    document["match_score"] = (
        float(document["match_score"])
        if document["match_score"] is not None
        else None
    )
    document["confidence_score"] = (
        float(document["confidence_score"])
        if document["confidence_score"] is not None
        else None
    )

    return document


@router.get("/search")
def search_companies(
    q: str = Query(..., min_length=2),
    limit: int = 20,
    db: Session = Depends(get_db),
):
    query = q.strip()
    limit = max(1, min(limit, 50))
    normalised_number = normalise_company_number(query)

    ensure_company_search_objects(db)
    db.execute(text("SET LOCAL statement_timeout = '5s';"))

    rows = db.execute(
        text("""
            WITH number_matches AS (
                SELECT
                    company_number,
                    official_company_name,
                    company_status,
                    company_type,
                    incorporation_date,
                    dissolution_date,
                    1.0::float AS score,
                    CASE
                        WHEN company_number = :exact_number
                        THEN 'exact_company_number'
                        ELSE 'prefix_company_number'
                    END AS match_method,
                    CASE
                        WHEN company_number = :exact_number
                        THEN 0
                        ELSE 1
                    END AS rank_group
                FROM companies
                WHERE :exact_number IS NOT NULL
                  AND (
                        company_number = :exact_number
                        OR company_number LIKE :prefix_number
                  )
                LIMIT :limit
            ),
            name_matches AS (
                SELECT
                    company_number,
                    official_company_name,
                    company_status,
                    company_type,
                    incorporation_date,
                    dissolution_date,
                    0.8::float AS score,
                    'prefix_company_name' AS match_method,
                    2 AS rank_group
                FROM companies
                WHERE lower(official_company_name) LIKE lower(:name_prefix)
                ORDER BY
                    CASE WHEN company_status = 'Active' THEN 0 ELSE 1 END,
                    official_company_name
                LIMIT :limit
            )
            SELECT
                company_number,
                official_company_name,
                company_status,
                company_type,
                incorporation_date,
                dissolution_date,
                score,
                match_method
            FROM (
                SELECT DISTINCT ON (company_number)
                    company_number,
                    official_company_name,
                    company_status,
                    company_type,
                    incorporation_date,
                    dissolution_date,
                    score,
                    match_method,
                    rank_group
                FROM (
                    SELECT * FROM number_matches
                    UNION ALL
                    SELECT * FROM name_matches
                ) matches
                ORDER BY
                    company_number,
                    rank_group,
                    score DESC
            ) deduped_matches
            ORDER BY
                rank_group,
                score DESC,
                CASE WHEN company_status = 'Active' THEN 0 ELSE 1 END,
                official_company_name
            LIMIT :limit;
        """),
        {
            "query": query,
            "exact_number": normalised_number,
            "prefix_number": f"{normalised_number}%" if normalised_number else None,
            "name_prefix": f"{query}%",
            "limit": limit,
        },
    ).mappings().all()

    results = [serialize_company(row) for row in rows]

    return {
        "query": query,
        "count": len(results),
        "results": results,
    }


@router.get("/{company_number}", response_class=HTMLResponse)
def company_detail_page(
    company_number: str,
    request: Request,
    db: Session = Depends(get_db),
):
    company = db.execute(
        text("""
            SELECT
                id,
                company_number,
                official_company_name,
                company_status,
                company_type,
                incorporation_date,
                dissolution_date,
                registered_address,
                sic_codes,
                fetched_at,
                updated_at
            FROM companies
            WHERE company_number = :company_number
            LIMIT 1;
        """),
        {"company_number": normalise_company_number(company_number)},
    ).mappings().first()

    if not company:
        raise HTTPException(
            status_code=404,
            detail="Company was not found",
        )

    company = serialize_company(company)
    company["fetched_at"] = (
        company["fetched_at"].isoformat()
        if company["fetched_at"]
        else None
    )
    company["updated_at"] = (
        company["updated_at"].isoformat()
        if company["updated_at"]
        else None
    )

    return templates.TemplateResponse(
        request=request,
        name="company_detail.html",
        context={
            "company": company,
        },
    )


@router.get("/{company_number}/documents")
def company_documents(
    company_number: str,
    q: str | None = None,
    document_type: str | None = None,
    form_type: str | None = None,
    review_status: str = "all",
    limit: int = 50,
    db: Session = Depends(get_db),
):
    company_number = normalise_company_number(company_number)
    query = clean_query_value(q)
    document_type = clean_query_value(document_type)
    form_type = clean_query_value(form_type)
    review_status = review_status.strip().lower()
    limit = max(1, min(limit, 100))

    review_conditions = {
        "all": "TRUE",
        "accepted": "dcm.is_accepted IS TRUE AND dcm.review_required IS FALSE",
        "review": "dcm.review_required IS TRUE",
        "conflict": "dcm.match_method = 'selected_company_conflict'",
        "errors": (
            "dm.extraction_error IS NOT NULL "
            "OR rd.processing_status IN ("
            "'failed_ocr', "
            "'failed_text_extraction', "
            "'failed_html_extraction', "
            "'failed_pdf_validation', "
            "'metadata_failed', "
            "'unknown_file_type'"
            ")"
        ),
    }

    if review_status not in review_conditions:
        raise HTTPException(
            status_code=400,
            detail="Invalid review_status filter",
        )

    rows = db.execute(
        text(f"""
            SELECT
                rd.id AS raw_document_id,
                rd.transaction_id,
                rd.source,
                rd.file_path,
                rd.detected_file_type,
                rd.processing_status,
                rd.page_count,
                rd.error_message,
                rd.inspection_error,
                rd.pdf_metadata_company_number,
                rd.pdf_metadata_company_name,
                rd.created_at,
                uf.filename,
                uf.upload_batch_id,
                uf.selected_company_number,
                uf.selected_company_name,
                dm.company_number AS extracted_company_number,
                dm.company_name AS extracted_company_name,
                dm.form_type,
                dm.document_type,
                dm.filing_date,
                dm.confidence_score,
                dm.parser_version,
                dm.extraction_error,
                dcm.match_method,
                dcm.match_score,
                dcm.is_accepted,
                dcm.review_required,
                dcm.source_company_number,
                dcm.source_company_name,
                LEFT(dt.extracted_text, 700) AS text_preview
            FROM companies c
            JOIN document_company_matches dcm
                ON dcm.company_id = c.id
            JOIN raw_documents rd
                ON rd.id = dcm.raw_document_id
            LEFT JOIN document_metadata dm
                ON dm.raw_document_id = rd.id
            LEFT JOIN document_text dt
                ON dt.raw_document_id = rd.id
            LEFT JOIN uploaded_files uf
                ON uf.raw_document_id = rd.id
            WHERE c.company_number = :company_number
              AND (:document_type IS NULL OR dm.document_type = :document_type)
              AND (:form_type IS NULL OR dm.form_type = :form_type)
              AND ({review_conditions[review_status]})
              AND (
                    :query IS NULL
                    OR rd.transaction_id ILIKE :query_like
                    OR COALESCE(uf.filename, '') ILIKE :query_like
                    OR COALESCE(dm.company_number, '') ILIKE :query_like
                    OR COALESCE(dm.company_name, '') ILIKE :query_like
                    OR COALESCE(dm.form_type, '') ILIKE :query_like
                    OR COALESCE(dm.document_type, '') ILIKE :query_like
                    OR COALESCE(dt.extracted_text, '') ILIKE :query_like
              )
            ORDER BY
                COALESCE(dm.filing_date, rd.created_at::date) DESC NULLS LAST,
                rd.id DESC
            LIMIT :limit;
        """),
        {
            "company_number": company_number,
            "document_type": document_type,
            "form_type": form_type.upper() if form_type else None,
            "query": query,
            "query_like": f"%{query}%" if query else None,
            "limit": limit,
        },
    ).mappings().all()

    documents = [serialize_document(row) for row in rows]

    return {
        "company_number": company_number,
        "count": len(documents),
        "documents": documents,
    }


@router.get("/{company_number}/documents/{raw_document_id}/file")
def view_company_document_file(
    company_number: str,
    raw_document_id: int,
    db: Session = Depends(get_db),
):
    row = db.execute(
        text("""
            SELECT
                rd.file_path,
                rd.detected_file_type,
                COALESCE(uf.filename, rd.transaction_id) AS display_filename
            FROM companies c
            JOIN document_company_matches dcm
                ON dcm.company_id = c.id
            JOIN raw_documents rd
                ON rd.id = dcm.raw_document_id
            LEFT JOIN uploaded_files uf
                ON uf.raw_document_id = rd.id
            WHERE c.company_number = :company_number
              AND rd.id = :raw_document_id
            LIMIT 1;
        """),
        {
            "company_number": normalise_company_number(company_number),
            "raw_document_id": raw_document_id,
        },
    ).mappings().first()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Document was not found for this company",
        )

    file_path = resolve_project_path(row["file_path"])

    if not file_path or not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Document file is missing on disk",
        )

    media_types = {
        "pdf": "application/pdf",
        "html": "text/html",
    }

    return FileResponse(
        path=file_path,
        media_type=media_types.get(row["detected_file_type"]),
        filename=row["display_filename"],
    )
