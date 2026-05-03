from fastapi import FastAPI
from app.routers import uploads

app=FastAPI()
app.include_router(uploads.router)
