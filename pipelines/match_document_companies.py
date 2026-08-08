import argparse

import psycopg2

from pipeline_config import get_database_url

DATABASE_URL = get_database_url()

DEFAULT_CANDIDATE_THRESHOLD = 0.75
DEFAULT_AUTO_ACCEPT_THRESHOLD = 0.90


def create_matching_objects(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS uploaded_files (
                id SERIAL PRIMARY KEY,
                filename VARCHAR NOT NULL,
                content_type VARCHAR,
                file_path VARCHAR NOT NULL,
                upload_batch_id VARCHAR,
                raw_document_id INTEGER,
                upload_mode VARCHAR DEFAULT 'blind',
                selected_company_number VARCHAR,
                selected_company_name VARCHAR,
                parse_requested BOOLEAN DEFAULT FALSE,
                uploaded_at TIMESTAMP DEFAULT NOW()
            );
        """)

        cur.execute("""
            ALTER TABLE uploaded_files
            ADD COLUMN IF NOT EXISTS upload_batch_id VARCHAR,
            ADD COLUMN IF NOT EXISTS raw_document_id INTEGER,
            ADD COLUMN IF NOT EXISTS upload_mode VARCHAR DEFAULT 'blind',
            ADD COLUMN IF NOT EXISTS selected_company_number VARCHAR,
            ADD COLUMN IF NOT EXISTS selected_company_name VARCHAR,
            ADD COLUMN IF NOT EXISTS parse_requested BOOLEAN DEFAULT FALSE;
        """)

        cur.execute("""
            CREATE OR REPLACE FUNCTION normalise_company_number_for_match(
                value TEXT
            )
            RETURNS TEXT
            LANGUAGE sql
            IMMUTABLE
            AS $$
                SELECT NULLIF(
                    upper(regexp_replace(coalesce(value, ''), '[^A-Za-z0-9]', '', 'g')),
                    ''
                );
            $$;
        """)

        cur.execute("""
            CREATE OR REPLACE FUNCTION normalise_company_name_for_match(
                value TEXT
            )
            RETURNS TEXT
            LANGUAGE sql
            IMMUTABLE
            AS $$
                SELECT NULLIF(
                    trim(
                        regexp_replace(
                            regexp_replace(
                                upper(coalesce(value, '')),
                                '\\m(PUBLIC LIMITED COMPANY|PRIVATE LIMITED COMPANY|LIMITED|LTD|PLC|LLP|THE)\\M\\.?', 
                                '',
                                'g'
                            ),
                            '[^A-Z0-9]+',
                            ' ',
                            'g'
                        )
                    ),
                    ''
                );
            $$;
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS document_company_matches (
                raw_document_id BIGINT PRIMARY KEY
                    REFERENCES raw_documents(id) ON DELETE CASCADE,

                company_id BIGINT NOT NULL
                    REFERENCES companies(id) ON DELETE CASCADE,

                match_method TEXT NOT NULL,
                match_score NUMERIC(5, 4) NOT NULL,
                is_accepted BOOLEAN NOT NULL DEFAULT FALSE,
                review_required BOOLEAN NOT NULL DEFAULT TRUE,

                source_company_number TEXT,
                source_company_name TEXT,
                matched_company_number TEXT NOT NULL,
                matched_company_name TEXT NOT NULL,
                match_details JSONB NOT NULL DEFAULT '{}',

                manual_override BOOLEAN NOT NULL DEFAULT FALSE,

                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS companies_company_number_match_idx
            ON companies (normalise_company_number_for_match(company_number));
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS companies_official_name_trgm_idx
            ON companies
            USING gin (
                normalise_company_name_for_match(official_company_name)
                gin_trgm_ops
            );
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS document_metadata_company_name_trgm_idx
            ON document_metadata
            USING gin (
                normalise_company_name_for_match(company_name)
                gin_trgm_ops
            );
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS document_company_matches_company_idx
            ON document_company_matches (company_id);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS document_company_matches_review_idx
            ON document_company_matches (review_required, match_score);
        """)


def require_companies_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT to_regclass('public.companies');
        """)
        table_name = cur.fetchone()[0]

    if not table_name:
        raise RuntimeError(
            "Missing companies table. Run import_company_data.py before "
            "matching documents to companies."
        )


def match_exact_company_numbers(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            WITH selected_conflicts AS (
                SELECT
                    dm.raw_document_id,
                    dm.company_number AS extracted_company_number,
                    rd.pdf_metadata_company_number AS pdf_metadata_company_number,
                    uf.selected_company_number,
                    dm.company_name AS extracted_company_name,
                    rd.pdf_metadata_company_name AS pdf_metadata_company_name,
                    uf.selected_company_name,
                    selected_company.id AS selected_company_id,
                    selected_company.company_number AS matched_company_number,
                    selected_company.official_company_name AS matched_company_name
                FROM document_metadata dm
                JOIN raw_documents rd
                    ON rd.id = dm.raw_document_id
                JOIN uploaded_files uf
                    ON uf.raw_document_id = dm.raw_document_id
                JOIN companies selected_company
                    ON normalise_company_number_for_match(selected_company.company_number)
                       = normalise_company_number_for_match(
                            uf.selected_company_number
                       )
                WHERE rd.source = 'user_upload'
                  AND uf.selected_company_number IS NOT NULL
                  AND (
                        (
                            dm.company_number IS NOT NULL
                            AND normalise_company_number_for_match(
                                    dm.company_number
                                )
                                <> normalise_company_number_for_match(
                                    uf.selected_company_number
                                )
                        )
                        OR (
                            rd.pdf_metadata_company_number IS NOT NULL
                            AND normalise_company_number_for_match(
                                    rd.pdf_metadata_company_number
                                )
                                <> normalise_company_number_for_match(
                                    uf.selected_company_number
                                )
                        )
                  )
            )
            INSERT INTO document_company_matches (
                raw_document_id,
                company_id,
                match_method,
                match_score,
                is_accepted,
                review_required,
                source_company_number,
                source_company_name,
                matched_company_number,
                matched_company_name,
                match_details,
                updated_at
            )
            SELECT
                raw_document_id,
                selected_company_id,
                'selected_company_conflict',
                0.5,
                FALSE,
                TRUE,
                COALESCE(
                    extracted_company_number,
                    pdf_metadata_company_number
                ),
                COALESCE(
                    extracted_company_name,
                    pdf_metadata_company_name
                ),
                matched_company_number,
                matched_company_name,
                jsonb_build_object(
                    'source', 'selected_company_upload',
                    'conflict', TRUE,
                    'extracted_company_number', extracted_company_number,
                    'pdf_metadata_company_number', pdf_metadata_company_number,
                    'selected_company_number', selected_company_number,
                    'extracted_company_name', extracted_company_name,
                    'pdf_metadata_company_name', pdf_metadata_company_name,
                    'selected_company_name', selected_company_name
                ),
                NOW()
            FROM selected_conflicts
            UNION ALL
            SELECT
                dm.raw_document_id,
                exact_company.company_id,
                exact_company.match_method,
                1.0,
                TRUE,
                FALSE,
                exact_company.source_company_number,
                dm.company_name,
                exact_company.matched_company_number,
                exact_company.matched_company_name,
                jsonb_build_object(
                    'source', 'company_number',
                    'metadata_company_number', dm.company_number,
                    'pdf_metadata_company_number', rd.pdf_metadata_company_number,
                    'selected_source', exact_company.match_method
                ),
                NOW()
            FROM document_metadata dm
            JOIN raw_documents rd
                ON rd.id = dm.raw_document_id
            LEFT JOIN uploaded_files uf
                ON uf.raw_document_id = dm.raw_document_id
            JOIN LATERAL (
                SELECT
                    c.id AS company_id,
                    source_numbers.match_method,
                    source_numbers.company_number AS source_company_number,
                    c.company_number AS matched_company_number,
                    c.official_company_name AS matched_company_name
                FROM (
                    VALUES
                        (
                            dm.company_number,
                            'exact_metadata_company_number',
                            1
                        ),
                        (
                            rd.pdf_metadata_company_number,
                            'exact_pdf_metadata_company_number',
                            2
                        ),
                        (
                            uf.selected_company_number,
                            'exact_selected_company_number',
                            3
                        )
                ) AS source_numbers(company_number, match_method, priority)
                JOIN companies c
                    ON normalise_company_number_for_match(c.company_number)
                       = normalise_company_number_for_match(
                            source_numbers.company_number
                       )
                WHERE source_numbers.company_number IS NOT NULL
                ORDER BY source_numbers.priority
                LIMIT 1
            ) exact_company ON TRUE
            LEFT JOIN selected_conflicts selected_conflict
                ON selected_conflict.raw_document_id = dm.raw_document_id
            WHERE selected_conflict.raw_document_id IS NULL
            ON CONFLICT (raw_document_id)
            DO UPDATE SET
                company_id = EXCLUDED.company_id,
                match_method = EXCLUDED.match_method,
                match_score = EXCLUDED.match_score,
                is_accepted = EXCLUDED.is_accepted,
                review_required = EXCLUDED.review_required,
                source_company_number = EXCLUDED.source_company_number,
                source_company_name = EXCLUDED.source_company_name,
                matched_company_number = EXCLUDED.matched_company_number,
                matched_company_name = EXCLUDED.matched_company_name,
                match_details = EXCLUDED.match_details,
                updated_at = NOW()
            WHERE document_company_matches.manual_override = FALSE;
        """)

        exact_matches = cur.rowcount

    return exact_matches


def match_fuzzy_company_names(
    conn,
    candidate_threshold: float,
    auto_accept_threshold: float,
) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT set_limit(%s);", (candidate_threshold,))
        cur.execute("""
            INSERT INTO document_company_matches (
                raw_document_id,
                company_id,
                match_method,
                match_score,
                is_accepted,
                review_required,
                source_company_number,
                source_company_name,
                matched_company_number,
                matched_company_name,
                match_details,
                updated_at
            )
            SELECT
                candidate.raw_document_id,
                candidate.company_id,
                CASE
                    WHEN candidate.match_score >= %(auto_accept_threshold)s
                    THEN 'fuzzy_company_name_auto'
                    ELSE 'fuzzy_company_name_review'
                END,
                candidate.match_score,
                candidate.match_score >= %(auto_accept_threshold)s,
                candidate.match_score < %(auto_accept_threshold)s,
                candidate.source_company_number,
                candidate.source_company_name,
                candidate.matched_company_number,
                candidate.matched_company_name,
                jsonb_build_object(
                    'source', 'company_name',
                    'candidate_threshold', %(candidate_threshold)s,
                    'auto_accept_threshold', %(auto_accept_threshold)s,
                    'normalised_source_name',
                    normalise_company_name_for_match(candidate.source_company_name),
                    'normalised_matched_name',
                    normalise_company_name_for_match(candidate.matched_company_name)
                ),
                NOW()
            FROM (
                SELECT
                    dm.raw_document_id,
                    dm.company_number AS source_company_number,
                    dm.company_name AS source_company_name,
                    best.company_id,
                    best.company_number AS matched_company_number,
                    best.official_company_name AS matched_company_name,
                    best.match_score
                FROM document_metadata dm
                LEFT JOIN document_company_matches existing_match
                    ON existing_match.raw_document_id = dm.raw_document_id
                   AND existing_match.manual_override = FALSE
                   AND (
                        existing_match.match_method LIKE 'exact%%'
                        OR existing_match.match_method = 'selected_company_conflict'
                   )
                JOIN LATERAL (
                    SELECT
                        c.id AS company_id,
                        c.company_number,
                        c.official_company_name,
                        similarity(
                            normalise_company_name_for_match(dm.company_name),
                            normalise_company_name_for_match(c.official_company_name)
                        ) AS match_score
                    FROM companies c
                    WHERE normalise_company_name_for_match(dm.company_name)
                          %% normalise_company_name_for_match(c.official_company_name)
                    ORDER BY
                        match_score DESC,
                        (c.company_status = 'Active') DESC,
                        c.company_number
                    LIMIT 1
                ) best ON TRUE
                WHERE existing_match.raw_document_id IS NULL
                  AND dm.company_name IS NOT NULL
                  AND normalise_company_name_for_match(dm.company_name)
                      IS NOT NULL
                  AND normalise_company_name_for_match(dm.company_name)
                      !~ '^(AD01|AD03|AP01|AP03|AR01|AA01|AA06|CH01|CS01|DS01|IN01|MR01|MR04|NM01|PSC01|PSC04|PSC05|RPOQ|SH01|SH08|TM01)( |$)'
                  AND dm.company_name !~* (
                      'confirmation statement|appointment of director|'
                      || 'termination of appointment|companies house|'
                      || 'application to strike off'
                  )
            ) candidate
            WHERE candidate.match_score >= %(candidate_threshold)s
            ON CONFLICT (raw_document_id)
            DO UPDATE SET
                company_id = EXCLUDED.company_id,
                match_method = EXCLUDED.match_method,
                match_score = EXCLUDED.match_score,
                is_accepted = EXCLUDED.is_accepted,
                review_required = EXCLUDED.review_required,
                source_company_number = EXCLUDED.source_company_number,
                source_company_name = EXCLUDED.source_company_name,
                matched_company_number = EXCLUDED.matched_company_number,
                matched_company_name = EXCLUDED.matched_company_name,
                match_details = EXCLUDED.match_details,
                updated_at = NOW()
            WHERE document_company_matches.manual_override = FALSE
              AND document_company_matches.match_method NOT LIKE 'exact%%';
        """, {
            "candidate_threshold": candidate_threshold,
            "auto_accept_threshold": auto_accept_threshold,
        })

        fuzzy_matches = cur.rowcount

    return fuzzy_matches


def get_match_summary(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                match_method,
                is_accepted,
                review_required,
                COUNT(*) AS match_count
            FROM document_company_matches
            GROUP BY match_method, is_accepted, review_required
            ORDER BY match_method, is_accepted DESC, review_required;
        """)
        return cur.fetchall()


def get_unmatched_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM document_metadata dm
            LEFT JOIN document_company_matches dcm
                ON dcm.raw_document_id = dm.raw_document_id
            WHERE dcm.raw_document_id IS NULL;
        """)
        return cur.fetchone()[0]


def run_matching(
    candidate_threshold: float,
    auto_accept_threshold: float,
    exact_only: bool,
    dry_run: bool,
) -> None:
    conn = psycopg2.connect(DATABASE_URL)

    try:
        require_companies_table(conn)
        create_matching_objects(conn)

        exact_matches = match_exact_company_numbers(conn)
        fuzzy_matches = 0

        if not exact_only:
            fuzzy_matches = match_fuzzy_company_names(
                conn=conn,
                candidate_threshold=candidate_threshold,
                auto_accept_threshold=auto_accept_threshold,
            )

        summary = get_match_summary(conn)
        unmatched_count = get_unmatched_count(conn)

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

        print("Company matching complete")
        print(f"Exact company-number matches: {exact_matches}")
        print(f"Fuzzy company-name matches: {fuzzy_matches}")
        print(f"Unmatched metadata rows: {unmatched_count}")

        print("")
        print("Match summary")

        for method, is_accepted, review_required, count in summary:
            print(
                f"{method} | accepted={is_accepted} | "
                f"review={review_required} | count={count}"
            )

        if dry_run:
            print("")
            print("Dry run requested; changes were rolled back.")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Match extracted document metadata to the imported companies "
            "table using exact company numbers first, then pg_trgm fuzzy "
            "company-name matching."
        ),
    )
    parser.add_argument(
        "--candidate-threshold",
        type=float,
        default=DEFAULT_CANDIDATE_THRESHOLD,
        help=(
            "Minimum trigram similarity to store a fuzzy candidate. "
            f"Default: {DEFAULT_CANDIDATE_THRESHOLD}."
        ),
    )
    parser.add_argument(
        "--auto-accept-threshold",
        type=float,
        default=DEFAULT_AUTO_ACCEPT_THRESHOLD,
        help=(
            "Fuzzy matches at or above this score are auto-accepted. "
            f"Default: {DEFAULT_AUTO_ACCEPT_THRESHOLD}."
        ),
    )
    parser.add_argument(
        "--exact-only",
        action="store_true",
        help="Only run exact company-number matching.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run matching and roll back changes at the end.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.auto_accept_threshold < args.candidate_threshold:
        raise ValueError(
            "--auto-accept-threshold must be greater than or equal to "
            "--candidate-threshold"
        )

    run_matching(
        candidate_threshold=args.candidate_threshold,
        auto_accept_threshold=args.auto_accept_threshold,
        exact_only=args.exact_only,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
