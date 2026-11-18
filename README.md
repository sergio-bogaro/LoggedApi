# Logged API — Instruções para rodar (Arch Linux / Fish)

> Guia rápido com passos para reproduzir o ambiente, resolver problemas com `pydantic-core` (PyO3) e executar a API.

## Comando rápido para rodar a API

Com o ambiente virtual ativado, execute:

```bash
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

O servidor estará disponível em http://localhost:8000

## Requisitos
- Sistema: Arch Linux
- Shell: Fish (instruções de ativação de venv incluem `activate.fish`)
- Ferramentas: `git`, `curl`, compiladores (`base-devel`), `python` (usaremos `pyenv` para instalar Python 3.12.12)

## Resumo da solução
O projeto precisa de uma versão do Python compatível com PyO3 usado pelo `pydantic-core`. Se o sistema tem Python 3.14, o build nativo pode falhar. Use Python 3.11/3.12 (por exemplo 3.12.12) e recrie o virtualenv.

## Passo a passo (recomendado: usar `pyenv`)

1. Instale dependências do sistema (compile/build):

```bash
sudo pacman -Syu --needed base-devel openssl zlib xz libffi sqlite bzip2 tk git curl
```

2. Instale `pyenv`:

```bash
curl https://pyenv.run | bash
```

Adicione inicialização do `pyenv` ao Fish (coloque no `~/.config/fish/config.fish`):

```fish
set -x PYENV_ROOT $HOME/.pyenv
set -x PATH $PYENV_ROOT/bin $PATH
status --is-interactive; and pyenv init --path | source
status --is-interactive; and pyenv init - | source
```

Depois abra um novo terminal ou recarregue o `config.fish`:

```bash
source ~/.config/fish/config.fish
```

3. Instale Python 3.12.12 com `pyenv` e defina localmente:

```bash
pyenv install 3.12.12
pyenv local 3.12.12
```

4. Recrie e ative o virtualenv no diretório do projeto (`LoggedApi`):

```bash
cd /home/serjo/Documentos/git/Logged/LoggedApi
rm -rf .venv
python -m venv .venv
source .venv/bin/activate.fish
```

5. Atualize `pip` e instale dependências:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Se ocorrer erro de build do `pydantic-core` relacionado ao PyO3 detectando Python 3.14, é sinal que o venv ainda está usando Python 3.14 — confirme com:

```bash
python --version
.venv/bin/python --version
```

Se estiver em 3.14, recrie o venv usando o `python` provido pelo `pyenv` (veja `pyenv local` acima) e repita os passos.

6. Rodar a API:

```bash
# com venv ativado
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

7. Testar endpoints:

```bash
# Health
curl http://localhost:8000/health
# Root
curl http://localhost:8000/
```

## Alternativa rápida (não recomendada)
Forçar a compatibilidade ABI quando não puder trocar de Python (pode causar problemas):

```bash
env PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 python -m pip install -r requirements.txt
```

## Problemas comuns e soluções
- Erro: `the configured Python interpreter version (3.14) is newer than PyO3's maximum supported version (3.13)` → Solução: usar Python 3.11/3.12 via `pyenv` ou sistema.
- Erro: `Unknown command: pip` no Fish → certifique-se de ativar o venv com `source .venv/bin/activate.fish` ou usar `.venv/bin/python -m pip`.
- Arquivo estranho em `.venv/bin` (ex: `𝜋thon`) — remova se for indesejado:

```bash
ls -la .venv/bin
rm .venv/bin/𝜋thon
```

## Variáveis de ambiente
O projeto lê `.env` (via `pydantic-settings`). Variáveis úteis:
- `DATABASE_URL` (ex: `sqlite:///./logged.db`)
- `UPLOAD_DIR` (ex: `./uploads`)
- `IMPORT_DIR` (opcional, ex: `./imports`) — arquivos temporários das importações em background, fora do `/uploads` público
- `IMPORT_JOB_WORKER_ENABLED` (padrão `true`) — worker que processa os jobs de importação em background
- `LIBRARY_SYNC_SCHEDULER_ENABLED` (padrão `true`) — agendador do sync global
- `LIBRARY_SYNC_REQUEST_TIMEOUT` (padrão `20`) — timeout, em segundos, das chamadas aos provedores
- `CORS_ORIGINS` (JSON string)

Exemplo de `.env` mínimo:

```
DATABASE_URL=sqlite:///./logged.db
UPLOAD_DIR=./uploads
CORS_ORIGINS=["http://localhost:5173","http://localhost:3000"]
```

## Observações finais
- Você já confirmou que a instalação funcionou com `Python 3.12.12` — ótimo; agora basta ativar o venv e rodar `uvicorn`.
- Se quiser, posso (a) iniciar o servidor aqui na sessão, (b) criar um script `make` ou `scripts/run.sh` para facilitar, ou (c) commitar o `README.md`. Diga qual prefere.

## Importação e sincronização de bibliotecas

A página **Importar** tem duas abas: **Importar** e **Revisão**.

### Importar

Aceita arquivo, username ou URL, dependendo do provedor. A leitura e o matching
rodam em background e o job fica salvo; se sair da página, o toast e o badge da
sidebar levam de volta à revisão. O histórico de jobs manuais fica nesta aba.

Provedores: Letterboxd, AniList, MyAnimeList, Goodreads, Trakt, IMDb, Grouvee e
Infinite Backlog (arquivo); AniList (username); Suwayomi (URL); Kitsu, Simkl,
Hardcover e Open Library (conexão já configurada em Integrações).

### Revisão

Fila única para jobs manuais prontos e sugestões do sync. Filtros: Pendentes,
Match 100%, Precisa validar, Sem match, Aprovados, Ignorados e Todos. Dá para
aprovar em lote, trocar o candidato, buscar outro match, ignorar e restaurar.
Itens manuais são confirmados em conjunto por job; itens do sync são confirmados
individualmente. A API unificada fica em `/api/review`.

Os matches são classificados em **Match 100%**, **Precisa validar**, **Sem
match** e **Já na biblioteca**. O commit continua usando o mesmo gravador da
importação manual: mídia, logs, backlog e vínculo de sync são criados juntos.

#### API de revisão

A revisão unificada fica em `/api/review`:

- `GET /api/review/items` — lista paginada com filtros de estado, match, origem,
  provedor, tipo de mídia e busca;
- `GET /api/review/counts` — contadores de tarefas e itens usados pela sidebar,
  pela aba Revisão e pelos filtros;
- `PATCH /api/review/items/{key}` — escolhe candidato ou ignora/restaura um item;
- `POST /api/review/state` — ignorar/restaurar em lote;
- `POST /api/review/commit` — confirma itens manuais e do sync no mesmo pedido.

A camada de serviço lê as tabelas existentes (`import_job_items` e
`library_sync_suggestions`) e as expõe no mesmo contrato `ReviewItem`. Os
endpoints antigos de importação e de sugestões continuam disponíveis.

#### Fase 3B — commit item a item para importações manuais (não implementado)

Na Fase 3A, selecionar qualquer item de um job manual confirma **todos os itens
não ignorados daquele job**. Itens do sync são confirmados individualmente. A
interface mostra um aviso explícito sobre esse comportamento.

A Fase 3B, quando necessária, permitirá confirmar apenas os itens selecionados
de um job manual:

- o job continua em revisão enquanto houver itens pendentes;
- o job vira `done` somente quando todos os itens forem confirmados ou
  ignorados;
- `ImportJobCommitRequest` ganha um `item_ids` opcional;
- `ImportJobService.commit` processa apenas os itens informados e calcula o
  status restante do job;
- `ReviewService.commit` repassa os itens manuais selecionados;
- `summary_json` e `counts_json` são atualizados após cada commit parcial;
- a UI remove o aviso de confirmação conjunta e permite seleção item a item;
- testes cobrem commit parcial, conclusão do job, idempotência e
  compatibilidade com o wizard antigo.

A primeira versão da 3B não deve exigir novas colunas no banco: `commit_outcome`
e `ignored` já permitem derivar o que falta revisar.

#### Fase 3C — tabela física de revisão (opcional)

Só será avaliada se a API de adaptação tiver problema de performance. Envolveria
uma tabela `review_items`, backfill das duas fontes e escrita dupla durante a
transição. As tabelas originais continuariam como auditoria.

### Sincronização automática

Em **Configurações → Integrações**:

- ative o sync global;
- defina o horário inicial e a frequência (1–24x por dia); os horários são
  fixos a partir do horário inicial;
- escolha quais provedores participam do sync geral;
- use **Sincronizar tudo** para executar os provedores incluídos em sequência.

**Sincronizar agora** em um provedor funciona mesmo sem ele participar do sync
geral. O Suwayomi participa quando a conta está configurada e habilitada.

Cada execução roda em background e abre um diálogo com progresso por provedor e
por item. **Continuar em segundo plano** fecha o diálogo sem cancelar; clicar de
novo em um botão de sync reabre a execução ativa. Syncs agendados aparecem no
mesmo diálogo. O estado é mantido em memória: reiniciar a API perde o progresso
e a execução em andamento.

Provedores: AniList, MyAnimeList, Kitsu, Trakt, Simkl, Letterboxd, Hardcover,
Open Library e Suwayomi. Trakt e Simkl usam OAuth device; Hardcover usa token
pessoal; MyAnimeList usa Client ID.

O sync do Letterboxd é parcial: o RSS traz apenas os ~50 diários mais recentes
e não inclui a watchlist. Para o histórico completo, use a importação do ZIP
exportado pelo Letterboxd em **Importar**.

Variáveis de ambiente para os apps OAuth/Client ID:

```env
TRAKT_CLIENT_ID=
TRAKT_CLIENT_SECRET=
SIMKL_CLIENT_ID=
SIMKL_CLIENT_SECRET=
MAL_CLIENT_ID=
```

Trakt e Simkl usam o fluxo de device code: o app mostra um código e o usuário
aprova no site. O token do Hardcover é criado nas configurações da conta do
próprio Hardcover.

### Como o sync decide o que fazer

O sync nunca cria mídia sozinho. Todo título remoto que ainda não existe no
Logged vira uma sugestão pendente em **Importar → Revisão**, inclusive os
matches exatos. A revisão usa o mesmo commit da importação manual: ao aprovar,
a mídia, os logs, o backlog e o vínculo de sync são criados juntos.

- títulos já vinculados recebem status, backlog e logs conforme o mapeamento;
- logs são criados quando o provedor fornece datas de início/fim/leitura;
- alterações manuais são preservadas até o status de origem mudar;
- só o backlog criado pelo próprio sync é removido por ele;
- títulos novos que aparecerem depois também entram na revisão.

Depois de conectar uma integração, a UI sugere a **sincronização inicial**. Ela
não cria títulos direto: apenas popula a fila de revisão. O Suwayomi segue a
mesma regra e usa as categorias como status/backlog.
