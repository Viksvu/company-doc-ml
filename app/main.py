from fastapi import FastAPI
from app.routers import uploads
from app.database import Base, engine
from app import models


Base.metadata.create_all(bind=engine)

app=FastAPI()
app.include_router(uploads.router)
