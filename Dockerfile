FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Diretório dos dados persistidos (banco + uploads).
# O volume do compose é montado em /data; criamos a subpasta de uploads aqui
# para o StaticFiles do FastAPI não falhar caso o volume esteja vazio.
RUN mkdir -p /data/uploads

EXPOSE 8000

CMD ["sh", "-c", "mkdir -p /data/uploads && uvicorn main:app --host 0.0.0.0 --port 8000"]
