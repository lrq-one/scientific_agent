"""Live P0 development server, not a frozen evaluator."""
import os
from pathlib import Path
from evaluation.p0_development import DATABASE
if os.getenv('APP_MODE')!='evaluation' or not os.environ.get('ADMIN_DATABASE_URL','').endswith('/'+DATABASE):
    raise RuntimeError('P0 development namespace required')
if not os.environ.get('DATABASE_URL','').endswith('/'+DATABASE):
    raise RuntimeError('Scientific reads must also use the development clone')
from evaluation.frozen.telemetry import install, IdentityMiddleware
install(Path(os.environ['EVALUATION_TELEMETRY_FILE']), 'P0_DEVELOPMENT_NOT_FROZEN')
from app.main import app
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware,allow_origins=['http://127.0.0.1:5175'],allow_methods=['*'],allow_headers=['*'],allow_credentials=True)
app.add_middleware(IdentityMiddleware)
