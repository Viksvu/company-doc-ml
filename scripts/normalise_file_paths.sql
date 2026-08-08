-- Converts old machine-specific absolute paths to project-relative paths.
-- Run after restoring a dump on another laptop if raw_documents.file_path
-- contains paths like /home/<user>/.../company-app/data/raw/...

UPDATE raw_documents
SET file_path = regexp_replace(file_path, '^.*/company-app/', '')
WHERE file_path LIKE '%/company-app/data/raw/%';

UPDATE uploaded_files
SET file_path = regexp_replace(file_path, '^.*/company-app/', '')
WHERE file_path LIKE '%/company-app/data/raw/%';
