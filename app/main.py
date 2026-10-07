from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import ENABLE_DEMO_DATA
from app.services.demo_data import ensure_demo_data


@asynccontextmanager
async def lifespan(app: FastAPI):
    if ENABLE_DEMO_DATA:
        ensure_demo_data()
    yield


app = FastAPI(title="Scientific Research Analysis Agent", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
