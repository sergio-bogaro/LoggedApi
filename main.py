from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from config import settings
from database import (
    Base,
    engine,
    ensure_columns,
    ensure_indexes,
    drop_obsolete_tables,
    validate_schema,
)
from routers.auth import router as auth_router
from routers.media import router as media_router
from routers.media_log import router as media_log_router
from routers.favorites import router as favorites_router
from routers.backlog import router as backlog_router
from routers.igdb import router as igdb_router
from routers.import_router import router as import_router
from routers.suwayomi import router as suwayomi_router
from routers.library_sync import router as library_sync_router
from routers.tmdb import router as tmdb_router
from services.import_jobs.worker import worker as import_job_worker
from services.library_sync.scheduler import scheduler as library_sync_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: cria as tabelas no banco e aplica colunas novas em tabelas existentes
    Base.metadata.create_all(bind=engine)
    ensure_columns()
    validate_schema()
    ensure_indexes()
    drop_obsolete_tables()
    print("Database tables created")
    print(f"Upload directory: {settings.upload_path}")
    await library_sync_scheduler.start()
    await import_job_worker.start()
    yield
    # Shutdown
    await import_job_worker.stop()
    await library_sync_scheduler.stop()


app = FastAPI(
    title="Logged API",
    description="API para tracking de mídias — filmes, animes, mangás, jogos e livros.",
    version="0.1.0",
    lifespan=lifespan,
)

# Erros de validação de domínio (upload inválido, jogo/credenciais IGDB etc.)
# chegam como ValueError; sem isso virariam 500.
@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    # Evita 500 quando um cliente envia um valor que viola uma constraint.
    return JSONResponse(
        status_code=409,
        content={"detail": "Não foi possível salvar os dados: valor duplicado ou inválido."},
    )


# CORS — permite o frontend React acessar a API
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir imagens estáticas da pasta uploads
app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

# Registrar routers
app.include_router(auth_router)
app.include_router(media_router)
app.include_router(media_log_router)
app.include_router(favorites_router)
app.include_router(backlog_router)
app.include_router(igdb_router)
app.include_router(import_router)
app.include_router(tmdb_router)
app.include_router(suwayomi_router)
app.include_router(library_sync_router)


@app.get("/", tags=["Health"])
def root():
    return {"status": "ok", "app": "Logged API", "version": "0.1.0"}


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "healthy"}
