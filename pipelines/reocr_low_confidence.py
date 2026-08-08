import argparse
import os
from pathlib import Path

from pipeline_config import get_database_url, resolve_project_path
from paddle_ocr_utils import paddle_image_to_text


DATABASE_URL = get_database_url()

RENDER_DPI = int(os.getenv("REOCR_DPI", "350"))
REOCR_MAX_IMAGE_SIDE = int(os.getenv("REOCR_MAX_IMAGE_SIDE", "3000"))
LOW_CONFIDENCE_THRESHOLD = 0.9

CONTRAST_FACTOR = 2.2
SHARPNESS_FACTOR = 1.7
THRESHOLD_VALUE = 170

LEGACY_CROP_PROFILES = {
    "288a": {
        "company_number": (0.39, 0.195, 0.72, 0.225),
        "company_name": (0.39, 0.225, 0.92, 0.277),
        "appointment_date": (0.39, 0.303, 0.61, 0.327),
        "forenames": (0.39, 0.382, 0.92, 0.407),
        "surname": (0.39, 0.417, 0.92, 0.438),
        "footer_date": (0.08, 0.85, 0.44, 0.94),
    },
    "288b": {
        "company_number": (0.39, 0.195, 0.72, 0.225),
        "company_name": (0.39, 0.225, 0.92, 0.277),
        "termination_date": (0.39, 0.303, 0.61, 0.327),
        "forenames": (0.39, 0.382, 0.92, 0.407),
        "surname": (0.39, 0.417, 0.92, 0.438),
        "footer_date": (0.08, 0.85, 0.44, 0.94),
    },
    "288c": {
        "company_number": (0.39, 0.195, 0.72, 0.225),
        "company_name": (0.39, 0.225, 0.92, 0.277),
        "forenames": (0.39, 0.382, 0.92, 0.407),
        "surname": (0.39, 0.417, 0.92, 0.438),
        "footer_date": (0.08, 0.85, 0.44, 0.94),
    },
    "363a": {
        "company_number": (0.36, 0.155, 0.72, 0.200),
        "company_name": (0.36, 0.195, 0.92, 0.265),
        "return_date": (0.36, 0.265, 0.72, 0.315),
        "footer_date": (0.08, 0.85, 0.44, 0.94),
    },
    "395": {
        "company_number": (0.36, 0.155, 0.72, 0.200),
        "company_name": (0.36, 0.195, 0.92, 0.270),
        "charge_creation_date": (0.36, 0.285, 0.72, 0.335),
        "footer_date": (0.08, 0.85, 0.44, 0.94),
    },
    "403a": {
        "company_number": (0.36, 0.155, 0.72, 0.200),
        "company_name": (0.36, 0.195, 0.92, 0.270),
        "satisfaction_date": (0.36, 0.285, 0.72, 0.335),
        "footer_date": (0.08, 0.85, 0.44, 0.94),
    },
    "legacy_header": {
        "company_number": (0.36, 0.155, 0.72, 0.220),
        "company_name": (0.36, 0.210, 0.92, 0.300),
        "footer_date": (0.08, 0.85, 0.44, 0.94),
    },
}

PROFILE_CHOICES = ("auto", *LEGACY_CROP_PROFILES.keys())


def get_low_confidence_documents(
    conn,
    confidence_threshold: float,
    limit: int | None,
) -> list[tuple]:
    query = """
        SELECT
            rd.id,
            rd.transaction_id,
            rd.file_path,
            dm.confidence_score,
            dm.document_type
        FROM raw_documents rd
        LEFT JOIN document_text dt
            ON dt.raw_document_id = rd.id
        LEFT JOIN document_metadata dm
            ON dm.raw_document_id = rd.id
        WHERE rd.detected_file_type = 'pdf'
          AND rd.file_path IS NOT NULL
          AND (
                rd.processing_status = 'failed_ocr'
                OR (
                    dt.raw_document_id IS NOT NULL
                    AND COALESCE(dm.confidence_score, 0) < %s
                )
              )
        ORDER BY
            CASE WHEN rd.processing_status = 'failed_ocr' THEN 0 ELSE 1 END,
            dm.confidence_score ASC NULLS FIRST,
            rd.id
    """

    params: list = [confidence_threshold]

    if limit:
        query += "\nLIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def render_page_to_image(page, dpi: int = RENDER_DPI):
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "Missing Pillow dependency. Install Pillow in the pipeline "
            "environment before running re-OCR."
        ) from error

    pixmap = page.get_pixmap(
        dpi=dpi,
        alpha=False,
    )

    mode = "RGB" if pixmap.n < 4 else "RGBA"

    return Image.frombytes(
        mode,
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )


def preprocess_image(image):
    try:
        from PIL import ImageEnhance, ImageFilter, ImageOps
    except ImportError as error:
        raise RuntimeError(
            "Missing Pillow dependency. Install Pillow in the pipeline "
            "environment before running re-OCR."
        ) from error

    image = image.convert("L")
    image = ImageOps.autocontrast(image)
    image = ImageEnhance.Contrast(image).enhance(CONTRAST_FACTOR)
    image = ImageEnhance.Sharpness(image).enhance(SHARPNESS_FACTOR)
    image = image.filter(ImageFilter.MedianFilter(size=3))

    return image.point(
        lambda pixel: 255 if pixel > THRESHOLD_VALUE else 0,
        mode="1",
    )


def preprocess_crop_image(image):
    try:
        from PIL import ImageEnhance, ImageFilter, ImageOps
    except ImportError as error:
        raise RuntimeError(
            "Missing Pillow dependency. Install Pillow in the pipeline "
            "environment before running re-OCR."
        ) from error

    image = image.convert("L")
    image = ImageOps.autocontrast(image)
    image = ImageEnhance.Contrast(image).enhance(CONTRAST_FACTOR)
    image = ImageEnhance.Sharpness(image).enhance(SHARPNESS_FACTOR)
    image = image.filter(ImageFilter.MedianFilter(size=3))

    return image.resize(
        (image.width * 2, image.height * 2),
    )


def crop_relative(image, box):
    width, height = image.size
    left, top, right, bottom = box

    return image.crop((
        int(width * left),
        int(height * top),
        int(width * right),
        int(height * bottom),
    ))


def select_crop_profile(page_text: str, requested_profile: str) -> str:
    if requested_profile != "auto":
        return requested_profile

    lowered = page_text.lower()

    if "appointment of director or secretary" in lowered or "form 288a" in lowered:
        return "288a"

    if "termination of appointment" in lowered or "form 288b" in lowered:
        return "288b"

    if "change of particulars" in lowered or "form 288c" in lowered:
        return "288c"

    if "annual return" in lowered or "363a" in lowered:
        return "363a"

    if "particulars of a charge" in lowered or "form no. 395" in lowered:
        return "395"

    if (
        "declaration of satisfaction" in lowered
        or "form no. 403a" in lowered
        or "403a" in lowered
    ):
        return "403a"

    return "legacy_header"


def extract_legacy_field_crops(
    image,
    page_number: int,
    page_text: str,
    requested_profile: str,
    max_side: int,
) -> str:
    if page_number != 1:
        return ""

    profile_name = select_crop_profile(page_text, requested_profile)
    profile = LEGACY_CROP_PROFILES[profile_name]
    field_text = [
        f"legacy_crop_profile: {profile_name}",
    ]

    for field_name, box in profile.items():
        crop = crop_relative(image, box)
        processed_crop = preprocess_crop_image(crop)

        value = paddle_image_to_text(
            processed_crop,
            max_side=max_side,
        )

        value = " ".join(value.split())

        if value:
            field_text.append(f"legacy_crop_{field_name}: {value}")

    if len(field_text) == 1:
        return ""

    return "\n--- Legacy field crops page 1 ---\n" + "\n".join(field_text)


def reocr_pdf(
    file_path: str,
    crop_profile: str = "auto",
    dpi: int = RENDER_DPI,
    max_pages: int | None = None,
    max_image_side: int = REOCR_MAX_IMAGE_SIDE,
) -> dict:
    try:
        import fitz
    except ImportError as error:
        raise RuntimeError(
            "Missing PyMuPDF dependency. Install project requirements before "
            "running re-OCR: pip install -r ../requirements.txt"
        ) from error

    path = resolve_project_path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"File does not exist: {path}")

    page_text_parts = []

    with fitz.open(path) as doc:
        page_count = doc.page_count

        for page_number, page in enumerate(doc, start=1):
            if max_pages and page_number > max_pages:
                page_text_parts.append(
                    f"--- Page {page_number} ---\n"
                    "[Re-OCR skipped by --max-pages]"
                )
                continue

            image = render_page_to_image(page, dpi=dpi)
            processed_image = preprocess_image(image)
            print(
                f"Re-OCR page {page_number}/{page_count} with PaddleOCR "
                f"({image.width}x{image.height}, dpi={dpi})",
                flush=True,
            )
            page_text = paddle_image_to_text(
                processed_image,
                max_side=max_image_side,
            )
            page_text = (
                page_text
                + extract_legacy_field_crops(
                    image,
                    page_number,
                    page_text,
                    crop_profile,
                    max_image_side,
                )
            ).strip()

            page_text_parts.append(
                f"--- Page {page_number} ---\n{page_text}"
            )

    extracted_text = "\n\n".join(page_text_parts).strip()

    if not extracted_text:
        raise ValueError("Preprocessed OCR produced no text")

    return {
        "extracted_text": extracted_text,
        "page_count": page_count,
        "ocr_page_count": min(page_count, max_pages or page_count),
        "extraction_method": "paddle_ocr_preprocessed",
    }


def save_reocr_result(
    conn,
    document_id: int,
    result: dict,
    delete_metadata: bool,
) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE document_text
            SET extracted_text = %s,
                extraction_method = %s,
                ocr_page_count = %s,
                updated_at = NOW()
            WHERE raw_document_id = %s;
        """, (
            result["extracted_text"],
            result["extraction_method"],
            result["ocr_page_count"],
            document_id,
        ))

        cur.execute("""
            UPDATE raw_documents
            SET processing_status = 'ready_for_parsing',
                page_count = %s,
                error_message = NULL,
                updated_at = NOW()
            WHERE id = %s;
        """, (
            result["page_count"],
            document_id,
        ))

        if delete_metadata:
            cur.execute("""
                DELETE FROM document_metadata
                WHERE raw_document_id = %s;
            """, (document_id,))

    conn.commit()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Re-OCR low-confidence PDF documents using high-DPI rendering "
            "and image preprocessing."
        ),
    )

    parser.add_argument(
        "--threshold",
        type=float,
        default=LOW_CONFIDENCE_THRESHOLD,
        help="Re-OCR documents below this confidence score.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of documents to process.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching documents without writing updated text.",
    )
    parser.add_argument(
        "--keep-metadata",
        action="store_true",
        help="Do not delete existing metadata rows after replacing text.",
    )
    parser.add_argument(
        "--crop-profile",
        choices=PROFILE_CHOICES,
        default="auto",
        help=(
            "Legacy field crop profile to use. Defaults to auto-detection "
            "from the OCR text."
        ),
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=RENDER_DPI,
        help=(
            "Render DPI for re-OCR. Use 300-400 for legacy scanned forms. "
            f"Default: {RENDER_DPI}."
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help=(
            "Only re-OCR the first N pages. By default every page is re-OCRed."
        ),
    )
    parser.add_argument(
        "--max-image-side",
        type=int,
        default=REOCR_MAX_IMAGE_SIDE,
        help=(
            "Largest image side sent into PaddleOCR after rendering. "
            f"Default: {REOCR_MAX_IMAGE_SIDE}."
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        import psycopg2
    except ImportError as error:
        raise RuntimeError(
            "Missing psycopg2 dependency. Install project requirements before "
            "running re-OCR: pip install -r ../requirements.txt"
        ) from error

    conn = psycopg2.connect(DATABASE_URL)

    try:
        documents = get_low_confidence_documents(
            conn=conn,
            confidence_threshold=args.threshold,
            limit=args.limit,
        )

        print(
            f"Found {len(documents)} low-confidence PDF documents "
            f"below {args.threshold}"
        )

        successful = 0
        failed = 0

        for (
            document_id,
            transaction_id,
            file_path,
            confidence_score,
            document_type,
        ) in documents:
            print(
                f"Re-OCR: {transaction_id} | "
                f"confidence={confidence_score} | "
                f"type={document_type}"
            )

            if args.dry_run:
                continue

            try:
                result = reocr_pdf(
                    file_path,
                    crop_profile=args.crop_profile,
                    dpi=args.dpi,
                    max_pages=args.max_pages,
                    max_image_side=args.max_image_side,
                )

                save_reocr_result(
                    conn=conn,
                    document_id=document_id,
                    result=result,
                    delete_metadata=not args.keep_metadata,
                )

                successful += 1

                print(
                    f"{transaction_id} -> reocr_complete | "
                    f"pages={result['page_count']} | "
                    f"method={result['extraction_method']}"
                )

            except Exception as error:
                conn.rollback()
                failed += 1
                print(f"{transaction_id} -> reocr_failed | {error}")

        print("")
        print("Low-confidence re-OCR complete")
        print(f"Successful: {successful}")
        print(f"Failed: {failed}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
