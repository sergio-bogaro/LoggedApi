# Logged API

Backend do [Logged](https://github.com/sergio-bogaro/Logged), um tracker pessoal de filmes,
séries, animes, mangás, jogos, livros e músicas.

A API é responsável por:

- biblioteca, logs de progresso, favoritos e backlog;
- importação de históricos e sincronização com serviços externos;
- fila de revisão de importações e sugestões de sync;
- upload e entrega das imagens de capa;
- proxy das APIs do TMDB e do IGDB para manter as chaves no servidor.

## Stack

| Tecnologia | Uso |
|---|---|
| Python 3.12 | Runtime |
| FastAPI + Uvicorn | API HTTP |
| SQLAlchemy 2 + SQLite | Persistência |
| Alembic | Migrações de schema |
| Pydantic v2 | Schemas e configuração |

## Requisitos

Para rodar **sem Docker**:

- Python **3.12** com `venv` e `pip`;
- Git.

Para rodar **com Docker**, basta Docker com Compose v2. Não é necessário instalar Python.
O frontend é um projeto separado; veja o
[README do LoggedApp](https://github.com/sergio-bogaro/LoggedApp/blob/master/README.md).

## Rodar com Docker (recomendado)

Os comandos são executados na raiz do repositório `Logged`.

**Build local (desenvolvimento):**

```bash
docker compose up -d --build
```

**Imagens publicadas (deploy):**

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Esses comandos usam o compose do repositório principal. Se você clonou apenas a API, siga
a seção [Rodar localmente](#rodar-localmente-sem-docker) ou construa a imagem diretamente
com `docker build -t logged-api .`.

No build local, a API fica exposta em **http://localhost:8000**. No deploy, apenas o
frontend é exposto (porta 5173 por padrão) e a API é acessada pelo proxy do Nginx na mesma
porta do frontend (`/docs`).

O container da API executa, nesta ordem:

1. backup do banco e dos uploads;
2. verificação do estado do schema;
3. `alembic upgrade head`;
4. Uvicorn em `0.0.0.0:8000`.

Se o backup falhar, o startup é interrompido antes das migrações. Os dados ficam no volume
`logged_data`, montado em `/data`:

```text
/data/logged.db      banco SQLite
/data/uploads/       imagens
/data/backups/       backups automáticos
```

Comandos úteis:

```bash
docker compose logs -f api
docker compose exec api alembic current
docker compose exec api python scripts/backup.py
docker compose restart api
```

> Em stacks com o compose de deploy, acrescente
> `-f deploy/docker-compose.yml` a cada comando ou execute dentro da pasta `deploy/`.

## Rodar localmente (sem Docker)

### 1. Prepare o Python 3.12

Confirme a versão:

```bash
python --version
```

Se o sistema tiver Python 3.13/3.14, use uma instalação 3.12. No Linux/macOS, o `pyenv` é
uma opção:

```bash
pyenv install 3.12.12
pyenv local 3.12.12
```

No Windows, instale o Python 3.12 e use `py -3.12`. Para listar as versões disponíveis:

```powershell
py -0p
```

### 2. Crie o ambiente virtual e instale as dependências

**Linux / macOS:**

```bash
cd LoggedApi
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

No **Fish**, a ativação é:

```fish
source .venv/bin/activate.fish
```

**Windows (PowerShell):**

```powershell
cd LoggedApi
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Se o PowerShell bloquear a ativação:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### 3. Configure o `.env`

O arquivo é opcional para o funcionamento básico, mas é útil para personalizar o ambiente:

**Linux / macOS:**

```bash
cp .env.example .env
```

**Windows:**

```powershell
Copy-Item .env.example .env
```

O exemplo já funciona para desenvolvimento local. Ajuste ou deixe em branco as chaves de
integração que não for usar (o `.env.example` traz placeholders para o IGDB). As variáveis
mais importantes estão em [Configuração](#configuração).

### 4. Aplique as migrações e suba a API

```bash
alembic upgrade head
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

A API estará em:

- **http://localhost:8000** — raiz;
- **http://localhost:8000/health** — health check;
- **http://localhost:8000/docs** — Swagger UI;
- **http://localhost:8000/redoc** — ReDoc.

### 5. Rode o frontend

Em outro terminal:

```bash
cd LoggedApp
npm ci
npm run dev
```

O frontend usa `http://localhost:8000` por padrão. O CORS padrão do backend já libera
`http://localhost:5173`.

### Erro de build do `pydantic-core`

Se aparecer uma mensagem como:

```text
the configured Python interpreter version (3.14) is newer than PyO3's maximum supported version
```

o virtualenv foi criado com uma versão de Python incompatível. Recrie o `.venv` com Python
3.12 e reinstale as dependências. Evite forçar `PYO3_USE_ABI3_FORWARD_COMPATIBILITY`; use a
versão suportada.

## Testes

Com o virtualenv ativado e a partir de `LoggedApi`:

```bash
python -m unittest discover -s tests -v
```

Os testes criam um banco temporário a partir do metadata dos models, então não é preciso
subir a API nem aplicar migrações antes. A imagem do container não inclui a pasta `tests/`
(veja o `.dockerignore`), então rode os testes localmente, fora do container.

## Migrações (Alembic)

O schema **não é criado nem alterado no startup**. Quem aplica as mudanças é o Alembic.

```bash
alembic upgrade head    # aplica migrações pendentes
alembic current         # mostra a revisão atual
alembic check           # compara os models com o banco
```

Para criar uma nova revisão depois de alterar os models:

```bash
alembic revision --autogenerate -m "descrição da mudança"
alembic upgrade head
```

### Bancos antigos (antes do Alembic)

Se o banco já existe mas não tem a tabela `alembic_version`, marque a baseline **uma única
vez** antes de aplicar as migrações:

```bash
alembic stamp 0001_baseline
alembic upgrade head
```

No container:

```bash
docker compose stop api
docker compose run --rm api alembic stamp 0001_baseline
docker compose up -d api
```

Nunca rode `alembic upgrade head` em um banco antigo sem o `stamp`: o Alembic tentaria criar
tabelas que já existem. O entrypoint detecta esse caso com `scripts/check_schema_state.py`.

## Configuração

As configurações são lidas de variáveis de ambiente e do arquivo `.env` na pasta
`LoggedApi` (via `pydantic-settings`). No Docker, o compose sobrescreve os caminhos para
`/data`.

### Aplicação

| Variável | Padrão | Descrição |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./logged.db` | URL do banco SQLite |
| `UPLOAD_DIR` | `./uploads` | Pasta pública das imagens |
| `IMPORT_DIR` | `./imports` | Pasta temporária das importações (fora de `/uploads`) |
| `CORS_ORIGINS` | `["http://localhost:5173","http://localhost:3000"]` | Origens permitidas, em JSON |
| `LOG_LEVEL` | `INFO` | Nível de log (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `MAX_UPLOAD_MB` | `10` | Limite de upload de imagens |
| `ALLOW_PRIVATE_IMAGE_URLS` | `false` | Permite baixar capas de hosts privados/loopback |

### Integrações

| Variável | Padrão | Descrição |
|---|---|---|
| `TMDB_API_KEY` | — | Fallback de instância para filmes |
| `IGDB_CLIENT_ID` | — | Fallback de instância para jogos |
| `IGDB_CLIENT_SECRET` | — | Fallback de instância para jogos |
| `TRAKT_CLIENT_ID` / `TRAKT_CLIENT_SECRET` | — | App OAuth do Trakt |
| `SIMKL_CLIENT_ID` / `SIMKL_CLIENT_SECRET` | — | App OAuth do Simkl |
| `MAL_CLIENT_ID` | — | Client ID do MyAnimeList |
| `TMDB_REQUEST_TIMEOUT` | `15` | Timeout das chamadas ao TMDB (segundos) |
| `IGDB_REQUEST_TIMEOUT` | `15` | Timeout das chamadas ao IGDB (segundos) |
| `SUWAYOMI_REQUEST_TIMEOUT` | `15` | Timeout das chamadas ao Suwayomi (segundos) |
| `LIBRARY_SYNC_REQUEST_TIMEOUT` | `20` | Timeout dos provedores de sincronização (segundos) |

As chaves também podem ser cadastradas **por usuário** dentro do app. O valor do usuário tem
prioridade sobre o fallback da instância. Elas ficam salvas em texto plano no SQLite;
proteja o acesso ao servidor e aos backups.

### Backup

| Variável | Padrão | Descrição |
|---|---|---|
| `BACKUP_DIR` | `./backups` | Pasta dos backups (Docker: `/data/backups`) |
| `BACKUP_ON_STARTUP` | `true` | Faz backup antes das migrações no startup |
| `BACKUP_INCLUDE_UPLOADS` | `true` | Inclui as imagens no pacote de backup |
| `BACKUP_KEEP_DAILY` | `7` | Dias de retenção diária |
| `BACKUP_KEEP_WEEKLY` | `4` | Semanas de retenção semanal |
| `BACKUP_KEEP_MONTHLY` | `3` | Meses de retenção mensal |

### Workers

| Variável | Padrão | Descrição |
|---|---|---|
| `IMPORT_JOB_WORKER_ENABLED` | `true` | Processa jobs de importação em background |
| `LIBRARY_SYNC_SCHEDULER_ENABLED` | `true` | Executa o sync agendado |

### Exemplo mínimo

```env
DATABASE_URL=sqlite:///./logged.db
UPLOAD_DIR=./uploads
CORS_ORIGINS=["http://localhost:5173","http://localhost:3000"]
```

Após alterar variáveis, reinicie a API local ou recrie o container com
`docker compose up -d --force-recreate api`.

## Endpoints principais

| Prefixo | Função |
|---|---|
| `/auth` | Registro, login e usuários |
| `/api/media` e `/api/media-logs` | Biblioteca, títulos e logs de progresso |
| `/api/favorites` e `/api/backlog` | Favoritos e backlog |
| `/api/import` | Importação por arquivo das fontes sem integração |
| `/api/review` | Fila unificada de revisão de importações e sugestões de sync |
| `/api/integrations` | Conexões e sincronização com serviços externos |
| `/api/tmdb` e `/api/igdb` | Proxy para as APIs externas (chaves ficam no servidor) |
| `/api/suwayomi` | Integração com Suwayomi-Server |
| `/uploads/*` | Imagens enviadas |
| `/health`, `/docs`, `/redoc` | Health check e documentação |

Observações:

- As respostas JSON usam **camelCase**; as requisições aceitam camelCase e snake_case.
- `GET /api/media/` e `GET /api/media-logs/` são paginados: `limit` padrão 200, máximo 500,
  e `offset`.
- Erros de regra de negócio retornam `400 {"detail": "..."}`. Erros inesperados retornam
  `500` e ficam registrados no log com traceback.
- A autenticação é intencionalmente simples, para uso pessoal: login por usuário/senha e
  `user_id` nas requisições, sem JWT. **Não exponha a porta 8000 diretamente na internet**;
  use o proxy do Nginx na porta 5173.

### Uploads de imagens

- Formatos: `.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`.
- Tamanho máximo: 10 MB (ajustável com `MAX_UPLOAD_MB`; o `client_max_body_size` do Nginx
  também precisa ser ajustado).
- O conteúdo é validado por magic bytes, não apenas pela extensão.
- Downloads por URL bloqueiam loopback e redes privadas, a menos que
  `ALLOW_PRIVATE_IMAGE_URLS=true`.

## Importação, sincronização e revisão

A página **Importar** do app tem duas abas:

- **Importar** — aceita os arquivos de exportação das fontes que ainda não têm sync
  completo: Letterboxd, Goodreads, IMDb, Grouvee e Infinite Backlog. O processamento roda
  em background e o job fica salvo.
- **Revisão** — fila única para jobs manuais e sugestões do sync, com filtros por estado e
  qualidade do match. A API unificada fica em `/api/review`.

Fontes com sync completo (AniList, MyAnimeList, Trakt, Kitsu, Simkl, Hardcover e Open
Library) e o Suwayomi são configuradas em **Configurações → Integrações** e não aparecem
na importação manual. O Letterboxd continua na importação porque o sync é parcial: o feed
traz só os diários recentes, enquanto o ZIP exporta o histórico completo e a watchlist.

O sync pode ser agendado em **Configurações → Integrações**. Ele nunca cria mídia sozinho:
títulos novos viram sugestões na fila de revisão. Ao aprovar, mídia, logs, backlog e vínculo
de sync são criados juntos.

## Backup e restauração

O script usa `VACUUM INTO` para gerar um snapshot consistente do SQLite, verifica a
integridade, compacta o arquivo e aplica retenção GFS:

```bash
python scripts/backup.py               # backup + poda
python scripts/backup.py --dry-run     # mostra o que faria
python scripts/backup.py --keep-monthly 12
```

Retenção por janelas de período (não por quantidade de arquivos):

- `BACKUP_KEEP_DAILY=7` — todos os backups dos últimos 7 dias;
- `BACKUP_KEEP_WEEKLY=4` — o mais recente de cada uma das últimas 4 semanas;
- `BACKUP_KEEP_MONTHLY=3` — o mais recente de cada um dos últimos 3 meses.

Arquivos fora do padrão `logged-AAAA-MM-DD.db.gz` / `uploads-AAAA-MM-DD.tar.gz` nunca são
apagados.

### Restauração com bind mount

> Nos comandos abaixo, acrescente `-f deploy/docker-compose.yml` se estiver usando o compose
> de deploy.

Se os dados estão visíveis no host, considere `/caminho` como a pasta montada em `/data`:

```bash
docker compose stop api

# guarde o banco atual
mv /caminho/logged.db /caminho/logged.db.bak-$(date +%F)
rm -f /caminho/logged.db-wal /caminho/logged.db-shm

# restaure o banco e, se existir, os uploads
gunzip -c /caminho/backups/logged-AAAA-MM-DD.db.gz > /caminho/logged.db
tar -xzf /caminho/backups/uploads-AAAA-MM-DD.tar.gz -C /caminho

docker compose up -d api
```

O `tar` contém a pasta `uploads/`, por isso a extração é feita na pasta de dados (a que é
montada em `/data`), e não dentro de `uploads/`.

### Restauração com volume Docker

Com o volume padrão, use um container auxiliar:

```bash
docker compose stop api

# guarde o banco atual
docker compose run --rm --no-deps api sh -c \
  'cp /data/logged.db /data/logged.db.bak-$(date +%F)'

# restaure o banco
docker compose run --rm --no-deps api sh -c \
  'rm -f /data/logged.db-wal /data/logged.db-shm && \
   gunzip -c /data/backups/logged-AAAA-MM-DD.db.gz > /data/logged.db'

# se o backup incluir uploads
docker compose run --rm --no-deps api sh -c \
  'tar -xzf /data/backups/uploads-AAAA-MM-DD.tar.gz -C /data'

docker compose up -d api
```

O startup aplica as migrações pendentes. Se o backup for anterior ao Alembic (sem tabela
`alembic_version`), marque a baseline uma única vez:

```bash
docker compose run --rm api alembic stamp 0001_baseline
docker compose up -d api
```

### Backup diário

O backup já roda no startup. Para uma rotina diária, agende:

```bash
docker compose exec api python scripts/backup.py
```

Use cron ou um timer do systemd no host. Guarde ao menos uma cópia fora do servidor: backup
no mesmo disco não protege contra falha de hardware.

## Estrutura

```text
LoggedApi/
├── main.py              # app FastAPI e registro dos routers
├── config.py            # configuração via pydantic-settings
├── routers/             # camada HTTP
├── services/            # regras de negócio
├── models/              # models SQLAlchemy
├── schemas/             # schemas Pydantic
├── migrations/          # revisões Alembic
├── scripts/             # entrypoint, backup e checagem de schema
└── tests/               # testes de regressão (unittest)
```

A arquitetura é em camadas: **router → service → model/schema**. Routers não acessam o banco
diretamente.

## Problemas comuns

| Sintoma | Causa / solução |
|---|---|
| `pydantic-core` falha ao compilar | O venv está usando Python 3.13/3.14. Recrie com Python 3.12 |
| `Unknown command: pip` no Fish | Ative o venv com `source .venv/bin/activate.fish` ou use `.venv/bin/python -m pip` |
| `ModuleNotFoundError` ao rodar a API | O venv não está ativado ou as dependências não foram instaladas |
| `alembic: command not found` | Ative o venv ou use `python -m alembic`; no container, `docker compose exec api alembic` |
| Banco antigo dá erro de tabela existente | Marque `alembic stamp 0001_baseline` antes de `upgrade head` |
| Frontend não acessa a API | Verifique se a API está em `:8000` e se a origem está em `CORS_ORIGINS` |
| Imagens retornam 404 | Confira `UPLOAD_DIR` e se o volume está montado em `/data` |
| Busca de filmes/jogos falha | Configure as chaves em **Integrações** ou no `.env` e veja os logs |
| API não sobe após reiniciar | O backup pode ter falhado. Veja `docker compose logs api` |
| Testes não encontram tabelas | Rode a partir de `LoggedApi`; os testes criam o schema temporário |
