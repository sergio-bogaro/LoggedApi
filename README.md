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
