FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Diretório dos dados persistidos (banco + uploads + backups).
# O volume do compose é montado em /data; criamos as subpastas aqui para o
# StaticFiles do FastAPI e o backup não falharem caso o volume esteja vazio.
RUN mkdir -p /data/uploads /data/backups && chmod +x scripts/entrypoint.sh

EXPOSE 8000

# Backup -> Alembic -> uvicorn (ver scripts/entrypoint.sh).
CMD ["scripts/entrypoint.sh"]
