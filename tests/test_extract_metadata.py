from datetime import date
from pathlib import Path

import pytest

import extract_metadata as metadata_parser


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.parametrize(
    ("filename", "expected_form_type", "expected_document_type"),
    [
        ("sample_ap01.txt", "AP01", "director_appointment"),
        ("sample_ch01.txt", "CH01", "director_details_change"),
        ("sample_mr01.txt", "MR01", "charge_registration"),
    ],
)
def test_extract_metadata_recognises_representative_form_types(
    filename,
    expected_form_type,
    expected_document_type,
):
    text = (FIXTURES_DIR / filename).read_text()

    metadata = metadata_parser.extract_metadata(text)

    assert metadata["form_type"] == expected_form_type
    assert metadata["document_type"] == expected_document_type


def test_extract_company_number_preserves_leading_zero():
    text = "Company Name: EXAMPLE LIMITED\nCompany Number: 05218004\n"

    assert metadata_parser.extract_company_number(text) == "05218004"


def test_extract_company_number_supports_prefixed_numbers():
    text = "Company Name: NORTHERN WIDGETS LTD\nCompany Number: SC123456\n"

    assert metadata_parser.extract_company_number(text) == "SC123456"


def test_extract_company_name_from_standard_company_name_label():
    text = "Company Name: EXAMPLE TRADING LIMITED\nCompany Number: 01234567\n"

    assert metadata_parser.extract_company_name(text) == "EXAMPLE TRADING LIMITED"


def test_extract_company_name_rejects_generic_document_title():
    assert metadata_parser.clean_company_name("CS01 Confirmation statement") is None


def test_extract_metadata_handles_empty_text_gracefully():
    metadata = metadata_parser.extract_metadata("")

    assert metadata["company_number"] is None
    assert metadata["company_name"] is None
    assert metadata["form_type"] is None
    assert metadata["document_type"] == "unknown"
    assert metadata["confidence_score"] == 0


def test_extract_metadata_handles_malformed_text_without_exception():
    metadata = metadata_parser.extract_metadata("» | ]]] Receivedforfiling ???")

    assert metadata["document_type"] == "unknown"
    assert metadata["confidence_score"] == 0


def test_extract_metadata_returns_filing_date_and_high_confidence_for_complete_form():
    text = (FIXTURES_DIR / "sample_ap01.txt").read_text()

    metadata = metadata_parser.extract_metadata(text)

    assert metadata["filing_date"] == date(2020, 4, 27)
    assert metadata["confidence_score"] >= 0.9


def test_calculate_confidence_scores_more_complete_metadata_higher():
    sparse = {
        "company_number": None,
        "company_name": None,
        "form_type": None,
        "document_type": "unknown",
        "filing_date": None,
        "extra_metadata": {},
    }
    complete = {
        "company_number": "01234567",
        "company_name": "EXAMPLE LIMITED",
        "form_type": "AP01",
        "document_type": "director_appointment",
        "filing_date": date(2024, 1, 1),
        "extra_metadata": {"appointment_date": "2024-01-01"},
    }

    assert metadata_parser.calculate_confidence(sparse) == 0
    assert metadata_parser.calculate_confidence(complete) == 1.0
