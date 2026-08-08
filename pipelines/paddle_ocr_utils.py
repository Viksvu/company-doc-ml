import os
import tempfile


PADDLEOCR_LANGUAGE = "en"
PADDLE_CACHE_DIR = "/tmp/paddlex_cache"
PADDLEOCR_MAX_IMAGE_SIDE = int(
    os.getenv("PADDLEOCR_MAX_IMAGE_SIDE", "2200")
)
PADDLEOCR_TEXT_DET_LIMIT_SIDE_LEN = int(
    os.getenv("PADDLEOCR_TEXT_DET_LIMIT_SIDE_LEN", "1600")
)

os.environ.setdefault("PADDLE_PDX_CACHE_HOME", PADDLE_CACHE_DIR)
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ["FLAGS_use_mkldnn"] = "0"
os.environ["FLAGS_use_onednn"] = "0"
os.environ["FLAGS_enable_pir_api"] = "0"
os.environ["FLAGS_enable_pir_in_executor"] = "0"

_PADDLE_OCR = None


def get_paddle_ocr():
    global _PADDLE_OCR

    if _PADDLE_OCR is not None:
        return _PADDLE_OCR

    try:
        from paddleocr import PaddleOCR
    except ImportError as error:
        raise RuntimeError(
            "Missing PaddleOCR dependency. Install PaddleOCR in the pipeline "
            "environment before running OCR: "
            "pip install paddleocr paddlepaddle"
        ) from error

    constructor_options = [
        {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "enable_mkldnn": False,
            "text_det_limit_side_len": PADDLEOCR_TEXT_DET_LIMIT_SIDE_LEN,
            "text_det_limit_type": "max",
            "lang": PADDLEOCR_LANGUAGE,
        },
        {
            "use_angle_cls": True,
            "lang": PADDLEOCR_LANGUAGE,
            "show_log": False,
        },
        {
            "use_angle_cls": True,
            "lang": PADDLEOCR_LANGUAGE,
        },
        {
            "use_textline_orientation": True,
            "lang": PADDLEOCR_LANGUAGE,
        },
        {
            "lang": PADDLEOCR_LANGUAGE,
        },
    ]

    last_error = None

    for options in constructor_options:
        try:
            _PADDLE_OCR = PaddleOCR(**options)
            return _PADDLE_OCR
        except (TypeError, ValueError) as error:
            last_error = error

    raise RuntimeError(
        f"Could not initialise PaddleOCR: {last_error}"
    )


def extract_text_from_paddle_result(result) -> str:
    lines = []

    def visit(value):
        if value is None:
            return

        if isinstance(value, dict):
            text = (
                value.get("text")
                or value.get("rec_text")
                or value.get("rec_texts")
            )

            if isinstance(text, str):
                lines.append(str(text))
            elif isinstance(text, list):
                lines.extend(str(item) for item in text if item)

            for child in value.values():
                visit(child)

            return

        if isinstance(value, tuple) and len(value) >= 2:
            text_candidate = value[0]
            score_candidate = value[1]

            if isinstance(text_candidate, str) and isinstance(
                score_candidate,
                (float, int),
            ):
                lines.append(text_candidate)
                return

        if isinstance(value, list):
            if (
                len(value) >= 2
                and isinstance(value[1], tuple)
                and value[1]
                and isinstance(value[1][0], str)
            ):
                lines.append(value[1][0])
                return

            for child in value:
                visit(child)

    visit(result)

    return "\n".join(
        line.strip()
        for line in lines
        if line and line.strip()
    )


def prepare_image_for_paddle(image, max_side: int | None = None):
    max_side = max_side or PADDLEOCR_MAX_IMAGE_SIDE
    width, height = image.size
    longest_side = max(width, height)

    if longest_side <= max_side:
        return image

    scale = max_side / longest_side
    resized_size = (
        max(1, int(width * scale)),
        max(1, int(height * scale)),
    )

    return image.resize(resized_size)


def paddle_image_to_text(image, max_side: int | None = None) -> str:
    ocr = get_paddle_ocr()
    image = prepare_image_for_paddle(image, max_side=max_side)

    with tempfile.NamedTemporaryFile(suffix=".png") as image_file:
        image.save(image_file.name)

        try:
            if hasattr(ocr, "ocr"):
                result = ocr.ocr(
                    image_file.name,
                    cls=True,
                )
            elif hasattr(ocr, "predict"):
                result = ocr.predict(image_file.name)
            else:
                raise RuntimeError("PaddleOCR object has no ocr/predict method")
        except TypeError:
            result = ocr.ocr(image_file.name)

    return extract_text_from_paddle_result(result)
