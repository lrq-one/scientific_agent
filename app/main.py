from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import ENABLE_DEMO_DATA
from app.services.demo_data import ensure_demo_data
from app.services.conversation_history import conversation_repository


@asynccontextmanager
async def lifespan(app: FastAPI):
    if ENABLE_DEMO_DATA:
        ensure_demo_data()
    repository = conversation_repository()
    if repository is not None:
        app.state.startup_recovery = repository.reconcile_orphaned_tasks()
    else:
        app.state.startup_recovery = {"failed_running": 0, "cancelled_cancelling": 0}
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
