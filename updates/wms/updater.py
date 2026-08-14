#!/usr/bin/env python3

# -------------------------
# Função para testar e instalar packages de terceiros
# -------------------------
def ensure_package(import_name, package_name=None):
    try:
        __import__(import_name)
    except ImportError:
        import subprocess
        import sys

        pkg = package_name or import_name
        print(f"📦 Instalando '{pkg}'...")

        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

    return __import__(import_name)

# -------------------------
# Instala packages de terceiros se necessário
# -------------------------
ensure_package("requests")
ensure_package("packaging")

import json
import base64
import subprocess
import requests
import os
import sys
from datetime import datetime
from pathlib import Path
from packaging import version
import time


# -------------------------
# Paths & Config
# -------------------------
BASE_DIR = os.environ.get("APP_DIR", "/opt/easy")

VERSION_FILE = f"{BASE_DIR}/version.json"
ENV_FILE = f"{BASE_DIR}/.env"
LOG_DIR = f"{BASE_DIR}/logs"
LOG_FILE = f"{LOG_DIR}/updater.log"
BACKUP_DIR = f"{BASE_DIR}/backups"
ROLLBACK_FILE = f"{BASE_DIR}/rollback.json"

VERSION_JSON_URL = "https://raw.githubusercontent.com/DataincTech/easy/main/updates/wms/latest.json"
VERSION_TAGS_URL = "https://github.com/DataincTech/datainc-wms/tags"
# API Contents (repo privado — requer GITHUB_TOKEN/GH_TOKEN)
EASY_CLI_URL = (
    "https://api.github.com/repos/datainctech/datainc-wms/contents/installer/easy.sh?ref=main"
)
UPDATER_URL = (
    "https://api.github.com/repos/datainctech/datainc-wms/contents/installer/updater.py?ref=main"
)
# Fallback público (mesmo host do latest.json) — sem token
UPDATER_PUBLIC_URL = (
    "https://raw.githubusercontent.com/DataincTech/easy/main/updates/wms/updater.py"
)
EASY_CLI_PUBLIC_URL = (
    "https://raw.githubusercontent.com/DataincTech/easy/main/updates/wms/easy.sh"
)
CLI_BIN = "/usr/local/bin/easy"
TIMEOUT = 10

BACKUP_RETENTION = 7  # mantém últimos N backups

DRY_RUN = "--dry-run" in sys.argv
SKIP_SELF_UPDATE = "--skip-self-update" in sys.argv

# -------------------------
# Utils
# -------------------------
def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")



def compose_cmd():
    """Prefer plugin `docker compose`; fallback para docker-compose v1."""
    try:
        subprocess.check_output(
            ["docker", "compose", "version"],
            stderr=subprocess.DEVNULL,
        )
        return ["docker", "compose"]
    except Exception:
        return ["docker-compose"]


def compose_env():
    """
    Ambiente explícito para o Compose.
    Compose prioriza variáveis do shell sobre o .env do projeto —
    sempre passar o os.environ atualizado (APP_VERSION etc.).
    """
    return os.environ.copy()


def compose_run(args, env=None):
    run(compose_cmd() + args, env=env or compose_env())

def run(cmd, env=None):
    log(f"→ {' '.join(cmd)}")
    if DRY_RUN:
        log("🧪 DRY-RUN: comando não executado")
        return
    subprocess.run(cmd, check=True, env=env or compose_env(), cwd=BASE_DIR)


def load_env_file(path):
    if not os.path.exists(path):
        raise RuntimeError(f".env não encontrado em {path}")

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)


def resolve_app_dir():
    """APP_DIR efetivo: env → /etc/easy_app_dir → BASE_DIR."""
    if os.environ.get("APP_DIR"):
        return os.environ["APP_DIR"]
    easy_app_dir = Path("/etc/easy_app_dir")
    if easy_app_dir.is_file():
        value = easy_app_dir.read_text().strip()
        if value:
            return value
    return BASE_DIR


def github_headers():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        # raw = conteúdo do arquivo; +json = metadados (evita gravar JSON no disco)
        "Accept": "application/vnd.github.raw",
        "User-Agent": "easy-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def normalize_downloaded_bytes(content, url):
    """
    Se a API Contents devolver JSON (em vez de raw), decodifica o base64.
    Rejeita respostas de erro da API.
    """
    if not content:
        return None

    text = content.decode("utf-8", errors="replace").lstrip("\ufeff")
    stripped = text.lstrip()

    if stripped.startswith("{") and ("api.github.com" in (url or "") or '"encoding"' in stripped[:500]):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return content

        if data.get("message"):
            raise RuntimeError(data.get("message") or "Erro na API do GitHub")

        if data.get("encoding") == "base64" and data.get("content"):
            return base64.b64decode("".join(data["content"].split()))

        raise RuntimeError("Resposta da API do GitHub sem conteúdo utilizável")

    return content


def fetch_remote_bytes(urls):
    """
    Tenta baixar bytes de uma lista de URLs (público → API privada).
    Retorna (content, url_usada) ou (None, None).
    """
    last_error = None
    for url in urls:
        try:
            headers = github_headers() if "api.github.com" in url else {
                "User-Agent": "easy-updater",
                "Cache-Control": "no-cache",
            }
            res = requests.get(url, headers=headers, timeout=TIMEOUT)
            res.raise_for_status()
            if not res.content.strip():
                last_error = f"vazio em {url}"
                continue
            content = normalize_downloaded_bytes(res.content, url)
            if not content or not content.strip():
                last_error = f"conteúdo inválido em {url}"
                continue
            return content, url
        except Exception as e:
            last_error = e
            log(f"⚠️ Download falhou ({url}): {e}")
    log(f"⚠️ Nenhum mirror respondeu: {last_error}")
    return None, None


def looks_like_shell_script(content: str) -> bool:
    head = content.lstrip("\ufeff").lstrip()
    return head.startswith("#!") and ("bash" in head.splitlines()[0] or "sh" in head.splitlines()[0])


def looks_like_python_script(content: bytes) -> bool:
    head = content.lstrip().decode("utf-8", errors="replace")
    return head.startswith("#!") or head.startswith("import ") or head.startswith("\"\"\"") or head.startswith("'''")


def update_easy_cli():
    """
    Baixa easy.sh do Git, ajusta APP_DIR e instala em APP_DIR + /usr/local/bin/easy.
    YMLs NÃO são atualizados aqui (somente na instalação).
    """
    log("🔧 Atualizando Easy CLI...")

    if DRY_RUN:
        log(f"🧪 DRY-RUN: CLI não baixada de {EASY_CLI_PUBLIC_URL}")
        return

    app_dir = resolve_app_dir()
    dest = Path(app_dir) / "easy.sh"

    # Preferir mirror público (raw) — evita gravar JSON da API Contents
    remote, source = fetch_remote_bytes([EASY_CLI_PUBLIC_URL, EASY_CLI_URL])
    if remote is None:
        log("⚠️ Não foi possível baixar a CLI — mantendo versão atual")
        return

    content = remote.decode("utf-8", errors="replace")
    if not content.strip():
        log("⚠️ CLI baixada está vazia — mantendo versão atual")
        return

    if not looks_like_shell_script(content):
        log("⚠️ CLI baixada não parece um script shell (possível JSON/HTML) — mantendo versão atual")
        return

    lines = []
    stamped = False
    for line in content.splitlines(keepends=True):
        if line.startswith("APP_DIR=") and not stamped:
            lines.append(f'APP_DIR="{app_dir}"\n')
            stamped = True
        else:
            lines.append(line)

    text = "".join(lines)

    # Valida sintaxe antes de sobrescrever a CLI do sistema
    try:
        subprocess.run(
            ["bash", "-n"],
            input=text.encode("utf-8"),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode("utf-8", errors="replace")

        log(f"⚠️ CLI inválida:\n{detail}")

        for i, line in enumerate(text.splitlines(), 1):
            if abs(i - 637) <= 5:
                log(f"{i:04d}: {line}")

        return

    dest.write_text(text)
    os.chmod(dest, 0o755)

    try:
        subprocess.run(["sudo", "cp", str(dest), CLI_BIN], check=True)
        subprocess.run(["sudo", "chmod", "+x", CLI_BIN], check=True)
    except Exception as e:
        log(f"⚠️ CLI salva em {dest}, mas falhou em {CLI_BIN}: {e}")
        return

    log(f"✅ CLI atualizada: {CLI_BIN} (fonte: {source})")


def update_self():
    """
    Baixa updater.py do Git. Se mudou, grava e reinicia o processo
    com --skip-self-update para aplicar a nova lógica nesta execução.
    """
    if SKIP_SELF_UPDATE:
        log("↪️ Self-update já aplicado nesta execução")
        return False

    log("🔄 Verificando atualização do próprio updater...")

    if DRY_RUN:
        log(f"🧪 DRY-RUN: updater não baixado de {UPDATER_PUBLIC_URL}")
        return False

    app_dir = resolve_app_dir()
    dest = Path(app_dir) / "updater.py"
    current_path = Path(__file__).resolve()

    # Preferir mirror público (raw)
    remote, source = fetch_remote_bytes([UPDATER_PUBLIC_URL, UPDATER_URL])
    if remote is None:
        log("⚠️ Não foi possível baixar o updater — mantendo versão atual")
        return False

    if not looks_like_python_script(remote):
        log("⚠️ Updater baixado inválido (possível JSON/HTML) — mantendo versão atual")
        return False

    try:
        local = current_path.read_bytes()
    except Exception:
        local = b""

    if remote == local:
        log("✔️ Updater já está na versão mais recente")
        return False

    log(f"⬇️ Nova versão do updater detectada — aplicando (fonte: {source})...")
    dest.write_bytes(remote)
    os.chmod(dest, 0o755)

    # Se o processo atual não é o arquivo em APP_DIR, espelha também
    if current_path != dest.resolve():
        try:
            current_path.write_bytes(remote)
            os.chmod(current_path, 0o755)
        except Exception as e:
            log(f"⚠️ updater salvo em {dest}, cópia em {current_path} falhou: {e}")

    log("♻️ Updater atualizado — reiniciando com a nova versão...")

    new_argv = [
        a for a in sys.argv[1:]
        if a not in ("--skip-self-update",)
    ] + ["--skip-self-update"]

    os.execv(sys.executable, [sys.executable, str(dest)] + new_argv)
    return True  # unreachable; execv substitui o processo


def log_running_images():
    """Registra tags/IDs das imagens em uso (diagnóstico pós-pull/up)."""
    try:
        out = subprocess.check_output(
            compose_cmd() + ["images"],
            cwd=BASE_DIR,
            env=compose_env(),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if out:
            for line in out.splitlines():
                log(f"🖼️  {line}")
    except Exception as e:
        log(f"⚠️ Não foi possível listar imagens: {e}")


def assert_running_version(expected_version, expected_build=None):
    """
    Garante que o backend em execução já está na versão alvo.
    Evita seguir para migrations com imagem antiga (bug APP_VERSION no shell).
    """
    running = get_local_version()
    got_v = str(running.get("version", "unknown"))
    got_b = str(running.get("build", "unknown"))
    log(f"🔍 Backend em execução: {got_v} ({got_b})")

    if got_v != str(expected_version):
        raise RuntimeError(
            f"Container ainda na versão {got_v}, esperado {expected_version}. "
            f"Compose provavelmente usou APP_VERSION do shell antigo."
        )

    if expected_build and got_b != str(expected_build):
        log(
            f"⚠️ Build diferente do esperado "
            f"(rodando={got_b}, esperado={expected_build}) — seguindo mesmo assim"
        )


def recreate_stack():
    """
    Pull + recreate forçado para a nova imagem ser assumida de imediato
    (sem depender de restart manual do Docker).
    """
    app_version = os.environ.get("APP_VERSION", "unknown")
    log(f"⬇️ Pull das imagens (APP_VERSION={app_version})")
    try:
        compose_run(["pull", "--policy", "always"])
    except Exception:
        # Compose antigo sem --policy
        compose_run(["pull"])
    log_running_images()
    log(f"🚀 Recriando containers com ghcr.io/datainctech/*:{app_version}")
    # --pull always: garante digest novo mesmo se pull anterior foi ambíguo
    try:
        compose_run([
            "up", "-d",
            "--pull", "always",
            "--force-recreate",
            "--remove-orphans",
        ])
    except Exception:
        compose_run(["up", "-d", "--force-recreate", "--remove-orphans"])
    log_running_images()


# -------------------------
# Version handling
# -------------------------
def get_local_version():
    load_env_file(ENV_FILE)

    url = f"http://{os.environ.get('SERVER_HOST')}:{os.environ.get('SERVER_PORT')}/status"
    res = requests.get(url, timeout=TIMEOUT)
    res.raise_for_status()
    data = res.json()

    local_version = data.get('app', {}).get('version', {}).get('local', {})

    return {
        "version": local_version.get('version', "unknown"),
        "build": local_version.get('build', "unknown")
    }

def save_local_version(version, build, updated_at):
    if DRY_RUN:
        log("🧪 DRY-RUN: versão e build não gravados")
        return
    with open(VERSION_FILE, "w") as f:
        json.dump({"version": version, "build": build, "updated_at": updated_at}, f)

def get_current_backend_image():
    result = subprocess.check_output(
        compose_cmd() + ["images", "-q", "backend"],
        cwd=BASE_DIR,
    ).decode().strip()

    image = subprocess.check_output([
        "docker",
        "inspect",
        "--format",
        "{{.RepoTags}}",
        result
    ]).decode().strip()

    return image

def save_rollback_info(local_version, backup_file):
    data = {
        "version": local_version["version"],
        "build": local_version["build"],
        "backup_file": backup_file,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    if DRY_RUN:
        log(
            f"🧪 DRY-RUN: rollback.json não gravado: {data}"
        )
        return

    with open(ROLLBACK_FILE, "w") as f:
        json.dump(data, f, indent=2)

    log(f"✅ Rollback salvo: {ROLLBACK_FILE}")

def rollback_application():
    log("🧯 Iniciando rollback da aplicação")

    data = load_rollback_info()

    rollback_version = data["version"]
    rollback_build = data["build"]

    log(f"↩️ Voltando para versão {rollback_version}")

    update_env_version(
        rollback_version,
        rollback_build,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    save_local_version(
        rollback_version,
        rollback_build,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    compose_run(["pull"])
    compose_run(["up", "-d", "--force-recreate", "--remove-orphans"])

    wait_backend()

    log("✅ Aplicação restaurada")    

def load_rollback_info():
    if not os.path.exists(ROLLBACK_FILE):
        raise RuntimeError(
            f"Arquivo de rollback não encontrado: {ROLLBACK_FILE}"
        )

    with open(ROLLBACK_FILE, "r") as f:
        return json.load(f)

def update_env_version(version, build, updated_at):
    log(f"✏️ Atualizando APP_VERSION, APP_BUILD e APP_UPDATED_AT no .env")

    lines = []
    found = False
    found_updated_at = False
    found_build = False

    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith("APP_VERSION="):
                    lines.append(f"APP_VERSION={version}\n")
                    found = True

                elif line.startswith("APP_UPDATED_AT="):
                    lines.append(f"APP_UPDATED_AT='{updated_at}'\n")
                    found_updated_at = True  

                elif line.startswith("APP_BUILD="):
                    lines.append(f"APP_BUILD={build}\n")
                    found_build = True

                else:
                    lines.append(line)

    if not found:
        lines.append(f"\nAPP_VERSION={version}\n")
    
    if not found_updated_at:
        lines.append(f"APP_UPDATED_AT={updated_at}\n")

    if not found_build:
        lines.append(f"APP_BUILD={build}\n")

    if not DRY_RUN:
        with open(ENV_FILE, "w") as f:
            f.writelines(lines)

    # Docker Compose prioriza variáveis do shell sobre o .env do projeto.
    # Sem isso, o processo mantém APP_VERSION antigo (load_env_file) e
    # pull/up continuam na imagem anterior (ex.: 1.7.26 em vez de 1.7.27).
    os.environ["APP_VERSION"] = str(version)
    os.environ["APP_BUILD"] = str(build)
    os.environ["APP_UPDATED_AT"] = str(updated_at)
    log(f"📌 Ambiente do processo: APP_VERSION={version} APP_BUILD={build}")

def get_remote_version():
    try:
        url = f"{VERSION_JSON_URL}?ts={int(datetime.now().timestamp())}"
        res = requests.get(url, timeout=TIMEOUT)
        res.raise_for_status()
        data = res.json()

        return {
            "version": data.get("version", "unknown"),
            "build": data.get("build", "unknown"),
            "published_at": data.get("published_at"),
        }
    except:
        return None


def needs_update(local, remote):
    """
    Atualiza se a versão remota for maior, ou se a versão for igual
    mas o build mudou (hotfix na mesma tag).
    """
    if not remote:
        return False, "sem versão remota"

    local_v = str(local.get("version") or "0")
    remote_v = str(remote.get("version") or "0")
    local_b = str(local.get("build") or "")
    remote_b = str(remote.get("build") or "")

    try:
        lv = version.parse(local_v)
        rv = version.parse(remote_v)
    except Exception:
        return local_v != remote_v or (remote_b and local_b != remote_b), "comparação textual"

    if lv < rv:
        return True, "versão nova"
    if lv > rv:
        return False, "local mais nova"
    # mesma versão: hotfix republica a tag com build novo
    if remote_b and local_b and remote_b != local_b:
        return True, "mesma versão, build novo (hotfix)"
    if remote_b and (not local_b or local_b in ("unknown", "null")):
        return True, "build remoto presente e local ausente"
    return False, "já atualizado"


# -------------------------
# Backup & Restore
# -------------------------
def cleanup_old_backups():
    backups = sorted(
        Path(BACKUP_DIR).glob("backup_*.dump"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    for old in backups[BACKUP_RETENTION:]:
        log(f"🧹 Removendo backup antigo: {old.name}")
        if not DRY_RUN:
            old.unlink()


def backup_database():
    load_env_file(ENV_FILE)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    required = ["PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD", "PG_DBNAME"]
    for var in required:
        if not os.environ.get(var):
            raise RuntimeError(f"Variável ausente: {var}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = f"{BACKUP_DIR}/backup_{ts}.dump"

    log("🗄️ Criando backup do PostgreSQL")

    env = os.environ.copy()
    env["PGPASSWORD"] = os.environ["PG_PASSWORD"]

    run([
        "pg_dump",
        "-Fc",
        "-h", os.environ["PG_HOST"],
        "-p", os.environ["PG_PORT"],
        "-U", os.environ["PG_USER"],
        "-d", os.environ["PG_DBNAME"],
        "-f", backup_file
    ], env=env)

    log(f"✅ Backup criado: {backup_file}")
    cleanup_old_backups()

    return backup_file


def restore_database(backup_file):
    log(f"🧯 Restaurando banco a partir de {backup_file}")

    env = os.environ.copy()
    env["PGPASSWORD"] = os.environ["PG_PASSWORD"]

    log("🔌 Encerrando conexões ativas")

    run([
        "psql",
        "-h", os.environ["PG_HOST"],
        "-p", os.environ["PG_PORT"],
        "-U", os.environ["PG_USER"],
        "-d", os.environ["PG_DBNAME"],
        "-c",
        f"""
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE datname = '{os.environ["PG_DBNAME"]}'
        AND pid <> pg_backend_pid();
        """
    ], env=env)

    run([
        "pg_restore",
        "--clean",
        "--single-transaction",
        "--if-exists",
        "-h", os.environ["PG_HOST"],
        "-p", os.environ["PG_PORT"],
        "-U", os.environ["PG_USER"],
        "-d", os.environ["PG_DBNAME"],
        backup_file
    ], env=env)

    log("✅ Rollback concluído")

def wait_backend():
    load_env_file(ENV_FILE)

    url = (
        f"http://{os.environ.get('SERVER_HOST')}:"
        f"{os.environ.get('SERVER_PORT')}/status"
    )

    for _ in range(30):
        try:
            res = requests.get(url, timeout=5)

            if res.status_code == 200:
                log("✅ Backend disponível")
                return

        except Exception:
            pass

        time.sleep(2)

    raise RuntimeError("Backend não iniciou")

# -------------------------
# Update flow
# -------------------------

def wait_worker(timeout=60):
    """Confirma que o serviço worker está Up (quando existir no compose)."""
    log("⏳ Verificando worker...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.check_output(
                compose_cmd() + ["ps", "worker"],
                cwd=BASE_DIR,
                stderr=subprocess.DEVNULL,
            ).decode()
            if "Up" in out or "running" in out.lower():
                log("✅ Worker UP")
                return
        except Exception:
            pass
        time.sleep(2)
    log("⚠️ Worker não confirmado como UP (confira docker compose ps / logs worker)")



def perform_update(remote):
    version = remote["version"]
    build = remote["build"]

    log(f"⬇️ Nova versão detectada: {version} ({build})")

    backup_file = backup_database()

    local_version = get_local_version()

    save_rollback_info(local_version, backup_file)
    
    try:
        update_env_version(version, build, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        recreate_stack()

        log("⏳ Aguardando backend iniciar...")
        wait_backend()
        assert_running_version(version, build)

        log("🗄️ Executando migrations")
        compose_run([
            "run", "--rm",
            "backend",
            "npm", "run", "fastbase", "--", "migrate"
        ])

        log("🔄 Reiniciando stack após migrations (API + worker)")
        compose_run(["up", "-d", "--force-recreate", "--remove-orphans"])

        wait_backend()
        wait_worker()
        assert_running_version(version, build)

        save_local_version(version, build, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        

        log("✅ Atualização concluída")

    except Exception as e:

        log(f"❌ Falha durante atualização: {e}")

        try:

            log("🛑 Parando containers")
            compose_run(["down"])

            log("🧯 Restaurando banco")
            restore_database(backup_file)

            log("🧯 Restaurando aplicação")
            rollback_application()

            log("✅ Rollback concluído com sucesso")

        except Exception as rollback_error:

            log(
                f"🚨 FALHA CRÍTICA NO ROLLBACK: "
                f"{rollback_error}"
            )

        sys.exit(1)

# -------------------------
# Main
# -------------------------
def main():
    try:
        log("🔎 Verificando atualizações...")
        if DRY_RUN:
            log("🧪 MODO DRY-RUN ATIVADO")

        load_env_file(ENV_FILE)

        # Autoatualiza o updater e reinicia se necessário (antes do restante)
        update_self()

        local_version = get_local_version()
        remote_version = get_remote_version()

        if not remote_version:
            raise RuntimeError("Não foi possível obter a versão remota (latest.json)")

        log(f"💻 Versão local: {local_version['version']} ({local_version['build']})")
        log(f"🌍 Versão remota: {remote_version['version']} ({remote_version['build']})")

        should, reason = needs_update(local_version, remote_version)
        if should:
            log(f"📦 Motivo: {reason}")
            perform_update(remote_version)
        else:
            log(f"✔️ Sistema já está atualizado (v{local_version['version']})")
            log(f"✔️ Build local: {local_version['build']}")
            log(f"✔️ Build remota: {remote_version['build']}")

        # CLI sempre atualizada no `easy update` (YMLs não — só na instalação)
        update_easy_cli()

    except Exception as e:
        log(f"❌ Erro fatal no updater: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
