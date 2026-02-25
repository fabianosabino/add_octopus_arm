# SimpleClaw v2.0 — Guia de Instalação
---

## Pré-requisitos

| Software         | Versão mínima | Obrigatório? |
|------------------|---------------|--------------|
| Docker           | 24.0+         | Sim          |
| Docker Compose   | 2.20+         | Sim          |
| Ollama           | 0.3+          | Só se usar modelos locais |
| Git              | 2.30+         | Recomendado  |

**RAM mínima:** 4GB (Ollama + PostgreSQL + App)
**RAM recomendada:** 8GB (para rodar os dois modelos confortavelmente)

---

## Passo 1 — Transferir o Projeto

Copie o arquivo `simpleclaw-v2.tar.gz` para a máquina destino e extraia:

```bash
# Na máquina destino
mkdir -p ~/projects
cd ~/projects
tar -xzf simpleclaw-v2.tar.gz
cd simpleclaw
```

---

## Passo 2 — Instalar Ollama (se usar modelos locais)

```bash
# Linux
curl -fsSL https://ollama.ai/install.sh | sh

# Verificar se está rodando
ollama --version

# Baixar os modelos (isso demora na primeira vez)
ollama pull qwen3:0.6b        # ~400MB - Router (leve, sempre on)
ollama pull nanbeige4.1:3b     # ~2GB  - Especialista (on demand)
```

**Se preferir usar API externa (OpenAI/Anthropic):** pule este passo,
configure as API keys no `.env` (Passo 3).

---

## Passo 3 — Configurar o Ambiente

```bash
# Copiar template de variáveis de ambiente
cp .env.example .env

# Editar com seu editor preferido
nano .env
```

**Configurações OBRIGATÓRIAS que você precisa preencher:**

```env
# 1. Token do Telegram Bot (obtenha com @BotFather no Telegram)
SIMPLECLAW_TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11

# 2. Seu Telegram ID (obtenha com @userinfobot no Telegram)
SIMPLECLAW_TELEGRAM_ADMIN_IDS=[seu_telegram_id_aqui]

# 3. Chave do Vault (gere uma string aleatória)
SIMPLECLAW_VAULT_MASTER_KEY=gere_uma_string_aleatoria_de_64_caracteres
```

**Para gerar a chave do vault:**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Configuração de Modelos

**Opção A — Modelos locais via Ollama (padrão, custo zero):**
```env
SIMPLECLAW_ROUTER_PROVIDER=ollama
SIMPLECLAW_ROUTER_MODEL_ID=qwen3:0.6b
SIMPLECLAW_SPECIALIST_PROVIDER=ollama
SIMPLECLAW_SPECIALIST_MODEL_ID=nanbeige4.1:3b
```

**Opção B — OpenAI:**
```env
SIMPLECLAW_ROUTER_PROVIDER=openai
SIMPLECLAW_ROUTER_MODEL_ID=gpt-4.1-nano
SIMPLECLAW_ROUTER_API_KEY=sk-xxxx
SIMPLECLAW_SPECIALIST_PROVIDER=openai
SIMPLECLAW_SPECIALIST_MODEL_ID=gpt-4.1-mini
SIMPLECLAW_SPECIALIST_API_KEY=sk-xxxx
```

**Opção C — Anthropic:**
```env
SIMPLECLAW_ROUTER_PROVIDER=anthropic
SIMPLECLAW_ROUTER_MODEL_ID=claude-haiku-4-5-20251001
SIMPLECLAW_ROUTER_API_KEY=sk-ant-xxxx
SIMPLECLAW_SPECIALIST_PROVIDER=anthropic
SIMPLECLAW_SPECIALIST_MODEL_ID=claude-sonnet-4-5-20250929
SIMPLECLAW_SPECIALIST_API_KEY=sk-ant-xxxx
```

**Opção D — Mix (router local, especialista na nuvem):**
```env
SIMPLECLAW_ROUTER_PROVIDER=ollama
SIMPLECLAW_ROUTER_MODEL_ID=qwen3:0.6b
SIMPLECLAW_SPECIALIST_PROVIDER=anthropic
SIMPLECLAW_SPECIALIST_MODEL_ID=claude-sonnet-4-5-20250929
SIMPLECLAW_SPECIALIST_API_KEY=sk-ant-xxxx
```

---

## Passo 4 — Subir os Containers

```bash
# Subir tudo (PostgreSQL + SearXNG + App)
docker compose up -d

# Verificar se está rodando
docker compose ps

# Ver logs em tempo real
docker compose logs -f simpleclaw
```

**Saída esperada nos logs:**
```
simpleclaw.starting  version=2.0.0 router_model=ollama/qwen3:0.6b
simpleclaw.database_ready
simpleclaw.scheduler_ready
simpleclaw.ready  message=All systems operational 🟢
```

---

## Passo 5 — Testar

1. Abra o Telegram
2. Procure o seu bot pelo username que você criou no BotFather
3. Envie `/start`
4. Envie uma mensagem qualquer: "Olá, quem é você?"

**Se o bot respondeu:** tudo funcionando ✅

---

## Troubleshooting

### Bot não responde
```bash
# Verificar logs
docker compose logs simpleclaw | tail -50

# Verificar se o token está correto
docker compose exec simpleclaw env | grep TELEGRAM
```

### Erro de conexão com Ollama
O Ollama roda FORA do Docker (na máquina host). O container precisa
acessar o host. Adicione ao `.env`:
```env
SIMPLECLAW_ROUTER_API_BASE=http://host.docker.internal:11434
SIMPLECLAW_SPECIALIST_API_BASE=http://host.docker.internal:11434
```

**No Linux**, pode ser necessário usar o IP real da máquina:
```bash
# Descobrir o IP do host
ip route show default | awk '{print $3}'
# Usar esse IP no lugar de host.docker.internal
```

Ou rodar o Docker com `--network=host`:
```bash
# Alternativa: editar docker-compose.yaml e adicionar ao serviço simpleclaw:
# network_mode: "host"
```

### Erro de banco de dados
```bash
# Verificar se PostgreSQL está rodando
docker compose exec postgres pg_isready

# Recriar banco do zero
docker compose down -v
docker compose up -d
```

### Verificar uso de RAM
```bash
docker stats --no-stream
```

---

## Comandos Úteis

```bash
# Parar tudo
docker compose down

# Parar e limpar dados (CUIDADO: apaga o banco)
docker compose down -v

# Rebuild após mudanças no código
docker compose build simpleclaw
docker compose up -d simpleclaw

# Entrar no container do app
docker compose exec simpleclaw bash

# Acessar PostgreSQL diretamente
docker compose exec postgres psql -U simpleclaw -d simpleclaw

# Ver schemas e tabelas criadas
docker compose exec postgres psql -U simpleclaw -d simpleclaw -c "\dt system.*"
```

---

## Estrutura de Dados no Host

Após subir, estas pastas são criadas e persistem entre restarts:

```
simpleclaw/
├── context/          ← Arquivos de tarefas (volume montado)
│   ├── pending/
│   ├── processing/
│   ├── completed/
│   └── interrupt/
├── backups/          ← Backups automáticos (3am)
│   ├── daily/
│   └── pre_task/
└── logs/             ← Logs da aplicação
```

O banco de dados fica no volume Docker `simpleclaw_pgdata`.
