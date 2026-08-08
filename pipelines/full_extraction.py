import download_files
import extract_pdf_text
import extract_html_text
import extract_metadata
import inspect_pdfs
import ocr_documents


def main():
    download_files.main()
    inspect_pdfs.main()
    ocr_documents.main()
    extract_pdf_text.main()
    extract_html_text.main()
    extract_metadata.main()


if __name__ == "__main__":
    main()