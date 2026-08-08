import match_document_companies as matcher


class RecordingCursor:
    rowcount = 0

    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


class RecordingConnection:
    def __init__(self):
        self.cursor_instance = RecordingCursor()

    def cursor(self):
        return self.cursor_instance


def test_choose_company_match_prefers_exact_company_number():
    metadata = {
        "company_number": " 05218004 ",
        "company_name": "Wrong OCR Name Limited",
    }
    companies = [
        {
            "id": 1,
            "company_number": "99999999",
            "official_company_name": "WRONG OCR NAME LIMITED",
            "company_status": "Active",
        },
        {
            "id": 2,
            "company_number": "05218004",
            "official_company_name": "EXAMPLE TRADING LIMITED",
            "company_status": "Dissolved",
        },
    ]

    match = matcher.choose_company_match(metadata, companies)

    assert match["company"]["company_number"] == "05218004"
    assert match["match_method"] == "exact_metadata_company_number"
    assert match["is_accepted"] is True
    assert match["review_required"] is False


def test_choose_company_match_auto_accepts_strong_fuzzy_name_match():
    metadata = {
        "company_number": None,
        "company_name": "Shrinkit Ltd",
    }
    companies = [
        {
            "id": 1,
            "company_number": "02926555",
            "official_company_name": "SHRINKIT LIMITED",
            "company_status": "Active",
        },
        {
            "id": 2,
            "company_number": "01234567",
            "official_company_name": "OTHER COMPANY LIMITED",
            "company_status": "Active",
        },
    ]

    match = matcher.choose_company_match(metadata, companies)

    assert match["company"]["company_number"] == "02926555"
    assert match["match_method"] == "fuzzy_company_name_auto"
    assert match["is_accepted"] is True
    assert match["review_required"] is False


def test_choose_company_match_flags_ambiguous_fuzzy_match_for_review():
    metadata = {
        "company_number": None,
        "company_name": "ABC SERVICES LTD",
    }
    companies = [
        {
            "id": 1,
            "company_number": "00000001",
            "official_company_name": "ABC SERVICE LIMITED",
            "company_status": "Active",
        },
        {
            "id": 2,
            "company_number": "00000002",
            "official_company_name": "ABC SERVICES LIMITED",
            "company_status": "Active",
        },
    ]

    match = matcher.choose_company_match(
        metadata,
        companies,
        candidate_threshold=0.70,
        auto_accept_threshold=0.85,
        ambiguity_margin=0.20,
    )

    assert match is not None
    assert match["match_method"] == "fuzzy_company_name_review"
    assert match["is_accepted"] is False
    assert match["review_required"] is True
    assert match["is_ambiguous"] is True


def test_choose_company_match_returns_none_below_fuzzy_threshold():
    metadata = {
        "company_number": None,
        "company_name": "COMPLETELY DIFFERENT LIMITED",
    }
    companies = [
        {
            "id": 1,
            "company_number": "12345678",
            "official_company_name": "NORTH SEA HOLDINGS LIMITED",
            "company_status": "Active",
        },
    ]

    assert (
        matcher.choose_company_match(
            metadata,
            companies,
            candidate_threshold=0.90,
        )
        is None
    )


def test_exact_database_match_accepts_optional_raw_document_scope():
    conn = RecordingConnection()

    matcher.match_exact_company_numbers(conn, raw_document_ids=[101, 102])

    sql, params = conn.cursor_instance.calls[0]

    assert "dm.raw_document_id = ANY(%(raw_document_ids)s)" in sql
    assert params == {"raw_document_ids": [101, 102]}


def test_fuzzy_database_match_checks_second_best_candidate_for_ambiguity():
    conn = RecordingConnection()

    matcher.match_fuzzy_company_names(
        conn,
        candidate_threshold=0.75,
        auto_accept_threshold=0.90,
        ambiguity_margin=0.03,
        raw_document_ids=[101],
    )

    _, set_limit_params = conn.cursor_instance.calls[0]
    sql, params = conn.cursor_instance.calls[1]

    assert set_limit_params == (0.75,)
    assert "LEAD(match_score)" in sql
    assert "second_best_match_score" in sql
    assert "ranked.match_score" in sql
    assert "<= %(ambiguity_margin)s" in sql
    assert "AND NOT candidate.is_ambiguous" in sql
    assert params["ambiguity_margin"] == 0.03
    assert params["raw_document_ids"] == [101]
