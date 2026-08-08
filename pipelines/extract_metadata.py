import re
from datetime import date, datetime

import psycopg2
from psycopg2.extras import Json

from pipeline_config import get_database_url

DATABASE_URL = get_database_url()
PARSER_VERSION = "v12"


FORM_TYPES = {
    "AD01": "change_registered_office",
    "AD03": "sail_records_location_change",
    "AP01": "director_appointment",
    "TM01": "director_termination",
    "CS01": "confirmation_statement",
    "DS01": "voluntary_strike_off",
    "IN01": "incorporation",
    "CH01": "director_details_change",
    "PSC01": "psc_notification",
    "PSC04": "psc_details_change",
    "PSC05": "rle_details_change",
    "AP03": "secretary_appointment",
    "NM01": "company_name_change",
    "AA01": "accounting_reference_date_change",
    "AA06": "parent_guarantee_statement",
    "MR01": "charge_registration",
    "MR04": "charge_satisfaction",
    "SH01": "return_of_allotment",
    "SH08": "share_class_name_change",
    "RPOQ": "default_service_address_change",
    "AR01": "annual_return",
}


def create_metadata_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS document_metadata (
                id BIGSERIAL PRIMARY KEY,

                raw_document_id BIGINT UNIQUE NOT NULL
                    REFERENCES raw_documents(id) ON DELETE CASCADE,

                company_number TEXT,
                company_name TEXT,
                form_type TEXT,
                document_type TEXT,
                filing_date DATE,

                extra_metadata JSONB NOT NULL DEFAULT '{}',

                confidence_score NUMERIC(4, 3),
                parser_version TEXT NOT NULL,

                extraction_error TEXT,

                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

    conn.commit()


def create_source_metadata_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE raw_documents
            ADD COLUMN IF NOT EXISTS pdf_metadata JSONB NOT NULL DEFAULT '{}',
            ADD COLUMN IF NOT EXISTS pdf_metadata_company_number TEXT,
            ADD COLUMN IF NOT EXISTS pdf_metadata_company_name TEXT;
        """)

    conn.commit()


def normalise_text(text: str) -> str:
    """
    Removes inconsistent whitespace while preserving line breaks.
    """

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


COMPANY_NUMBER_VALUE_PATTERN = (
    r"[A-Z$]{0,2}[ \t]*(?:\d[ \t|IlOoQSBZ]*){6,8}"
)

OTHER_DIRECTORSHIPS_PATTERN = (
    r"\b(?:t?\s*other\s+directorships|tother\s+directorships|"
    r"owrectors\s+only\s+tother\s+directorships)\b"
)


def get_page_text(text: str, page_number: int) -> str:
    page_match = re.search(
        rf"---\s*page\s+{page_number}\s*---\n(.*?)(?=\n---\s*page\s+\d+\s*---|\Z)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if page_match:
        return page_match.group(1)

    return text if page_number == 1 else ""


def split_before_other_directorships(text: str) -> str:
    match = re.search(
        OTHER_DIRECTORSHIPS_PATTERN,
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return text[:match.start()]

    return text


def get_legacy_primary_zone(text: str) -> str:
    page_one = get_page_text(text, 1)
    return split_before_other_directorships(page_one)


def normalise_company_number(value: str) -> str | None:
    decoded = decode_ocr_digits(value)
    decoded = decoded.replace("$C", "SC").replace("$c", "SC")
    compact = re.sub(r"[^A-Za-z0-9]", "", decoded).upper()

    if compact.startswith("C") and len(compact) == 8:
        compact = f"0{compact[1:]}"

    match = re.fullmatch(r"([A-Z]{0,2})(\d{6,8})", compact)

    if not match:
        return None

    prefix, digits = match.groups()

    if not prefix:
        digits = digits.zfill(8)

    return f"{prefix}{digits}"


def add_company_number_candidate(
    candidates: list[dict],
    value: str,
    source: str,
    primary_eligible: bool,
    confidence: str,
    excluded_reason: str | None = None,
) -> None:
    company_number = normalise_company_number(value)

    if not company_number:
        return

    candidate = {
        "value": company_number,
        "source": source,
        "confidence": confidence,
        "primary_eligible": primary_eligible,
    }

    if excluded_reason:
        candidate["excluded_reason"] = excluded_reason

    if candidate not in candidates:
        candidates.append(candidate)


def extract_company_number_candidates(text: str) -> list[dict]:
    candidates: list[dict] = []
    primary_zone = get_legacy_primary_zone(text)

    labelled_patterns = [
        (
            r"company\s+(?:number|no\.?)\s*[:|\-]?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),
        (
            r"registered\s+(?:number|no\.?)\s*[:|\-]?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),
        (
            r"company\s+registration\s+(?:number|no\.?)\s*[:|\-]?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),
        (
            r"registered\s+no\.?\s*,?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),
    ]

    for match in re.finditer(
        (
            r"legacy_crop_company_number\s*[:\-]?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),
        text,
        flags=re.IGNORECASE,
    ):
        add_company_number_candidate(
            candidates,
            match.group(1),
            "legacy_field_crop",
            True,
            "high",
        )

    for pattern in labelled_patterns:
        for match in re.finditer(
            pattern,
            primary_zone,
            flags=re.IGNORECASE,
        ):
            add_company_number_candidate(
                candidates,
                match.group(1),
                "legacy_primary_zone",
                True,
                "high",
            )

    generic_primary_patterns = [
        r"\(\s*([A-Z]{0,2}\d{6,8})\s*\)",
        (
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\s+"
            r"([A-Z]{0,2}\d{6,8})\s+(?:true|false|\d{4}-\d{2}-\d{2})\b"
        ),
    ]

    for pattern in generic_primary_patterns:
        for match in re.finditer(
            pattern,
            primary_zone,
            flags=re.IGNORECASE,
        ):
            add_company_number_candidate(
                candidates,
                match.group(1),
                "legacy_primary_zone",
                True,
                "medium",
            )

    other_zone = text[len(split_before_other_directorships(text)):]

    for match in re.finditer(
        (
            r"company\s+(?:number|no\.?|mmb|numb[ea]r)\s*[:|\-]?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),
        other_zone,
        flags=re.IGNORECASE,
    ):
        add_company_number_candidate(
            candidates,
            match.group(1),
            "other_directorships_or_continuation",
            False,
            "low",
            "inside_other_directorships_or_continuation_page",
        )

    ocr_candidate = extract_ocr_company_number_candidate(primary_zone)

    if ocr_candidate:
        add_company_number_candidate(
            candidates,
            ocr_candidate,
            "ocr_decoded_primary_zone",
            False,
            "low",
            "ocr_decoded_unvalidated",
        )

    return candidates


def choose_primary_company_number(candidates: list[dict]) -> str | None:
    for confidence in ("high", "medium", "low"):
        for candidate in candidates:
            if (
                candidate["primary_eligible"]
                and candidate["confidence"] == confidence
            ):
                return candidate["value"]

    return None


def clean_company_name(value: str) -> str | None:
    value = re.sub(
        r"^\s*in\s+full\s+",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" _:-|")

    value = re.sub(
        r"\b(limited|ltd|plc|llp|c\.?i\.?c\.?)\b.*$",
        lambda match: match.group(0).split()[0],
        value,
        flags=re.IGNORECASE,
    )

    value = value.strip(" _:-|")

    value = re.sub(
        r"^in\s+(?=.*\b(?:limited|ltd|plc|llp|c\.?i\.?c\.?)\b)",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(" _:-|")

    value = re.sub(
        r"^(?:and\s+wales\)?|england\s+and\s+wales\)?|"
        r"\(?england\s+and\s+wales\)?)\s+",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(" _:-|")

    generic_document_titles = {
        "accounts",
        "cs01",
        "cs01 confirmation statement",
        "cs01 confirmation statement ef",
        "confirmation statement",
        "psc04 change of individual person with significant control details",
        "psc04 change of individual person with significant control psc details",
        "change of individual person with significant control details",
        "change of individual person with significant control psc details",
        "dormant accounts",
        "dormant company accounts",
        "financial statements",
        "unaudited financial statements",
        "micro-entity accounts",
        "abridged accounts",
        "england",
        "england and wales",
        "scotland",
        "wales",
        "(england and wales)",
    }

    if value.lower() in generic_document_titles:
        return None

    if re.fullmatch(
        r"(?:CS01(?:\s*\(?ef\)?)?\s+)?confirmation\s+statement",
        value,
        flags=re.IGNORECASE,
    ):
        return None

    if re.fullmatch(
        (
            r"(?:PSC04(?:\s*\(?ef\)?)?\s+)?change\s+of\s+individual\s+"
            r"person\s+with\s+significant\s+control\s*(?:\(PSC\)\s*)?"
            r"details"
        ),
        value,
        flags=re.IGNORECASE,
    ):
        return None

    noisy_fragments = [
        "available use",
        "webcheck",
        "postcode",
        "company name availability",
        "registered office",
        "*insert full name",
        "insert full name",
        "eyfull",
        "specified orindicated",
        "specified or indicated",
        "uk-incorporated parent",
        "companies act",
        "page 1",
        "be changed to",
        "companies house",
    ]

    if any(fragment in value.lower() for fragment in noisy_fragments):
        return None

    if re.search(r"\b[A-Z]\d{2}\s+\d{2}/\d{2}/\d{4}\s+#\d+\b", value):
        return None

    if re.fullmatch(r"[A-Z]{0,2}\d{6,8}", value, flags=re.IGNORECASE):
        return None

    if not 2 <= len(value) <= 255:
        return None

    return value


def clean_legacy_company_name_block(value: str) -> str | None:
    lines = []

    for line in value.splitlines():
        line = re.sub(
            r"^\s*(?:company\s+name\s+in\s+full|legacy_crop_company_name)"
            r"\s*[:|\-]?\s*",
            "",
            line,
            flags=re.IGNORECASE,
        )
        line = re.sub(r"\bday\s+month\s+year\b.*$", "", line, flags=re.IGNORECASE)
        line = line.strip(" _:-|")

        if not line:
            continue

        if re.search(
            r"\b(?:company\s+number|date\s+of|appointment|forename|surname)\b",
            line,
            flags=re.IGNORECASE,
        ):
            continue

        lines.append(line)

    value = " ".join(lines)
    value = re.sub(r"\s+", " ", value).strip(" _:-|")

    return clean_company_name(value)


def extract_legacy_company_name(text: str) -> str | None:
    crop_match = re.search(
        (
            r"legacy_crop_company_name\s*[:\-]?\s*"
            r"(.*?)(?=\nlegacy_crop_|\n---\s*page\s+\d+\s*---|\Z)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if crop_match:
        company_name = clean_legacy_company_name_block(crop_match.group(1))

        if company_name:
            return company_name

    primary_zone = get_legacy_primary_zone(text)

    block_patterns = [
        (
            r"company\s+name\s+in\s+full\s*[:|\-]?\s*"
            r"(.*?)(?=\n\s*day\s+month\s+year\b|\bday\s+month\s+year\b)"
        ),
        (
            r"company\s+name\s+in\s+full\s*[:|\-]?\s*"
            r"(.*?)(?=\n\s*date\s+of\s+(?:appointment|birth)\b)"
        ),
    ]

    for pattern in block_patterns:
        match = re.search(
            pattern,
            primary_zone,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if not match:
            continue

        company_name = clean_legacy_company_name_block(match.group(1))

        if company_name:
            return company_name

    return None


def extract_company_name(text: str) -> str | None:
    legacy_company_name = extract_legacy_company_name(text)

    if legacy_company_name:
        return legacy_company_name

    patterns = [
        # Company Name: EXAMPLE LIMITED
        (
            r"company\s+name(?:\s+in\s+full)?\s*[:\-]?\s*"
            r"(.+?)(?=\s+(?:company\s+number|received|new\s+address|"
            r"date\s+of\s+change|important\s+notice|electronically\s+"
            r"filed|notification\s+details|cessation\s+details|"
            r"details\s+prior\s+to\s+change|new\s+appointment\s+details)"
            r"\b|\n|\||$)"
        ),

        # Company Name in EXAMPLE LIMITED full:
        r"company\s+name\s+in\s+(.{2,255}?)\s+full\s*:",

        # Noisy AA06 OCR:
        # Company name ... in ... full | EXAMPLE LIMITED specified ...
        (
            r"company\s+name\w*\s+in\s+\w*full\s*\|\s*"
            r"(.+?)(?=\s+specified|\n|$)"
        ),

        # Articles of association of EXAMPLE LIMITED
        (
            r"articles\s+of\s+association\s+of\s+"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),

        # Certificate of incorporation on change of name
        (
            r"now\s+incorporated\s+under\s+the\s+name\s+(?:of\s+)?"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),

        # Name of company: EXAMPLE LIMITED
        r"name\s+of\s+company\s*[:\-]?\s*([^\n|]+)",

        # Auditor resignation letter:
        # EXAMPLE LIMITED (Company number 01234567)
        (
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
            r"\s+\(\s*company\s+number\b"
        ),

        # Legacy charge form: *Insert full name of company EXAMPLE LIMITED
        (
            r"\*?\s*insert\s+full\s+name\s+of\s+company\s+"
            r"(.+?)(?=\s+date\s+of\s+creation|\n|$)"
        ),

        # Unaudited Financial Statements for the Year Ended ... for EXAMPLE LTD
        (
            r"\b(?:unaudited\s+)?financial\s+statements\s+for\s+the\s+"
            r"(?:year|period)\s+ended\s+"
            r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}\s+for\s+"
            r"([^|\n]{2,255}?)(?=\s*(?:---\s*page|\n|$))"
        ),

        # Old accounts title:
        # Registered No. 1816248 ... EXAMPLE LIMITED
        (
            r"registered\s+no\.?\s*,?\s*[A-Z]{0,2}\d{6,8}"
            r"(?:\s*\([^)]*\))?\s+"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),

        # Company Registration No. 01234567 ... EXAMPLE LIMITED
        (
            r"company\s+registration\s+no\.?\s*"
            r"[A-Z]{0,2}\d{6,8}[^\n]*\n"
            r"(?:\s*\(?england\s+and\s+wales\)?\s*\n)?"
            r"(?:\s*\n)*\s*([^\n|]+)"
        ),

        # Company Registration Number 01234567, followed by company name
        (
            r"company\s+registration\s+number\s*"
            r"[A-Z]{0,2}\d{6,8}[^\n]*\n"
            r"(?:\s*\(?england\s+and\s+wales\)?\s*\n)?"
            r"(?:\s*\n)*\s*([^\n|]+)"
        ),

        # EXAMPLE LIMITED ... Company Registration Number: 01234567
        (
            r"^---\s*page\s+\d+\s*---\s*\n"
            r"\s*([^\n|]{2,255}?)\s+"
            r"(?:company\s+limited|company\s+registration\s+number)"
        ),

        (
            r"^([^\n|]{2,255}?)\s+"
            r"(?:company\s+limited|company\s+registration\s+number)"
        ),

        # Registered Number 01234567, followed by company name
        (
            r"registered\s+number[^\n]*\n"
            r"(?:\s*\n)*\s*([^\n|]+)"
        ),

        # Company Registration No. 01234567 EXAMPLE LIMITED ...
        (
            r"company\s+registration\s+no\.?\s*"
            r"[A-Z]{0,2}\d{6,8}[^\n]*?\s+"
            r"([A-Z][A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),

        # COMPANY REGISTRATION NUMBER 01234567 - EXAMPLE LIMITED
        (
            r"company\s+registration\s+number\s+"
            r"[A-Z]{0,2}\d{6,8}\s*[-:]\s*"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),

        # EXAMPLE LIMITED UNAUDITED ACCOUNTS FOR THE YEAR ENDED ...
        (
            r"^([A-Z][A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
            r"\s+(?:annual\s+report\s+and\s+)?"
            r"(?:(?:filleted|unaudited|abridged|micro-entity)\s+){0,4}"
            r"(?:accounts|financial\s+statements)\b"
        ),

        # EXAMPLE LIMITED (Registered number: 01234567)
        (
            r"^([^\n|]{2,255}?)\s*"
            r"\(\s*registered\s+number\s*[:\-]?"
        ),

        # EXAMPLE PLC (3676824)
        (
            r"^([^\n|]{2,255}?)\s*"
            r"\(\s*[A-Z]{0,2}\d{6,8}\s*\)"
        ),

        # XBRL-style text:
        # EXAMPLE LIMITED | Companies House |
        r"^([^|\n]{2,255}?)\s*\|\s*companies\s+house\s*\|",

        # XBRL-style text without pipe separators:
        # EXAMPLE LIMITED Companies House 01234567
        r"^(.{2,255}?)\s+companies\s+house\s+[A-Z]{0,2}\d{6,8}\b",

        # Dormant/Micro-entity Accounts - EXAMPLE LIMITED Companies House
        (
            r"^(?:(?:dormant|micro-entity)\s+(?:company\s+)?accounts)\s*-\s*"
            r"(.{2,255}?)\s+companies\s+house\b"
        ),

        # EXAMPLE LIMITED 10957117 false 2023-04-01 ...
        (
            r"^(.{2,255}?\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
            r"\s+[A-Z]{0,2}\d{6,8}\s+(?:true|false|\d{4}-\d{2}-\d{2})\b"
        ),

        # EXAMPLE LIMITED Acorah Software Products - Accounts Production
        r"^(.{2,255}?)\s+acorah\s+software\s+products\s+-\s+accounts\s+production\b",

        # EXAMPLE LIMITED - Period Ending 2024-03-31
        r"^(.{2,255}?)\s+-\s+period\s+ending\s+\d{4}-\d{2}-\d{2}\b",

        # EXAMPLE LIMITED - Accounts
        r"^(.{2,255}?)\s+-\s+accounts\b",

        # Shareholder Resolution heading
        (
            r"^---\s*page\s+\d+\s*---.*?\n\s*"
            r"([A-Z][A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)\s*;"
            r"\s*\n\s*company\s+number"
        ),

        # Shareholder Resolution heading without reliable punctuation
        (
            r"^---\s*page\s+\d+\s*---.*?\n\s*"
            r"([A-Z][A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
            r"\s+(?:company\s+number|shareholder\s+resolution)\b"
        ),

        # EXAMPLE_LIMITED_31_Mar_2025_set_of_accounts_for_filing.html
        (
            r"^([A-Z0-9_]+?(?:LIMITED|LTD|PLC|LLP|CIC))"
            r"_[0-9]{1,2}_[A-Za-z]{3}_[0-9]{4}_set_of_accounts"
        ),

        # Unaudited Financial Statements ... for EXAMPLE LIMITED
        (
            r"\bunaudited\s+financial\s+statements\b"
            r".{0,120}?\bfor\s+([^|\n]{2,255}?)"
            r"(?=\s*(?:---\s*page|\n|$))"
        ),
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )

        if match:
            company_name = clean_company_name(match.group(1))

            if company_name:
                return company_name

    return None


def extract_company_number(text: str) -> str | None:
    company_number_candidates = extract_company_number_candidates(text)
    company_number = choose_primary_company_number(company_number_candidates)

    if company_number:
        return company_number

    if company_number_candidates and re.search(
        r"\b(?:288[abc]|appointment\s+of\s+director\s+or\s+secretary|"
        r"terminating\s+appointment)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return None

    patterns = [
        (
            r"company\s+(?:number|no\.?)\s*[:\-]?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),
        (
            r"registered\s+(?:number|no\.?)\s*[:\-]?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),
        (
            r"company\s+registration\s+number\s*[:\-]?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),
        (
            r"company\s+registration\s+no\.?\s*[:\-]?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),
        (
            r"registered\s+no\.?\s*,?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
        ),

        # Company name followed by number in brackets
        r"\(\s*([A-Z]{0,2}\d{6,8})\s*\)",

        # XBRL-style title: EXAMPLE LIMITED 10957117 false ...
        (
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\s+"
            r"([A-Z]{0,2}\d{6,8})\s+(?:true|false|\d{4}-\d{2}-\d{2})\b"
        ),
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)

        if match:
            company_number = normalise_company_number(match.group(1))

            if company_number:
                return company_number

    return None


def extract_form_type(text: str) -> str | None:
    for form_type in sorted(
        FORM_TYPES,
        key=len,
        reverse=True,
    ):
        pattern = rf"\b{re.escape(form_type)}(?:\s*\([^)]*\))?\b"

        if re.search(pattern, text, flags=re.IGNORECASE):
            return form_type

    return None


def infer_document_type(
    text: str,
    form_type: str | None,
) -> str:
    if form_type:
        return FORM_TYPES[form_type]

    keyword_rules = [
        (
            r"\bcertificate\s+of\s+incorporation\s+on\s+change\s+of\s+name\b",
            "certificate_name_change",
        ),
        (
            r"\b(?:IN01|Application\s+to\s+register\s+a\s+company|"
            r"certificate\s+of\s+incorporation)\b",
            "incorporation",
        ),
        (
            r"\b(?:AA01|change\s+of\s+accounting\s+reference\s+date)\b",
            "accounting_reference_date_change",
        ),
        (
            r"\b(?:CH01|CHO\s*1|change\s+of\s+particulars\s+for\s+director)\b",
            "director_details_change",
        ),
        (
            r"\bchange\s+in\s+the\s+details\s+of\s+a\s+director\s+or\s+"
            r"(?:company\s+)?secretary\b",
            "director_details_change",
        ),
        (
            r"\b(?:PSC01|PSCO1|notice\s+of\s+individual\s+person\s+with\s+"
            r"significant\s+control)\b",
            "psc_notification",
        ),
        (
            r"\b(?:PSC05|PSCO5|change\s+of\s+relevant\s+legal\s+entity\s+"
            r"\(RLE\)\s+details)\b",
            "rle_details_change",
        ),
        (
            r"\bnotice\s+of\s+relevant\s+legal\s+entity\s+\(RLE\)\s+"
            r"person\s+with\s+significant\s+control\s+\(PSC\)\b|"
            r"\brle\s+details\s+date\s+of\s+becoming\b",
            "psc_rle_notification",
        ),
        (
            r"\b(?:withdrawal\s+of\s+person\s+with\s+significant\s+control\s+"
            r"\(PSC\)\s+statement)\b",
            "psc_statement_withdrawal",
        ),
        (
            r"\bnotice\s+of\s+ceasing\s+to\s+be\s+a\s+person\s+with\s+"
            r"significant\s+control\b",
            "psc_cessation",
        ),
        (
            r"\b(?:AP03|appointment\s+of\s+secretary)\b",
            "secretary_appointment",
        ),
        (
            r"\b(?:NM01|NMO1|notice\s+of\s+change\s+of\s+name\s+by\s+"
            r"resolution)\b",
            "company_name_change",
        ),
        (
            r"\b(?:AA06|AA0O|statement\s+of\s+guarantee\s+by\s+a\s+parent)\b",
            "parent_guarantee_statement",
        ),
        (
            r"\b(?:MR04|statement\s+of\s+satisfaction\s+in\s+full\s+or\s+"
            r"in\s+part\s+of\s+charge)\b",
            "charge_satisfaction",
        ),
        (
            r"\b(?:MR01|registration\s+of\s+a\s+charge)\b",
            "charge_registration",
        ),
        (
            r"\b(?:companies\s+form\s+no\.?\s*395|particulars\s+of\s+a\s+"
            r"charge)\b",
            "particulars_of_charge",
        ),
        (
            r"\b(?:companies\s+form\s+no\.?\s*403a|declaration\s+of\s+"
            r"satisfaction\s+in\s+full\s+or\s+in\s+part\s+of\s+"
            r"mortgage\s+or\s+charge)\b",
            "legacy_charge_satisfaction",
        ),
        (
            r"\b(?:SH08|S\s*H\s*0?8|notice\s+of\s+name\s+or\s+other\s+"
            r"designation\s+of\s+class\s+of\s+shares)\b",
            "share_class_name_change",
        ),
        (
            r"\b(?:RPOQ|change\s+of\s+service\s+address\s+to\s+default\s+"
            r"address)\b",
            "default_service_address_change",
        ),
        (
            r"\b(?:AD03|change\s+of\s+location\s+of\s+company\s+records\s+"
            r"to\s+the\s+single\s+alternative\s+inspection\s+location|"
            r"single\s+alternative\s+inspection\s+location\s+\(SAIL\))\b",
            "sail_records_location_change",
        ),
        (
            r"\b(?:AR01|annual\s+return)\b",
            "annual_return",
        ),
        (
            r"\b(?:resign\s+as\s+auditors?|section\s*519\s+of\s+the\s+"
            r"companies\s+act|formal\s+resignation\s+as\s+auditors?)\b",
            "auditor_resignation",
        ),
        (
            r"\bpublication\s+date\s+in\s+the\s+gazette\b[\s\S]*?"
            r"\bstruck\s+off\s+the\s+register\b",
            "gazette_strike_off_notice",
        ),
        (
            r"\b(?:the\s+companies\s+act\s+2006\s+)?(?:private\s+company\s+"
            r"limited\s+by\s+shares\s+)?articles\s+of\s+association\s+of\b",
            "articles_of_association",
        ),
        (
            r"\b(?:micro-entity|dormant)\s+accounts\b",
            "accounts",
        ),
        (
            r"\b(?:unaudited\s+financial\s+statements|unaudited\s+accounts|"
            r"abridged\s+accounts|abbreviated\s+accounts|unaudited\s+"
            r"abbreviated\s+accounts|abbreviated\s+balance\s+sheet|"
            r"annual\s+report\s+and\s+unaudited\s+accounts|statement\s+"
            r"of\s+financial\s+position|financial\s+"
            r"statements\s+for\s+the\s+year\s+ending|financial\s+"
            r"statements\s+for\s+the\s+year\s+ended)\b",
            "accounts",
        ),
        (
            r"\bbalance\s+sheet\s+as\s+at\b",
            "accounts",
        ),
        (
            r"\bspecial\s+resolution\b",
            "special_resolution",
        ),
        (
            r"\breturn\s+of\s+allotment\s+of\s+shares\b",
            "return_of_allotment",
        ),
        (
            r"\b88\s*\(\s*2\s*\)",
            "return_of_allotment",
        ),
        (
            r"\btermination\s+of\s+appointment\b",
            "director_termination",
        ),
        (
            r"\btermination\s+of\s+a\s+director\s+appointment\b",
            "director_termination",
        ),
        (
            r"\bterminating\s+appointment\b",
            "director_termination",
        ),
        (
            r"\bappointment\s+of\s+(?:a\s+)?director\s+or\s+secretary\b",
            "director_appointment",
        ),
        (
            r"\bchange\s+of\s+individual\s+person\s+with\s+"
            r"significant\s+control\b",
            "psc_details_change",
        ),
        (
            r"\bapplication\s+to\s+strike\s+off\b",
            "voluntary_strike_off",
        ),
        (
            r"\bconfirmation\s+statement\b",
            "confirmation_statement",
        ),
    ]

    for pattern, document_type in keyword_rules:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return document_type

    return "unknown"


def parse_date(date_value: str) -> date | None:
    supported_formats = [
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
    ]

    for date_format in supported_formats:
        try:
            return datetime.strptime(
                date_value,
                date_format,
            ).date()
        except ValueError:
            continue

    return None


def parse_written_date(date_value: str) -> date | None:
    supported_formats = [
        "%d %B %Y",
        "%d %b %Y",
    ]

    date_value = date_value.replace("™", "")
    date_value = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", date_value)
    date_value = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", date_value)
    date_value = re.sub(r"\s+", " ", date_value).strip()

    for date_format in supported_formats:
        try:
            return datetime.strptime(
                date_value,
                date_format,
            ).date()
        except ValueError:
            continue

    return None


def parse_any_date(date_value: str) -> date | None:
    return parse_date(date_value) or parse_written_date(date_value)


OCR_DIGIT_TRANSLATION = str.maketrans({
    "O": "0",
    "o": "0",
    "Q": "0",
    "I": "1",
    "l": "1",
    "|": "1",
    "S": "5",
    "s": "5",
    "Z": "2",
    "B": "8",
})


def decode_ocr_digits(value: str) -> str:
    return value.translate(OCR_DIGIT_TRANSLATION)


def extract_ocr_company_number_candidate(text: str) -> str | None:
    match = re.search(
        r"company\s+number.{0,80}",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        return None

    decoded = decode_ocr_digits(match.group(0))
    digits = re.sub(r"\D", "", decoded)

    if 6 <= len(digits) <= 8:
        return digits.zfill(8)

    if len(digits) > 8:
        return digits[-8:]

    return None


def extract_ocr_date_candidate(
    text: str,
    label_pattern: str,
) -> str | None:
    match = re.search(
        rf"{label_pattern}.{{0,80}}",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        return None

    decoded = decode_ocr_digits(match.group(0))
    numbers = re.findall(r"\d{1,4}", decoded)

    if len(numbers) < 3:
        return None

    day, month, year = numbers[-3:]

    if len(year) == 2:
        year = f"20{year}" if int(year) < 50 else f"19{year}"

    try:
        parsed_date = date(int(year), int(month), int(day))
    except ValueError:
        return None

    return parsed_date.isoformat()


def extract_filing_date(text: str) -> date | None:
    date_pattern = (
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}"
        r"|\d{4}-\d{2}-\d{2})"
    )

    patterns = [
        # Handles both "Received for filing" and "Receivedforfiling"
        rf"received\s*for\s*filing.{{0,150}}?{date_pattern}",

        rf"filing\s+date\s*[:\-]?\s*{date_pattern}",
        rf"received\s+on\s*[:\-]?\s*{date_pattern}",
        rf"date\s+of\s+filing\s*[:\-]?\s*{date_pattern}",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if match:
            parsed_date = parse_date(match.group(1))

            if parsed_date:
                return parsed_date

    footer_match = re.search(
        r"\b[A-Z]\d{1,3}\s+(\d{1,2}/\d{1,2}/\d{4})\s+\d{1,4}\b",
        text,
        flags=re.IGNORECASE,
    )

    if footer_match:
        return parse_date(footer_match.group(1))

    return None


def extract_accounts_metadata(text: str) -> dict:
    metadata = {}

    if re.search(
        (
            r"\bmicro-entity\s+accounts\b|"
            r"\b(?:uk-bus|frs-bus|bus|ns\d+):Micro-entities\b|"
            r"\bmicro_entity_frs_105\b"
        ),
        text,
        flags=re.IGNORECASE,
    ):
        metadata["accounts_type"] = "micro_entity"

    elif re.search(
        r"\bdormant\s+(?:company\s+)?accounts\b",
        text,
        flags=re.IGNORECASE,
    ):
        metadata["accounts_type"] = "dormant"

    elif re.search(
        r"\bunaudited\s+financial\s+statements\b",
        text,
        flags=re.IGNORECASE,
    ):
        metadata["accounts_type"] = "unaudited"

    elif re.search(
        r"\babridged\s+accounts\b|\babridged\s+financial\s+statements\b",
        text,
        flags=re.IGNORECASE,
    ):
        metadata["accounts_type"] = "abridged"

    elif re.search(
        r"\babbreviated\s+accounts\b|\babbreviated\s+balance\s+sheet\b",
        text,
        flags=re.IGNORECASE,
    ):
        metadata["accounts_type"] = "abbreviated"

    elif re.search(
        (
            r"\b(?:uk-bus|frs-bus|bus|ns\d+):FullAccounts\b|"
            r"\bfull\s+accounts\b"
        ),
        text,
        flags=re.IGNORECASE,
    ):
        metadata["accounts_type"] = "full"

    date_patterns = [
        (
            r"(?:period|year)\s+from\s+"
            r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})\s+to\s+"
            r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})"
        ),
        (
            r"(?:balance\s+sheet\s+)?as\s+at\s+"
            r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})"
        ),
        (
            r"(?:year|period)\s+ended\s+"
            r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})"
        ),
        (
            r"accounts.*?"
            r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})"
        ),
    ]

    for pattern in date_patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if not match:
            continue

        try:
            if len(match.groups()) >= 2 and match.group(2):
                period_start = parse_written_date(match.group(1))
                period_end = parse_written_date(match.group(2))

                if period_start:
                    metadata["accounting_period_start"] = (
                        period_start.isoformat()
                    )
            else:
                period_end = parse_written_date(match.group(1))

            if not period_end:
                continue

            metadata["accounting_period_end"] = (
                period_end.isoformat()
            )
            break

        except ValueError:
            continue

    return metadata


def clean_multiline_value(value: str) -> str:
    value = re.sub(r"\s*\n\s*", ", ", value)
    value = re.sub(r",\s*,+", ", ", value)
    value = re.sub(r"\s{2,}", " ", value)

    return value.strip(" ,:-")


def parse_noisy_numeric_date(value: str) -> date | None:
    exact_match = re.search(
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        value,
    )

    if exact_match:
        candidate = exact_match.group(1)

        if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2}", candidate):
            candidate = re.sub(
                r"(\d{1,2}[/-]\d{1,2}[/-])(\d{2})$",
                lambda match: (
                    f"{match.group(1)}20{match.group(2)}"
                    if int(match.group(2)) < 50
                    else f"{match.group(1)}19{match.group(2)}"
                ),
                candidate,
            )

        return parse_date(candidate)

    decoded = decode_ocr_digits(value)
    numbers = re.findall(r"\d{1,4}", decoded)

    for index in range(0, max(len(numbers) - 2, 0)):
        day, month, year = numbers[index:index + 3]

        if len(year) == 2:
            year = f"20{year}" if int(year) < 50 else f"19{year}"

        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            continue

    return None


def extract_legacy_crop_value(text: str, field_name: str) -> str | None:
    match = re.search(
        rf"legacy_crop_{re.escape(field_name)}\s*[:\-]?\s*([^\n]+)",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    value = re.sub(r"\s+", " ", match.group(1)).strip(" :-|")

    return value or None


def clean_legacy_person_name(value: str) -> str | None:
    if re.search(r"\d", value):
        return None

    if re.search(r"[^A-Za-z .,'-]", value):
        return None

    value = re.sub(r"\s+", " ", value).strip(" .,'-")

    if not 2 <= len(value) <= 160:
        return None

    return value


def extract_legacy_other_directorships(text: str) -> list[dict]:
    zone = text[len(split_before_other_directorships(text)):]
    directorships = []

    for match in re.finditer(
        (
            r"(?:company|com\s*pany)\s+(?:mmb|numb[ea]r)"
            r"\s*[:|\-]?\s*"
            rf"({COMPANY_NUMBER_VALUE_PATTERN})"
            r"(?:\s*\n\s*([^\n|]{3,160}))?"
        ),
        zone,
        flags=re.IGNORECASE,
    ):
        line_start = zone.rfind("\n", 0, match.start()) + 1
        line_end = zone.find("\n", match.start())

        if line_end == -1:
            line_end = len(zone)

        source_line = zone[line_start:line_end]

        if re.match(
            r"\s*company\s+number\s*\|",
            source_line,
            flags=re.IGNORECASE,
        ):
            continue

        company_number = normalise_company_number(match.group(1))

        if not company_number:
            continue

        company = {
            "company_number": company_number,
            "source": "other_directorships_section",
            "confidence": "low",
        }

        if match.group(2):
            company_name = clean_company_name(match.group(2))

            if company_name:
                company["company_name"] = company_name

        if company not in directorships:
            directorships.append(company)

    return directorships


def extract_legacy_footer_date_candidate(text: str) -> str | None:
    match = re.search(
        r"\b[A-Z]\d{1,3}\s+(\d{1,2}/\d{1,2}/\d{4})\s+\d{1,4}\b",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    parsed_date = parse_date(match.group(1))

    if not parsed_date:
        return None

    return parsed_date.isoformat()


def extract_ad01_metadata(text: str) -> dict:
    """
    Extracts the new registered office address from an AD01 form.
    """

    patterns = [
        (
            r"new\s+address(?:\s+details)?\s*[:\-]?\s*"
            r"(.*?)"
            r"(?=\n\s*(?:the company confirms|please note|authorisation)\b)"
        ),
        (
            r"new\s+registered\s+office\s+address\s*[:\-]?\s*"
            r"(.*?)"
            r"(?=\n\s*(?:the company confirms|please note|authorisation)\b)"
        ),
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if match:
            address = clean_multiline_value(match.group(1))

            if address:
                return {
                    "new_registered_office": address,
                }

    return {}


def extract_ap01_metadata(text: str) -> dict:
    """
    Basic parser for director appointment forms.
    Extend this when you inspect more AP01 documents.
    """

    metadata = {}

    if re.search(
        r"\bappointment\s+of\s+director\s+or\s+secretary\b",
        text,
        flags=re.IGNORECASE,
    ):
        metadata["legacy_form_type"] = "288a"

    company_number_candidates = extract_company_number_candidates(text)

    if company_number_candidates:
        metadata["company_number_candidates"] = company_number_candidates

    legacy_company_name = extract_legacy_company_name(text)

    if legacy_company_name:
        metadata["company_name_from_legacy_block"] = legacy_company_name

    name_patterns = [
        r"full\s+forename\(s\)\s*[:\-]?\s*([^\n]+)",
        r"director(?:'s)?\s+name\s*[:\-]?\s*([^\n]+)",
        r"name\s+of\s+director\s*[:\-]?\s*([^\n]+)",
    ]

    for pattern in name_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)

        if match:
            metadata["director_name"] = match.group(1).strip(" :-")
            break

    crop_forenames = extract_legacy_crop_value(text, "forenames")
    crop_surname = extract_legacy_crop_value(text, "surname")

    if crop_forenames and crop_surname:
        director_name = clean_legacy_person_name(
            f"{crop_forenames} {crop_surname}"
        )

        if director_name:
            metadata["director_name_from_crops"] = director_name

    appointment_match = re.search(
        r"date\s+of\s+appointment\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if appointment_match:
        appointment_date = parse_date(appointment_match.group(1))

        if appointment_date:
            metadata["appointment_date"] = appointment_date.isoformat()

    crop_appointment_date = extract_legacy_crop_value(
        text,
        "appointment_date",
    )

    if crop_appointment_date and "appointment_date" not in metadata:
        appointment_date = parse_noisy_numeric_date(crop_appointment_date)

        if appointment_date:
            metadata["appointment_date"] = appointment_date.isoformat()

    ocr_company_number = extract_ocr_company_number_candidate(text)

    if ocr_company_number:
        metadata["ocr_company_number_candidate"] = ocr_company_number

    ocr_appointment_date = extract_ocr_date_candidate(
        text,
        r"date\s+of\s+appointment|appointment\s*date",
    )

    if ocr_appointment_date and "appointment_date" not in metadata:
        metadata["ocr_appointment_date_candidate"] = ocr_appointment_date

    other_directorships = extract_legacy_other_directorships(text)

    if other_directorships:
        metadata["other_directorships"] = other_directorships

    footer_date = extract_legacy_footer_date_candidate(text)

    if footer_date:
        metadata["legacy_footer_date_candidate"] = footer_date

    return metadata


def extract_tm01_metadata(text: str) -> dict:
    """
    Basic parser for director termination forms.
    """

    metadata = {}

    if re.search(
        r"\bterminating\s+appointment\b",
        text,
        flags=re.IGNORECASE,
    ):
        metadata["legacy_form_type"] = "288b"

    company_number_candidates = extract_company_number_candidates(text)

    if company_number_candidates:
        metadata["company_number_candidates"] = company_number_candidates

    legacy_company_name = extract_legacy_company_name(text)

    if legacy_company_name:
        metadata["company_name_from_legacy_block"] = legacy_company_name

    name_patterns = [
        r"name\s+of\s+director\s*[:\-]?\s*([^\n]+)",
        r"director(?:'s)?\s+name\s*[:\-]?\s*([^\n]+)",
    ]

    for pattern in name_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)

        if match:
            metadata["director_name"] = match.group(1).strip(" :-")
            break

    termination_match = re.search(
        r"date\s+of\s+termination\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if termination_match:
        termination_date = parse_date(termination_match.group(1))

        if termination_date:
            metadata["termination_date"] = termination_date.isoformat()

    ocr_company_number = extract_ocr_company_number_candidate(text)

    if ocr_company_number:
        metadata["ocr_company_number_candidate"] = ocr_company_number

    ocr_termination_date = extract_ocr_date_candidate(
        text,
        r"date\s+of\s+termination\s+of\s+appointment|"
        r"date\s+of\s+termination",
    )

    if ocr_termination_date and "termination_date" not in metadata:
        metadata["ocr_termination_date_candidate"] = ocr_termination_date

    other_directorships = extract_legacy_other_directorships(text)

    if other_directorships:
        metadata["other_directorships"] = other_directorships

    footer_date = extract_legacy_footer_date_candidate(text)

    if footer_date:
        metadata["legacy_footer_date_candidate"] = footer_date

    return metadata


def extract_psc04_metadata(text: str) -> dict:
    metadata = {}

    psc_name_match = re.search(
        (
            r"details\s+prior\s+to\s+change.*?"
            r"name\s*[:\-]?\s*"
            r"(.+?)"
            r"(?=\s+date\s+of\s+birth\b|\n|$)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if psc_name_match:
        psc_name = re.sub(r"\s+", " ", psc_name_match.group(1))
        metadata["psc_name"] = psc_name.strip(" :-")

    dob_match = re.search(
        (
            r"date\s+of\s+birth\s*[:\-]?\s*"
            r"(\*{2}|\d{1,2})[/-](\d{1,2})[/-](\d{4})"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if dob_match:
        metadata["psc_birth_month"] = dob_match.group(2).zfill(2)
        metadata["psc_birth_year"] = dob_match.group(3)

    change_date_match = re.search(
        (
            r"date\s+of\s+change\s*[:\-]?\s*"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if change_date_match:
        change_date = parse_date(change_date_match.group(1))

        if change_date:
            metadata["psc_change_date"] = change_date.isoformat()

    address_match = re.search(
        (
            r"new\s+service\s+address\s*[:\-]?\s*"
            r"(.*?)"
            r"(?=\s+(?:electronically\s+filed|register\s+entry\s+date|"
            r"authorisation)\b|\n\s*(?:electronically\s+filed|"
            r"register\s+entry\s+date|authorisation)\b|$)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if address_match:
        address = clean_multiline_value(address_match.group(1))

        if address:
            metadata["new_service_address"] = address

    register_entry_match = re.search(
        (
            r"register\s+entry\s+date(?:\s+register\s+entry\s+date)?"
            r"\s*[:\-]?\s*"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if register_entry_match:
        register_entry_date = parse_date(register_entry_match.group(1))

        if register_entry_date:
            metadata["register_entry_date"] = (
                register_entry_date.isoformat()
            )

    return metadata


def extract_ds01_metadata(text: str) -> dict:
    metadata = {}

    directors_match = re.search(
        (
            r"authorising\s+company\s+director\(s\)\s*[:\-]?\s*"
            r"(.*?)"
            r"(?=\s+signature\s+date\b|\n\s*signature\s+date\b|$)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if directors_match:
        directors = clean_multiline_value(directors_match.group(1))

        if directors:
            metadata["authorising_directors"] = directors

    signature_date_match = re.search(
        (
            r"signature\s+date\s*[:\-]?\s*"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if signature_date_match:
        signature_date = parse_date(signature_date_match.group(1))

        if signature_date:
            metadata["signature_date"] = signature_date.isoformat()

    return metadata


def extract_sh01_metadata(text: str) -> dict:
    metadata = {}

    if re.search(r"\b88\s*\(\s*2\s*\)", text, flags=re.IGNORECASE):
        metadata["legacy_form_type"] = "88(2)"

    class_match = re.search(
        (
            r"class\s+of\s+shares\s*"
            r"([A-Z][A-Z0-9 &/.'-]{1,80}?)"
            r"(?=\s+\(?ordinary\s+or\s*preference\b|\s+\||\n|$)"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if class_match:
        metadata["share_class"] = re.sub(
            r"\s+",
            " ",
            class_match.group(1),
        ).strip(" :-|")

    allotment_date_patterns = [
        (
            r"date\s+or\s+period\s+during\s+which.*?"
            r"shares\s+were\s+allotted.*?"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})"
        ),
        (
            r"shares\s+allotted.*?"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})"
        ),
        (
            r"\ballotted\s+on\s*[:\-]?\s*"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})"
        ),
    ]

    for pattern in allotment_date_patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if not match:
            continue

        allotment_date = parse_date(match.group(1))

        if allotment_date:
            metadata["allotment_date"] = allotment_date.isoformat()
            break

    shares_match = re.search(
        (
            r"(?:number\s+of\s+shares|shares\s+allotted)\s*[:\-]?\s*"
            r"([0-9][0-9,]*)"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if shares_match:
        metadata["shares_allotted"] = shares_match.group(1).replace(",", "")

    return metadata


def extract_special_resolution_metadata(text: str) -> dict:
    metadata = {}

    resolution_date_patterns = [
        (
            r"\b(?:meeting|company)\s+held\s+on\s+"
            r"(\d{1,2}(?:st|nd|rd|th|™)?\s*[A-Za-z]+\s+\d{4})"
        ),
        (
            r"\bpassed\b.{0,80}?\bon\s+"
            r"(\d{1,2}(?:st|nd|rd|th|™)?\s*[A-Za-z]+\s+\d{4})"
        ),
    ]

    for pattern in resolution_date_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)

        if not match:
            continue

        resolution_date = parse_written_date(match.group(1))

        if resolution_date:
            metadata["resolution_date"] = resolution_date.isoformat()
            break

    return metadata


def extract_in01_metadata(text: str) -> dict:
    metadata = {}

    incorporation_match = re.search(
        (
            r"(?:given\s+at\s+companies\s+house,\s+cardiff,\s+on|"
            r"certifies\s+that.*?\bis\s+this\s+day\s+incorporated.*?\bon)\s+"
            r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if incorporation_match:
        incorporation_date = parse_written_date(incorporation_match.group(1))

        if incorporation_date:
            metadata["incorporation_date"] = incorporation_date.isoformat()

    company_type_match = re.search(
        r"company\s+type\s*[:\-]?\s*(.+?)(?=\s+situation\s+of|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if company_type_match:
        metadata["company_type"] = re.sub(
            r"\s+",
            " ",
            company_type_match.group(1),
        ).strip(" :-")

    country_match = re.search(
        r"situation\s+of\s*(?:registered\s+)?office\s*[:\-]?\s*"
        r"(.+?)(?=\s+proposed\s+registered|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if country_match:
        metadata["registered_office_country"] = re.sub(
            r"\s+",
            " ",
            country_match.group(1),
        ).strip(" :-")

    office_match = re.search(
        (
            r"proposed\s+registered\s+(?:office\s+)?address\s*[:\-]?\s*"
            r"(.*?)"
            r"(?=\s+(?:sic\s+codes|i\s+wish|electronically\s+filed|"
            r"proposed\s+officers)\b|\n\s*(?:sic\s+codes|i\s+wish|"
            r"electronically\s+filed|proposed\s+officers)\b|$)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if office_match:
        address = clean_multiline_value(office_match.group(1))

        if address:
            metadata["registered_office_address"] = address

    sic_match = re.search(
        r"sic\s+codes?\s*[:\-]?\s*([0-9\s]{5,})",
        text,
        flags=re.IGNORECASE,
    )

    if sic_match:
        sic_codes = re.findall(r"\b\d{5}\b", sic_match.group(1))

        if sic_codes:
            metadata["sic_codes"] = sic_codes

    return metadata


def extract_ch01_metadata(text: str) -> dict:
    metadata = {}

    original_name_match = re.search(
        r"original\s+name\s*[:\-]?\s*(.+?)(?=\s+date\s+of\s*birth\b|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if original_name_match:
        metadata["director_name"] = re.sub(
            r"\s+",
            " ",
            original_name_match.group(1),
        ).strip(" :-")

    dob_match = re.search(
        r"date\s+of\s*birth\s*[:\-]?\s*(\*{2}|\d{1,2})[/-]?(\d{1,3})[/-](\d{4})",
        text,
        flags=re.IGNORECASE,
    )

    if dob_match:
        metadata["director_birth_month"] = dob_match.group(2)[-2:].zfill(2)
        metadata["director_birth_year"] = dob_match.group(3)

    change_date_match = re.search(
        (
            r"date\s+of\s*change\s*[:\-]?\s*"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if change_date_match:
        change_date = parse_date(change_date_match.group(1))

        if change_date:
            metadata["director_change_date"] = change_date.isoformat()

    new_name_match = re.search(
        r"new\s+name\s*[:\-]?\s*(.+?)(?=\s+country/state|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if new_name_match:
        metadata["new_director_name"] = re.sub(
            r"\s+",
            " ",
            new_name_match.group(1),
        ).strip(" :-")

    address_match = re.search(
        (
            r"new\s+service\s+address\s*[:\-]?\s*"
            r"(.*?)"
            r"(?=\s+(?:the\s+usual\s+residential|electronically\s+filed|"
            r"authorisation)\b|\n\s*(?:the\s+usual\s+residential|"
            r"electronically\s+filed|authorisation)\b|$)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if address_match:
        address = clean_multiline_value(address_match.group(1))

        if address:
            metadata["new_service_address"] = address

    return metadata


def extract_psc01_metadata(text: str) -> dict:
    metadata = {}

    became_match = re.search(
        (
            r"date\s+that\s+person\s+became\s+"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})\s+"
            r"registrable"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if became_match:
        became_date = parse_date(became_match.group(1))

        if became_date:
            metadata["became_registrable_date"] = became_date.isoformat()

    name_match = re.search(
        r"notification\s+details.*?name\s*[:\-]?\s*(.+?)(?=\s+service\s+address|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if name_match:
        metadata["psc_name"] = re.sub(
            r"\s+",
            " ",
            name_match.group(1),
        ).strip(" :-")

    dob_match = re.search(
        r"date\s+of\s+birth\s*[:\-]?\s*(\*{2}|\d{1,2})[/-]?(\d{1,3})[/-](\d{4})",
        text,
        flags=re.IGNORECASE,
    )

    if dob_match:
        metadata["psc_birth_month"] = dob_match.group(2)[-2:].zfill(2)
        metadata["psc_birth_year"] = dob_match.group(3)

    nationality_match = re.search(
        r"nationality\s*[:\-]?\s*([A-Z ]{2,80})(?=\s+electronically|\n|$)",
        text,
        flags=re.IGNORECASE,
    )

    if nationality_match:
        metadata["nationality"] = re.sub(
            r"\s+",
            " ",
            nationality_match.group(1),
        ).strip(" :-")

    control_match = re.search(
        r"nature\s+of\s+control\s*(.*?)(?=\s+register\s+entry\s+date|\n\s*register\s+entry\s+date|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if control_match:
        nature = clean_multiline_value(control_match.group(1))

        if nature:
            metadata["nature_of_control"] = nature

    register_entry_match = re.search(
        r"register\s+entry\s+date(?:\s+register\s+entry\s+date)?\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if register_entry_match:
        register_entry_date = parse_date(register_entry_match.group(1))

        if register_entry_date:
            metadata["register_entry_date"] = register_entry_date.isoformat()

    return metadata


def extract_psc05_metadata(text: str) -> dict:
    metadata = {}

    name_match = re.search(
        r"details\s+prior\s+to\s+change.*?name\s*[:\-]?\s*(.+?)(?=\s+new\s+details|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if name_match:
        metadata["rle_name"] = re.sub(
            r"\s+",
            " ",
            name_match.group(1),
        ).strip(" :-")

    change_date_match = re.search(
        r"date\s+of\s+change\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if change_date_match:
        change_date = parse_date(change_date_match.group(1))

        if change_date:
            metadata["rle_change_date"] = change_date.isoformat()

    office_match = re.search(
        (
            r"new\s+registered\s+or\s+.*?principal\s+office\s+address\s*[:\-]?\s*"
            r"(.*?)"
            r"(?=\s+(?:electronically\s+filed|register\s+entry\s+date|"
            r"authorisation)\b|\n\s*(?:electronically\s+filed|"
            r"register\s+entry\s+date|authorisation)\b|$)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if office_match:
        address = clean_multiline_value(office_match.group(1))

        if address:
            metadata["new_principal_office_address"] = address

    register_entry_match = re.search(
        r"register\s+entry\s+date(?:\s+register\s+entry\s+date)?\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if register_entry_match:
        register_entry_date = parse_date(register_entry_match.group(1))

        if register_entry_date:
            metadata["register_entry_date"] = register_entry_date.isoformat()

    return metadata


def extract_psc_rle_notification_metadata(text: str) -> dict:
    metadata = {}

    became_match = re.search(
        (
            r"date\s+of\s+becoming\s+a\s+"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})\s+"
            r"registrable\s+rle"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if became_match:
        became_date = parse_date(became_match.group(1))

        if became_date:
            metadata["became_registrable_date"] = became_date.isoformat()

    name_match = re.search(
        r"rle\s+details.*?name\s*[:\-]?\s*(.+?)(?=\s+registered\s+or\s+principal|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if name_match:
        metadata["rle_name"] = re.sub(
            r"\s+",
            " ",
            name_match.group(1),
        ).strip(" :-")

    office_match = re.search(
        (
            r"registered\s+or\s+principal\s+office\s+address\s*[:\-]?\s*"
            r"(.*?)"
            r"(?=\s+(?:legal\s+form|governing\s+law|register)\b|"
            r"\n\s*(?:legal\s+form|governing\s+law|register)\b|$)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if office_match:
        address = clean_multiline_value(office_match.group(1))

        if address:
            metadata["registered_or_principal_office_address"] = address

    labelled_fields = [
        ("legal_form", r"legal\s+form\s*[:\-]?\s*(.+?)(?=\s+governing\s+law|\n|$)"),
        ("governing_law", r"governing\s+law\s*[:\-]?\s*(.+?)(?=\s+register\s*:|\n|$)"),
        ("register", r"\bregister\s*[:\-]\s*(.+?)(?=\s+country/state\s+of\s+register|\n|$)"),
        (
            "country_state_of_register",
            r"country/state\s+of\s+register\s*[:\-]?\s*(.+?)(?=\s+registration\s+number|\n|$)",
        ),
        (
            "registration_number",
            r"registration\s+number\s*[:\-]?\s*([A-Z0-9 -]{2,80})(?=\s+nature\s+of\s+control|\n|$)",
        ),
    ]

    for key, pattern in labelled_fields:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)

        if match:
            metadata[key] = re.sub(r"\s+", " ", match.group(1)).strip(" :-")

    control_match = re.search(
        (
            r"nature\s+of\s+control\s*(.*?)"
            r"(?=\s+register\s+entry\s+date|\n\s*register\s+entry\s+date|"
            r"\s+electronically\s+filed|\n\s*electronically\s+filed|$)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if control_match:
        nature = clean_multiline_value(control_match.group(1))

        if nature:
            metadata["nature_of_control"] = nature

    return metadata


def extract_psc_cessation_metadata(text: str) -> dict:
    metadata = {}

    ceased_match = re.search(
        r"date\s+ceased\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if ceased_match:
        ceased_date = parse_date(ceased_match.group(1))

        if ceased_date:
            metadata["ceased_date"] = ceased_date.isoformat()

    name_match = re.search(
        r"cessation\s+details.*?name\s*[:\-]?\s*(.+?)(?=\s+register\s+entry|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if name_match:
        metadata["psc_name"] = re.sub(
            r"\s+",
            " ",
            name_match.group(1),
        ).strip(" :-")

    return metadata


def extract_psc_statement_withdrawal_metadata(text: str) -> dict:
    metadata = {}

    register_match = re.search(
        r"register\s+entry\s+date\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if register_match:
        register_entry_date = parse_date(register_match.group(1))

        if register_entry_date:
            metadata["register_entry_date"] = register_entry_date.isoformat()

    ceased_match = re.search(
        r"statement\s+ceased\s+to\s+be\s+true\s+on\s+"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if ceased_match:
        ceased_date = parse_date(ceased_match.group(1))

        if ceased_date:
            metadata["statement_ceased_date"] = ceased_date.isoformat()

    statement_match = re.search(
        r"psc\s+statement\s*(.*?)(?=\s+electronically\s+filed|\n\s*electronically\s+filed|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if statement_match:
        statement = clean_multiline_value(statement_match.group(1))

        if statement:
            metadata["psc_statement"] = statement

    return metadata


def extract_ap03_metadata(text: str) -> dict:
    metadata = {}

    appointment_match = re.search(
        r"date\s+of\s+appointment\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if appointment_match:
        appointment_date = parse_date(appointment_match.group(1))

        if appointment_date:
            metadata["appointment_date"] = appointment_date.isoformat()

    name_match = re.search(
        r"new\s+appointment\s+details.*?name\s*[:\-]?\s*(.+?)(?=\s+the\s+company|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if name_match:
        metadata["secretary_name"] = re.sub(
            r"\s+",
            " ",
            name_match.group(1),
        ).strip(" :-")

    return metadata


def extract_nm01_metadata(text: str) -> dict:
    metadata = {}

    existing_match = re.search(
        (
            r"existing\s+company[^\n]{0,200}?"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if existing_match:
        existing_name = clean_company_name(existing_match.group(1))

        if existing_name:
            metadata["existing_company_name"] = existing_name

    specific_proposed_match = re.search(
        (
            r"proposed\s+name\s*\|\s*"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if not specific_proposed_match:
        specific_proposed_match = re.search(
            (
                r"the\s+above\s+company\s+resolved\s+to\s+change\s+"
                r"the\s+company\s+name\s+to.*?proposed\s+name\s*\|\s*"
                r"([A-Z0-9 &.,'()+/-]{2,255}?"
                r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
            ),
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

    if specific_proposed_match:
        proposed_name = clean_company_name(specific_proposed_match.group(1))

        if proposed_name:
            metadata["proposed_company_name"] = proposed_name

    proposed_match = re.search(
        r"proposed\s+name\s*[:\-]?\s*(.+?)(?=\s+(?:signature|the\s+above\s+company|$)|\n)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if proposed_match and "proposed_company_name" not in metadata:
        proposed_name = clean_company_name(proposed_match.group(1))

        if proposed_name:
            metadata["proposed_company_name"] = proposed_name

    resolution_date_match = re.search(
        r"resolved\s+to\s+change.*?\bon\s+"
        r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if resolution_date_match:
        resolution_date = parse_written_date(resolution_date_match.group(1))

        if resolution_date:
            metadata["resolution_date"] = resolution_date.isoformat()

    ocr_company_number = extract_ocr_company_number_candidate(text)

    if ocr_company_number:
        metadata["ocr_company_number_candidate"] = ocr_company_number

    return metadata


def extract_aa01_metadata(text: str) -> dict:
    metadata = {}

    period_match = re.search(
        (
            r"accounting\s+reference\s+period\s+ending\s+"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})\s+"
            r"is\s+(shortened|extended)\s+so\s+as\s+at\s+to\s+end\s+on\s+"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if period_match:
        previous_date = parse_date(period_match.group(1))
        new_date = parse_date(period_match.group(3))

        if previous_date:
            metadata["previous_accounting_reference_date"] = (
                previous_date.isoformat()
            )

        metadata["accounting_reference_change_type"] = (
            period_match.group(2).lower()
        )

        if new_date:
            metadata["new_accounting_reference_date"] = new_date.isoformat()

    return metadata


def extract_aa06_metadata(text: str) -> dict:
    metadata = {}

    year_end_match = re.search(
        (
            r"date\s+of\s+financial\s+year.*?ending\s*"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2}|"
            r"\d{1,2}\s+[A-Za-z]+\s+\d{4})"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if year_end_match:
        year_end = parse_any_date(year_end_match.group(1))

        if year_end:
            metadata["financial_year_end"] = year_end.isoformat()

    guarantee_match = re.search(
        (
            r"this\s+guarantee\s+is\s+being\s+given\s+under\s+"
            r"(section\s+\d+[A-Za-z]?)"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if guarantee_match:
        metadata["guarantee_section"] = guarantee_match.group(1)

    parent_match = re.search(
        (
            r"guarantee\s+is\s+being\s+given\s+by\s+"
            r"(.+?)\s*"
            r"\(\s*company.{0,80}?number\s*"
            r"([A-Z]{0,2}\s*\d{6,8})\s*\)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if parent_match:
        parent_name = clean_company_name(parent_match.group(1))

        if parent_name:
            metadata["parent_company_name"] = parent_name

        metadata["parent_company_number"] = re.sub(
            r"\s+",
            "",
            parent_match.group(2),
        ).upper()

    return metadata


def extract_mr04_metadata(text: str) -> dict:
    metadata = {}

    charge_code_match = re.search(
        r"charge\s+code\s*[:\-]?\s*([0-9 ]{8,30})",
        text,
        flags=re.IGNORECASE,
    )

    if charge_code_match:
        metadata["charge_code"] = re.sub(r"\s+", "", charge_code_match.group(1))

    satisfaction_match = re.search(
        r"satisfaction\s+of\s+(in\s+full|in\s+part)\s+charge",
        text,
        flags=re.IGNORECASE,
    )

    if satisfaction_match:
        metadata["satisfaction_type"] = satisfaction_match.group(1).lower().replace(" ", "_")

    deliverer_match = re.search(
        r"details\s+of\s+the\s+person\s+delivering.*?name\s*[:\-]?\s*(.+?)\s+address\s*:",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if deliverer_match:
        metadata["deliverer_name"] = re.sub(
            r"\s+",
            " ",
            deliverer_match.group(1),
        ).strip(" :-")

    interest_match = re.search(
        r"interest\s*[:\-]?\s*([A-Z ]{2,80})(?=\s+authentication|\n|$)",
        text,
        flags=re.IGNORECASE,
    )

    if interest_match:
        metadata["deliverer_interest"] = re.sub(
            r"\s+",
            " ",
            interest_match.group(1),
        ).strip(" :-")

    return metadata


def extract_mr01_metadata(text: str) -> dict:
    metadata = {}

    creation_match = re.search(
        r"date\s+of\s+creation\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if creation_match:
        creation_date = parse_date(creation_match.group(1))

        if creation_date:
            metadata["charge_creation_date"] = creation_date.isoformat()

    charge_code_match = re.search(
        r"charge\s+code\s*[:\-]?\s*([0-9 ]{8,30})",
        text,
        flags=re.IGNORECASE,
    )

    if charge_code_match:
        metadata["charge_code"] = re.sub(r"\s+", "", charge_code_match.group(1))

    persons_match = re.search(
        r"persons\s+entitled\s*[:\-]?\s*(.+?)(?=\s+brief\s+description|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if persons_match:
        metadata["persons_entitled"] = re.sub(
            r"\s+",
            " ",
            persons_match.group(1),
        ).strip(" :-")

    description_match = re.search(
        r"brief\s+description\s*[:\-]?\s*(.+?)(?=\s+contains\s+fixed|\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if description_match:
        metadata["brief_description"] = re.sub(
            r"\s+",
            " ",
            description_match.group(1),
        ).strip(" :-")

    metadata["contains_fixed_charge"] = bool(
        re.search(r"contains\s+fixed\s+charge", text, flags=re.IGNORECASE)
    )
    metadata["contains_floating_charge"] = bool(
        re.search(r"contains\s+floating\s+charge", text, flags=re.IGNORECASE)
    )
    metadata["contains_negative_pledge"] = bool(
        re.search(r"contains\s+negative\s+pledge", text, flags=re.IGNORECASE)
    )

    return metadata


def extract_form_395_metadata(text: str) -> dict:
    metadata = {"legacy_form_type": "395"}

    creation_match = re.search(
        r"date\s+of\s+creation\s+of\s+the\s+charge\s*[:\-]?\s*(.+?)\s+description",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if creation_match:
        metadata["charge_creation_date_text"] = re.sub(
            r"\s+",
            " ",
            creation_match.group(1),
        ).strip(" :-|")

    instrument_match = re.search(
        r"description\s+of\s+the\s+instrument.*?\)\s*(.+?)\s+amount\s+secured",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if instrument_match:
        metadata["charge_instrument"] = clean_multiline_value(
            instrument_match.group(1)
        )

    secured_match = re.search(
        r"amount\s+secured\s+by\s+the\s+charge\s*(.+?)\s+names\s+and\s+addresses",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if secured_match:
        metadata["amount_secured"] = clean_multiline_value(secured_match.group(1))

    mortgagee_match = re.search(
        r"persons\s+entitled\s+to\s+the\s+charge\s*(.+?)\s+presenter",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if mortgagee_match:
        metadata["mortgagee"] = clean_multiline_value(mortgagee_match.group(1))

    return metadata


def extract_form_403a_metadata(text: str) -> dict:
    metadata = {"legacy_form_type": "403a"}

    if re.search(
        r"declaration\s+of\s+satisfaction\s+in\s+full",
        text,
        flags=re.IGNORECASE,
    ):
        metadata["satisfaction_type"] = "in_full"

    elif re.search(
        r"declaration\s+of\s+satisfaction\s+in\s+part",
        text,
        flags=re.IGNORECASE,
    ):
        metadata["satisfaction_type"] = "in_part"

    declarant_match = re.search(
        r"\bi\s*,?\s*([A-Z][A-Z .,'-]{2,120})\s+of\s+",
        text,
        flags=re.IGNORECASE,
    )

    if declarant_match:
        metadata["declarant_name"] = re.sub(
            r"\s+",
            " ",
            declarant_match.group(1),
        ).strip(" ,-")

    company_name_match = re.search(
        (
            r"\*?\s*insert\s+full\s+name\s+\+?\s*"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if company_name_match:
        company_name = clean_company_name(company_name_match.group(1))

        if company_name:
            metadata["company_name_from_form"] = company_name

    ocr_company_number = extract_ocr_company_number_candidate(text)

    if ocr_company_number:
        metadata["ocr_company_number_candidate"] = ocr_company_number

    return metadata


def extract_sh08_metadata(text: str) -> dict:
    metadata = {}

    assignment_match = re.search(
        r"date\s+of\s+assignment\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if assignment_match:
        assignment_date = parse_date(assignment_match.group(1))

        if assignment_date:
            metadata["assignment_date"] = assignment_date.isoformat()

    ocr_assignment_date = extract_ocr_date_candidate(
        text,
        r"date\s+of\s+assignment",
    )

    if ocr_assignment_date and "assignment_date" not in metadata:
        metadata["ocr_assignment_date_candidate"] = ocr_assignment_date

    ocr_company_number = extract_ocr_company_number_candidate(text)

    if ocr_company_number:
        metadata["ocr_company_number_candidate"] = ocr_company_number

    share_match = re.search(
        (
            r"existing\s+class/description\s+of\s+shares\s*"
            r"(.+?)\s+name\s+\(or\s+new\s+name\)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if share_match:
        metadata["existing_share_class"] = clean_multiline_value(
            share_match.group(1)
        )

    table_match = re.search(
        (
            r"existing\s+class/description\s+of\s+shares\s*\|?\s*"
            r"name\s+\(or\s+new\s+name\)\s+or\s+other\s+designation\s+"
            r"(.+?)(?=\s+(?:signature|i\s+am\s+signing)\b|\n\s*(?:signature|"
            r"i\s+am\s+signing)\b|$)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if table_match:
        table_text = clean_multiline_value(table_match.group(1))

        if table_text:
            metadata["share_class_change_text"] = table_text

    return metadata


def extract_rpoq_metadata(text: str) -> dict:
    metadata = {}

    person_match = re.search(
        r"changed\s+the\s+service\s+address\s+for\s+(.+?)\s+to\s+the\s+default\s+address",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if person_match:
        metadata["person_name"] = re.sub(
            r"\s+",
            " ",
            person_match.group(1),
        ).strip(" :-")

    address_match = re.search(
        r"to\s+the\s+default\s+address\s*[:\-]?\s*(.*?)\s+date\s+of\s+change",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if address_match:
        address = clean_multiline_value(address_match.group(1))

        if address:
            metadata["default_address"] = address

    change_date_match = re.search(
        r"date\s+of\s+change\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if change_date_match:
        change_date = parse_date(change_date_match.group(1))

        if change_date:
            metadata["change_date"] = change_date.isoformat()

    return metadata


def extract_ad03_metadata(text: str) -> dict:
    metadata = {}

    records_match = re.search(
        (
            r"the\s+following\s+records\s+have\s+moved\s+to\s+the\s+"
            r"single\s+alternative\s+inspection\s+location\s*[:\-]?\s*"
            r"(.*?)"
            r"(?=\s+authorisation\b|\n\s*authorisation\b|$)"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if records_match:
        records_text = clean_multiline_value(records_match.group(1))
        records = [
            record.strip(" .")
            for record in re.split(r"\s{2,}|,\s*", records_text)
            if record.strip(" .")
        ]

        if records:
            metadata["records_moved"] = records

    return metadata


def extract_ar01_metadata(text: str) -> dict:
    metadata = {}

    return_date_match = re.search(
        r"date\s+of\s*this\s+return\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if return_date_match:
        return_date = parse_date(return_date_match.group(1))

        if return_date:
            metadata["return_date"] = return_date.isoformat()

    sic_match = re.search(
        r"sic\s+codes?\s*[:\-]?\s*([0-9\s]{4,})",
        text,
        flags=re.IGNORECASE,
    )

    if sic_match:
        sic_codes = re.findall(r"\b\d{4,5}\b", sic_match.group(1))

        if sic_codes:
            metadata["sic_codes"] = sic_codes

    return metadata


def extract_auditor_resignation_metadata(text: str) -> dict:
    metadata = {}

    company_match = re.search(
        (
            r"dear\s+sirs,\s*"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if company_match:
        company_name = clean_company_name(company_match.group(1))

        if company_name:
            metadata["company_name_from_letter"] = company_name

    if "company_name_from_letter" not in metadata:
        company_match = re.search(
            (
                r"([A-Z0-9 &.,'()+/-]{2,255}?"
                r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
                r"\s+\(\s*company\s+number\b"
            ),
            text,
            flags=re.IGNORECASE,
        )

        if company_match:
            company_name = clean_company_name(company_match.group(1))

            if company_name:
                metadata["company_name_from_letter"] = company_name

    resignation_match = re.search(
        r"with\s+effect\s+from\s+(?:today'?s\s+date|"
        r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}|"
        r"\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2}))",
        text,
        flags=re.IGNORECASE,
    )

    if resignation_match and resignation_match.group(1):
        resignation_date = parse_any_date(resignation_match.group(1))

        if resignation_date:
            metadata["resignation_date"] = resignation_date.isoformat()

    if re.search(r"\bno\s+circumstances\b", text, flags=re.IGNORECASE):
        metadata["statement_of_circumstances"] = "none"

    stamp_match = re.search(
        r"\b[A-Z]\d{2}\s+(\d{2}/\d{2}/\d{4})\s+#\d+\s+COMPANIES\s+HOUSE\b",
        text,
        flags=re.IGNORECASE,
    )

    if stamp_match:
        stamp_date = parse_date(stamp_match.group(1))

        if stamp_date:
            metadata["companies_house_stamp_date"] = stamp_date.isoformat()

    return metadata


def extract_gazette_strike_off_metadata(text: str) -> dict:
    metadata = {}

    header_match = re.search(
        (
            r"^(?:---\s*page\s+\d+\s*---\s*)?"
            r"(?:first|second|final)?\s*gazette\s+notice\s*"
            r"([A-Z]{0,2}\d{6,8}|OC\d{6})\s+"
            r"([^\n]+?)\s*"
            r"(?:\n|\s{2,})\s*publication\s+date\s+in\s+the\s+gazette"
        ),
        get_page_text(text, 1),
        flags=re.IGNORECASE | re.MULTILINE,
    )

    if header_match:
        metadata["gazette_company_number"] = header_match.group(1).upper()
        company_name = clean_company_name(header_match.group(2))

        if company_name:
            metadata["gazette_company_name"] = company_name

    publication_match = re.search(
        r"publication\s+date\s+in\s+the\s+gazette\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if publication_match:
        publication_date = parse_date(publication_match.group(1))

        if publication_date:
            metadata["gazette_publication_date"] = publication_date.isoformat()

    return metadata


def extract_articles_metadata(text: str) -> dict:
    metadata = {}

    company_match = re.search(
        (
            r"articles\s+of\s+association\s+of\s+"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if company_match:
        company_name = clean_company_name(company_match.group(1))

        if company_name:
            metadata["company_name_from_articles"] = company_name

    if re.search(
        r"\bprivate\s+company\s+limited\s+by\s+shares\b",
        text,
        flags=re.IGNORECASE,
    ):
        metadata["company_type"] = "private_company_limited_by_shares"

    return metadata


def extract_certificate_name_change_metadata(text: str) -> dict:
    metadata = {}

    previous_match = re.search(
        (
            r"certifies\s+that\s+"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
            r"\s+having\s+by\s+special\s+resolution\s+changed\s+its\s+name"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if previous_match:
        previous_name = clean_company_name(previous_match.group(1))

        if previous_name:
            metadata["previous_company_name"] = previous_name

    new_match = re.search(
        (
            r"now\s+incorporated\s+under\s+the\s+name\s+(?:of\s+)?"
            r"([A-Z0-9 &.,'()+/-]{2,255}?"
            r"\b(?:LIMITED|LTD|PLC|LLP|C\.?I\.?C\.?)\b)"
        ),
        text,
        flags=re.IGNORECASE,
    )

    if new_match:
        new_name = clean_company_name(new_match.group(1))

        if new_name:
            metadata["new_company_name"] = new_name

    effective_match = re.search(
        r"given\s+at\s+companies\s+house,\s+cardiff,?\s*(?:the|on)\s+"
        r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})",
        text,
        flags=re.IGNORECASE,
    )

    if effective_match:
        effective_date = parse_written_date(effective_match.group(1))

        if effective_date:
            metadata["effective_date"] = effective_date.isoformat()

    return metadata


FORM_PARSERS = {
    "AD01": extract_ad01_metadata,
    "AD03": extract_ad03_metadata,
    "AP01": extract_ap01_metadata,
    "TM01": extract_tm01_metadata,
    "DS01": extract_ds01_metadata,
    "IN01": extract_in01_metadata,
    "CH01": extract_ch01_metadata,
    "PSC01": extract_psc01_metadata,
    "PSC04": extract_psc04_metadata,
    "PSC05": extract_psc05_metadata,
    "AP03": extract_ap03_metadata,
    "NM01": extract_nm01_metadata,
    "AA01": extract_aa01_metadata,
    "AA06": extract_aa06_metadata,
    "MR01": extract_mr01_metadata,
    "MR04": extract_mr04_metadata,
    "SH01": extract_sh01_metadata,
    "SH08": extract_sh08_metadata,
    "RPOQ": extract_rpoq_metadata,
    "AR01": extract_ar01_metadata,
}


DOCUMENT_TYPE_PARSERS = {
    "director_appointment": extract_ap01_metadata,
    "director_termination": extract_tm01_metadata,
    "return_of_allotment": extract_sh01_metadata,
    "special_resolution": extract_special_resolution_metadata,
    "incorporation": extract_in01_metadata,
    "director_details_change": extract_ch01_metadata,
    "psc_notification": extract_psc01_metadata,
    "psc_details_change": extract_psc04_metadata,
    "rle_details_change": extract_psc05_metadata,
    "psc_rle_notification": extract_psc_rle_notification_metadata,
    "psc_cessation": extract_psc_cessation_metadata,
    "psc_statement_withdrawal": extract_psc_statement_withdrawal_metadata,
    "secretary_appointment": extract_ap03_metadata,
    "company_name_change": extract_nm01_metadata,
    "parent_guarantee_statement": extract_aa06_metadata,
    "charge_registration": extract_mr01_metadata,
    "charge_satisfaction": extract_mr04_metadata,
    "particulars_of_charge": extract_form_395_metadata,
    "legacy_charge_satisfaction": extract_form_403a_metadata,
    "share_class_name_change": extract_sh08_metadata,
    "default_service_address_change": extract_rpoq_metadata,
    "sail_records_location_change": extract_ad03_metadata,
    "annual_return": extract_ar01_metadata,
    "auditor_resignation": extract_auditor_resignation_metadata,
    "gazette_strike_off_notice": extract_gazette_strike_off_metadata,
    "articles_of_association": extract_articles_metadata,
    "certificate_name_change": extract_certificate_name_change_metadata,
    "accounting_reference_date_change": extract_aa01_metadata,
}


def calculate_confidence(metadata: dict) -> float:
    """
    Simple initial confidence score based on which common fields
    were successfully extracted.
    """

    score = 0.0

    if metadata["company_number"]:
        score += 0.40

    if metadata["company_name"]:
        score += 0.25

    if metadata["form_type"] or metadata["document_type"] != "unknown":
        score += 0.20

    if metadata["extra_metadata"]:
        score += 0.10

    if metadata["filing_date"]:
        score += 0.15

    return round(min(score, 1.0), 3)


def extract_metadata(
    text: str,
    known_company_number: str | None = None,
    known_company_name: str | None = None,
) -> dict:
    cleaned_text = normalise_text(text)

    form_type = extract_form_type(cleaned_text)

    document_type = infer_document_type(
        cleaned_text,
        form_type,
    )

    extra_metadata = {}

    form_parser = FORM_PARSERS.get(form_type)

    if form_parser:
        extra_metadata.update(form_parser(cleaned_text))

    document_type_parser = DOCUMENT_TYPE_PARSERS.get(document_type)

    if document_type_parser:
        extra_metadata.update(document_type_parser(cleaned_text))

    if document_type == "accounts":
        extra_metadata.update(
            extract_accounts_metadata(cleaned_text)
        )

    company_number_candidates = extra_metadata.get("company_number_candidates")

    if not company_number_candidates:
        company_number_candidates = extract_company_number_candidates(cleaned_text)

        if company_number_candidates:
            extra_metadata["company_number_candidates"] = company_number_candidates

    if known_company_number:
        add_company_number_candidate(
            company_number_candidates,
            known_company_number,
            "pdf_metadata",
            True,
            "high",
        )
        extra_metadata["company_number_candidates"] = company_number_candidates
        normalised_known_number = normalise_company_number(known_company_number)

        if normalised_known_number:
            extra_metadata["pdf_metadata_company_number"] = normalised_known_number

    if known_company_name:
        cleaned_known_name = clean_company_name(known_company_name)

        if cleaned_known_name:
            extra_metadata["pdf_metadata_company_name"] = cleaned_known_name

    company_name = extract_company_name(cleaned_text)

    metadata_company_name = (
        extra_metadata.get("company_name_from_letter")
        or extra_metadata.get("existing_company_name")
        or extra_metadata.get("gazette_company_name")
        or extra_metadata.get("company_name_from_articles")
        or extra_metadata.get("company_name_from_form")
        or extra_metadata.get("company_name_from_legacy_block")
        or extra_metadata.get("pdf_metadata_company_name")
        or extra_metadata.get("new_company_name")
    )

    if not company_name:
        company_name = metadata_company_name

    company_number = choose_primary_company_number(company_number_candidates)

    if not company_number:
        company_number = extract_company_number(cleaned_text)

    if not company_number:
        company_number = extra_metadata.get("gazette_company_number")

    metadata = {
        "company_number": company_number,
        "company_name": company_name,
        "form_type": form_type,
        "document_type": document_type,
        "filing_date": extract_filing_date(cleaned_text),
        "extra_metadata": extra_metadata,
    }

    metadata["confidence_score"] = calculate_confidence(metadata)

    return metadata


def get_documents_to_process(conn) -> list[tuple]:
    """
    Selects documents that have extracted text but have not been parsed
    with the current parser version.
    """

    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                rd.id,
                rd.transaction_id,
                dt.extracted_text,
                rd.pdf_metadata_company_number,
                rd.pdf_metadata_company_name
            FROM raw_documents rd
            JOIN document_text dt
                ON dt.raw_document_id = rd.id
            LEFT JOIN document_metadata dm
                ON dm.raw_document_id = rd.id
            WHERE dt.extracted_text IS NOT NULL
              AND LENGTH(TRIM(dt.extracted_text)) > 0
              AND (
                    dm.id IS NULL
                    OR dm.parser_version <> %s
              )
            ORDER BY rd.id;
        """, (PARSER_VERSION,))

        return cur.fetchall()


def save_metadata(
    conn,
    raw_document_id: int,
    metadata: dict,
) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO document_metadata (
                raw_document_id,
                company_number,
                company_name,
                form_type,
                document_type,
                filing_date,
                extra_metadata,
                confidence_score,
                parser_version,
                extraction_error,
                updated_at
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                NULL,
                NOW()
            )
            ON CONFLICT (raw_document_id)
            DO UPDATE SET
                company_number = EXCLUDED.company_number,
                company_name = EXCLUDED.company_name,
                form_type = EXCLUDED.form_type,
                document_type = EXCLUDED.document_type,
                filing_date = EXCLUDED.filing_date,
                extra_metadata = EXCLUDED.extra_metadata,
                confidence_score = EXCLUDED.confidence_score,
                parser_version = EXCLUDED.parser_version,
                extraction_error = NULL,
                updated_at = NOW();
        """, (
            raw_document_id,
            metadata["company_number"],
            metadata["company_name"],
            metadata["form_type"],
            metadata["document_type"],
            metadata["filing_date"],
            Json(metadata["extra_metadata"]),
            metadata["confidence_score"],
            PARSER_VERSION,
        ))

        cur.execute("""
            UPDATE raw_documents
            SET processing_status = 'metadata_extracted',
                updated_at = NOW()
            WHERE id = %s;
        """, (raw_document_id,))

    conn.commit()


def save_extraction_failure(
    conn,
    raw_document_id: int,
    error: Exception,
) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO document_metadata (
                raw_document_id,
                extra_metadata,
                confidence_score,
                parser_version,
                extraction_error,
                updated_at
            )
            VALUES (
                %s,
                '{}',
                0,
                %s,
                %s,
                NOW()
            )
            ON CONFLICT (raw_document_id)
            DO UPDATE SET
                confidence_score = 0,
                parser_version = EXCLUDED.parser_version,
                extraction_error = EXCLUDED.extraction_error,
                updated_at = NOW();
        """, (
            raw_document_id,
            PARSER_VERSION,
            str(error)[:2000],
        ))

        cur.execute("""
            UPDATE raw_documents
            SET processing_status = 'metadata_failed',
                updated_at = NOW()
            WHERE id = %s;
        """, (raw_document_id,))

    conn.commit()


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)

    try:
        create_source_metadata_columns(conn)
        create_metadata_table(conn)

        documents = get_documents_to_process(conn)

        print(f"Found {len(documents)} documents to process")

        successful = 0
        failed = 0

        for (
            raw_document_id,
            transaction_id,
            extracted_text,
            known_company_number,
            known_company_name,
        ) in documents:
            try:
                metadata = extract_metadata(
                    extracted_text,
                    known_company_number=known_company_number,
                    known_company_name=known_company_name,
                )

                save_metadata(
                    conn=conn,
                    raw_document_id=raw_document_id,
                    metadata=metadata,
                )

                successful += 1

                print(
                    f"{transaction_id} | "
                    f"company={metadata['company_number']} | "
                    f"name={metadata['company_name']} | "
                    f"form={metadata['form_type']} | "
                    f"date={metadata['filing_date']} | "
                    f"confidence={metadata['confidence_score']}"
                )

            except Exception as error:
                conn.rollback()

                save_extraction_failure(
                    conn=conn,
                    raw_document_id=raw_document_id,
                    error=error,
                )

                failed += 1
                print(f"Failed {transaction_id}: {error}")

        print("")
        print("Metadata extraction complete")
        print(f"Successful: {successful}")
        print(f"Failed: {failed}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
