#!/usr/bin/env bash

# Fixado pelo install/updater; se vazio, resolve via /etc/easy_app_dir
APP_DIR=""

if [ -z "${APP_DIR}" ]; then
  if [ -f /etc/easy_app_dir ]; then
    APP_DIR="$(tr -d '[:space:]' < /etc/easy_app_dir)"
  else
    APP_DIR="/opt/easy"
  fi
fi

if [ -z "$APP_DIR" ] || [ ! -d "$APP_DIR" ]; then
  echo "❌ Diretório do Easy não encontrado${APP_DIR:+: $APP_DIR}."
  echo "Execute o installer novamente."
  exit 1
fi

SHELL_BIN="sh"

# Detecta docker compose
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif docker-compose version >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "❌ Docker Compose não encontrado"
  exit 1
fi

ensure_jq() {
  if command -v jq >/dev/null 2>&1; then
    return 0
  fi

  echo "⚙️ jq não encontrado, instalando..."

  if command -v apt >/dev/null 2>&1; then
    sudo apt update && sudo apt install -y jq
  elif command -v apk >/dev/null 2>&1; then
    sudo apk add jq
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y jq
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y jq
  else
    echo "❌ Não foi possível instalar o jq automaticamente."
    echo "Instale o pacote 'jq' manualmente."
    exit 1
  fi

  if ! command -v jq >/dev/null 2>&1; then
    echo "❌ Falha ao instalar o jq."
    exit 1
  fi
}

if $DC exec backend sh -c "command -v bash" >/dev/null 2>&1; then
  SHELL_BIN="bash"
fi

# garantir env carregado
if [ -f "$APP_DIR/.env" ]; then
  set -a
  . "$APP_DIR/.env"
  set +a
else
  echo "⚠️ .env não encontrado"
fi

cd "$APP_DIR" || exit 1

case "$1" in

start)
  COMPOSE_FILES=(-f docker-compose.yml)
  export DEBUG_FASTBASE="${DEBUG_FASTBASE:-false}"
  shift
  for arg in "$@"; do
    case "$arg" in
      debug) export DEBUG_FASTBASE=true ;;
      --cpu-prof|-c)
        if [ ! -f docker-compose.cpu-prof.yml ]; then
          echo "❌ docker-compose.cpu-prof.yml não encontrado em $APP_DIR"
          echo "   Reexecute o install.sh ou copie o arquivo do installer."
          exit 1
        fi
        COMPOSE_FILES+=(-f docker-compose.cpu-prof.yml)
        mkdir -p profiles
        echo "🔬 CPU profiling habilitado (profiles/)"
        ;;
    esac
  done
  $DC "${COMPOSE_FILES[@]}" up -d --remove-orphans
;;

stop)
  $DC down
;;

restart)
  COMPOSE_FILES=(-f docker-compose.yml)
  export DEBUG_FASTBASE="${DEBUG_FASTBASE:-false}"
  shift
  for arg in "$@"; do
    case "$arg" in
      debug) export DEBUG_FASTBASE=true ;;
      --cpu-prof|-c)
        if [ ! -f docker-compose.cpu-prof.yml ]; then
          echo "❌ docker-compose.cpu-prof.yml não encontrado em $APP_DIR"
          exit 1
        fi
        COMPOSE_FILES+=(-f docker-compose.cpu-prof.yml)
        mkdir -p profiles
        echo "🔬 CPU profiling habilitado (profiles/)"
        ;;
    esac
  done
  $DC down --remove-orphans
  $DC "${COMPOSE_FILES[@]}" up -d --remove-orphans
;;

logs)
  if [ "$2" = "reset" ]; then
    $DC ps -q | xargs -I{} docker inspect --format='{{.LogPath}}' {} | while read log; do
      sudo truncate -s 0 $log
    done
  elif [ "$2" = "clean" ]; then
    $DC logs -f --tail=0
  elif [ "$2" = "tail" ]; then
    $DC logs -f --tail=1000
  elif [ "$2" = "all" ]; then
    $DC logs -f --tail=all
  else
     echo "📦 Comandos logs:"
     echo
     echo "easy logs reset (limpa os logs dos containers)"
     echo "easy logs clean (mostra os logs dos containers em tempo real)"
     echo "easy logs tail (mostra os últimos 1000 linhas dos logs dos containers)"
     echo
     echo "easy logs <comando>"
  fi
;;

status)
  echo "📊 Status dos serviços"
echo

# -------------------------
# Containers
# -------------------------

$DC ps backend | grep -q "Up" && echo "✔ backend rodando" || echo "❌ backend parado"
$DC ps worker | grep -q "Up" && echo "✔ worker rodando" || echo "❌ worker parado"
$DC ps frontend | grep -q "Up" && echo "✔ frontend rodando" || echo "❌ frontend parado"
$DC ps redis | grep -q "Up" && echo "✔ redis rodando" || echo "❌ redis parado"

echo

# -------------------------
# PostgreSQL
# -------------------------
if env PGPASSWORD="$PG_PASSWORD" timeout 5 psql \
  -h "$PG_HOST" \
  -p "$PG_PORT" \
  -U "$PG_USER" \
  -d "$PG_DBNAME" \
  -c "SELECT 1;" >/dev/null 2>&1; then
  echo "✔ PostgreSQL conectado"
else
  echo "❌ PostgreSQL não responde"
fi

;;

update)
  shift
  UPDATER_ARGS=()
  for arg in "$@"; do
    case "$arg" in
      dry|dry-run|--dry-run)
        UPDATER_ARGS+=(--dry-run)
        ;;
      --skip-self-update)
        UPDATER_ARGS+=(--skip-self-update)
        ;;
      *)
        echo "❌ Opção desconhecida para update: $arg"
        echo "Uso: easy update [dry|--dry-run] [--skip-self-update]"
        exit 1
        ;;
    esac
  done
  python3 "$APP_DIR/updater.py" "${UPDATER_ARGS[@]}"
;;

# ========================================
# BACKUP
# ========================================
backup)
  if [ ! -f "$APP_DIR/backup.sh" ]; then
    echo "❌ backup.sh não encontrado em $APP_DIR"
    exit 1
  fi
  shift
  APP_DIR="$APP_DIR" bash "$APP_DIR/backup.sh" "$@"
;;

# ========================================
# EXEC
# ========================================
exec)
  shift

  if [ -z "$1" ]; then
    echo "❌ Informe o comando"
    exit 1
  fi

  $DC exec -it backend "$@"
;;

# ========================================
# SHELL
# ========================================
shell)
  $DC exec -it backend $SHELL_BIN
;;

# ========================================
# DB
# ========================================
db)

case "$2" in

bootstrap)
  $DC exec backend npm run db:bootstrap
;;

schema)
  $DC exec backend npm run db:schema
;;

seed)
  $DC exec backend npm run db:seed
;;

alter)
  $DC exec backend npm run db:alter
;;

core)
  $DC exec backend npm run db:core
;;

wms)
  $DC exec backend npm run db:wms:all
;;

wms:bootstrap)
  $DC exec backend npm run db:wms:bootstrap
;;

wms:schema)
  $DC exec backend npm run db:wms:schema
;;

wms:seed)
  $DC exec backend npm run db:wms:seed
;;

wms:alter)
  $DC exec backend npm run db:wms:alter
;;

wms:core)
  $DC exec backend npm run db:wms:core
;;

wms:functions)
  $DC exec backend npm run db:wms:functions
;;

wms:triggers)
  $DC exec backend npm run db:wms:triggers
;;

wms:all)
  $DC exec backend npm run db:wms:all
;;

all)
  $DC exec backend npm run db:all
;;

*)
  echo "📦 Comandos DB:"
  echo
  echo "easy db bootstrap"
  echo "easy db schema"
  echo "easy db seed"
  echo "easy db alter"
  echo "easy db core"
  echo
  echo "easy db wms"
  echo "easy db wms:bootstrap"
  echo "easy db wms:schema"
  echo "easy db wms:seed"
  echo "easy db wms:alter"
  echo "easy db wms:core"
  echo "easy db wms:functions"
  echo "easy db wms:triggers"
  echo "easy db wms:all"
  echo
  echo "easy db all"
;;

esac
;;

# ========================================
# INFO
# ========================================
info)

SERVER_IP=$(hostname -I | awk '{print $1}')

echo "📦 Easy - Informações da instalação"
echo

echo "📁 Diretório:"
echo "  $APP_DIR"
echo

# ========================
# VERSÃO
# ========================

echo "📦 Versão"

SERVER_IP=$(hostname -I | awk '{print $1}')
DATA=$(curl -s "http://$SERVER_IP:$SERVER_PORT/status")

if [ -n "$DATA" ]; then

  ensure_jq

  LOCAL_VERSION=$(echo "$DATA" | jq -r '.app.version.local.version')
  LOCAL_BUILD=$(echo "$DATA" | jq -r '.app.version.local.build')
  REMOTE_VERSION=$(echo "$DATA" | jq -r '.app.version.remote.version')
  REMOTE_BUILD=$(echo "$DATA" | jq -r '.app.version.remote.build')

  echo "  Local:  $LOCAL_VERSION ($LOCAL_BUILD)"
  echo "  Remota: $REMOTE_VERSION ($REMOTE_BUILD)"
else
  echo "  ❌ Não foi possível obter versão"
fi

echo


echo "🌍 Servidor:"
echo "  $SERVER_IP"
echo

echo "🗄 PostgreSQL"
echo "  Host: $PG_HOST"
echo "  Port: $PG_PORT"
echo "  Database: $PG_DBNAME"
echo

echo "🧠 Redis"
echo "  Port: $REDIS_PORT"
echo

echo "⚙ Backend"
echo "  http://$SERVER_IP:$SERVER_PORT"
echo

echo "🌐 Frontend"
echo "  http://$SERVER_IP:$REACT_PORT"
echo

echo "🐳 Containers"
$DC ps
;;

# ========================================
# DOCTOR
# ========================================
doctor)

# garantir contexto correto
cd "$APP_DIR" 2>/dev/null || {
  echo "❌ Diretório $APP_DIR não encontrado"
  exit 1
}

if !docker info >/dev/null 2>&1; then  
  echo "❌ Docker não está rodando"
fi

# garantir env carregado
if [ -f "$APP_DIR/.env" ]; then
  set -a
  . "$APP_DIR/.env"
  set +a
else
  echo "⚠️ .env não encontrado"
fi

echo "🔎 Easy Doctor"
echo

# ========================
# BASIC MODE
# ========================
if [ "$2" != "--full" ]; then

  if !docker info >/dev/null 2>&1; then    
    echo "❌ Docker não está rodando"
  fi

  echo

  $DC ps backend | grep -q Up && echo "✔ backend rodando" || echo "❌ backend parado"
  $DC ps worker | grep -q Up && echo "✔ worker rodando" || echo "❌ worker parado"
  $DC ps frontend | grep -q Up && echo "✔ frontend rodando" || echo "❌ frontend parado"
  $DC ps redis | grep -q Up && echo "✔ redis rodando" || echo "❌ redis parado"

  echo

  if [ -n "$SERVER_PORT" ] && timeout 2 bash -c "</dev/tcp/localhost/$SERVER_PORT" &>/dev/null; then
    echo "✔ Backend responde na porta $SERVER_PORT"
  else
    echo "❌ Backend não responde na porta ${SERVER_PORT:-'não definida'}"
  fi

  if [ -n "$REACT_PORT" ] && timeout 2 bash -c "</dev/tcp/localhost/$REACT_PORT" &>/dev/null; then
    echo "✔ Frontend responde na porta $REACT_PORT"
  else
    echo "❌ Frontend não responde na porta ${REACT_PORT:-'não definida'}"
  fi

  echo

  echo "💾 Disco disponível:"
  df -h / | awk 'NR==2 {print $4}'

  echo
  echo "👉 Use 'easy doctor --full' para diagnóstico completo"
  exit 0
fi

# ========================
# FULL MODE
# ========================

# -------------------------
# garantir sysstat (mpstat)
# -------------------------
if ! command -v mpstat >/dev/null 2>&1; then
  echo "⚙️ mpstat não encontrado, instalando (sysstat)..."

  if command -v apt >/dev/null 2>&1; then
    sudo apt install -y sysstat || {
      echo "🔄 Tentando com apt update..."
      sudo apt update && sudo apt install -y sysstat || {
        echo "❌ Falha ao instalar sysstat"
        exit 1
      }
    }
  else
    echo "❌ apt não encontrado. Instale o pacote sysstat manualmente."
    exit 1
  fi

  echo "✅ sysstat instalado com sucesso"
fi

echo "📊 Sistema"
echo "CPU:"
mpstat 1 1 | awk '/Average/ {print "  Uso:", 100 - $NF "%"}'

echo "RAM:"
free -h | awk '/Mem:/ {print "  Total:", $2, "| Usado:", $3, "| Livre:", $4 "| Disponível:", $6}'

echo

echo "💾 Disco:"
df -h / | awk 'NR==2 {print "  Total:", $2, "| Usado:", $3, "| Livre:", $4}'

echo

# ========================
# DOCKER
# ========================

echo "🐳 Docker"
docker info >/dev/null 2>&1 && echo "  ✔ Docker rodando" || echo "  ❌ Docker não está rodando"

echo
echo "📦 Containers"
docker ps --format "  - {{.Image}}"

echo

# ========================
# PORTAS
# ========================

echo "🌐 Portas"

[ -n "$SERVER_PORT" ] && timeout 2 bash -c "</dev/tcp/localhost/$SERVER_PORT" &>/dev/null \
  && echo "  ✔ Backend ($SERVER_PORT)" \
  || echo "  ❌ Backend (${SERVER_PORT:-não definida})"

[ -n "$REACT_PORT" ] && timeout 2 bash -c "</dev/tcp/localhost/$REACT_PORT" &>/dev/null \
  && echo "  ✔ Frontend ($REACT_PORT)" \
  || echo "  ❌ Frontend (${REACT_PORT:-não definida})"

echo


# ========================
# BACKUP
# ========================

echo "💾 Backup"

LAST_BACKUP=$(ls -t "$APP_DIR/backups" 2>/dev/null | head -n 1)

if [ -z "$LAST_BACKUP" ]; then
  echo "  ❌ Nenhum backup encontrado"
else
  LAST_DATE=$(stat -c %y "$APP_DIR/backups/$LAST_BACKUP" | cut -d'.' -f1)
  echo "  ✔ Último backup: $LAST_BACKUP"
  echo "  📅 Data: $LAST_DATE"
fi

echo


# ========================
# UPTIME
# ========================
echo "🕜 Tempo em atividade:"
uptime -p

echo

# ========================
# HEALTH API
# ========================

echo "🩺 Health API"

if curl -s --max-time 3 "http://$SERVER_IP:$SERVER_PORT/status" >/dev/null; then
  echo "  ✔ API respondeu"
else
  echo "  ❌ API não respondeu"
fi

echo
;;

# ========================================
# UNINSTALL
# ========================================
uninstall)
cd "$APP_DIR"
./uninstall.sh
;;

# ========================================
# HELP
# ========================================
*)
echo "🔧 Easy CLI"
echo
echo "Comandos disponíveis:"
echo
echo "easy start"
echo "easy start debug"
echo "easy start --cpu-prof"
echo "easy start debug --cpu-prof"
echo "easy stop"
echo "easy restart"
echo "easy restart debug"
echo "easy restart --cpu-prof"
echo "easy status"
echo "easy logs tail"
echo "easy logs reset"
echo "easy logs clean"
echo "easy update"
echo "easy update dry"
echo "easy update --dry-run"
echo "easy update --skip-self-update"
echo "easy update dry --skip-self-update"
echo "easy backup"
echo "easy backup 7"
echo "easy backup --days 14"
echo "easy backup --days 7 --dry-run"
echo "easy info"
echo "easy doctor"
echo "easy uninstall"
echo
echo "easy exec <comando>"
echo "easy shell"
echo "easy db <comando>"
;;

esac