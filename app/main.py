from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routers import uploads
from app.database import Base, engine
from app import models
from app.routers import pages
Base.metadata.create_all(bind=engine)

app=FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(uploads.router)
app.include_router(pages.router)

