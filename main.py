from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from database import Base, engine, ensure_columns
from routers.auth import router as auth_router
from routers.media import router as media_router
from routers.media_log import router as media_log_router
from routers.favorites import router as favorites_router
from routers.backlog import router as backlog_router
from routers.custom_views import router as custom_views_router
from routers.igdb import router as igdb_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: cria as tabelas no banco e aplica colunas novas em tabelas existentes
    Base.metadata.create_all(bind=engine)
    ensure_columns()
    print("Database tables created")
    print(f"Upload directory: {settings.upload_path}")
    yield
    # Shutdown


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
app.include_router(custom_views_router)
app.include_router(igdb_router)


@app.get("/", tags=["Health"])
def root():
    return {"status": "ok", "app": "Logged API", "version": "0.1.0"}


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "healthy"}
