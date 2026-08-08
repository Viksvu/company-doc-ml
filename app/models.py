from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

class UploadedFile(Base):
    __tablename__="uploaded_files" 
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    upload_batch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_document_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upload_mode: Mapped[str] = mapped_column(String, default="blind")
    selected_company_number: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_company_name: Mapped[str | None] = mapped_column(String, nullable=True)
    parse_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
