from fastapi import APIRouter, File, UploadFile, Depends
from pathlib import Path
from shutil import copyfileobj
from uuid import uuid4
from app.models import UploadedFile
from app.database import get_db
from sqlalchemy.orm import Session

UPLOAD_DIR = Path("uploaded_file")
UPLOAD_DIR.mkdir(exist_ok=True)
router= APIRouter(
    prefix="/uploads",
    tags=["uploads"],
)

@router.post("/")
def upload_file(
    file:UploadFile=File(...),
    db:Session = Depends(get_db),):
    suffix= Path(file.filename).suffix
    uniquename= f"{uuid4()}{suffix}" 
    file_path= UPLOAD_DIR / uniquename
    with  file_path.open("wb") as buffer:
        copyfileobj(file.file, buffer)
    
    uploaded_file=UploadedFile(
        filename=file.filename,
        content_type=file.content_type,
        file_path=str(file_path),
    )

    db.add(uploaded_file)
    db.commit()
    db.refresh(uploaded_file)
    return {
        "id": uploaded_file.id,
        "filename": uploaded_file.filename,
        "content_type": uploaded_file.content_type,
        "file_path": uploaded_file.file_path,
        "uploaded_at": uploaded_file.uploaded_at,
    }