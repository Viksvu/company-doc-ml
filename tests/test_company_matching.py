import match_document_companies as matcher


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
