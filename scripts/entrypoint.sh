#!/bin/sh
# Entrypoint da API: backup -> migrações -> servidor.
# O backup antes do Alembic é a rede de segurança de qualquer mudança de schema.
set -e

UPLOAD_DIR="${UPLOAD_DIR:-/data/uploads}"
BACKUP_DIR="${BACKUP_DIR:-/data/backups}"

# Garante que os defaults acima cheguem ao processo Python (o shell não exporta
# automaticamente variáveis atribuídas no script).
export UPLOAD_DIR BACKUP_DIR

mkdir -p "$UPLOAD_DIR" "$BACKUP_DIR"

if [ "${BACKUP_ON_STARTUP:-true}" = "true" ]; then
  echo "[entrypoint] Backup pré-migração"
  python scripts/backup.py --on-startup
else
  echo "[entrypoint] Backup de startup desabilitado (BACKUP_ON_STARTUP=false)"
fi

echo "[entrypoint] Aplicando migrações"
python scripts/check_schema_state.py
alembic upgrade head

echo "[entrypoint] Iniciando API"
exec uvicorn main:app --host 0.0.0.0 --port 8000
