#!/usr/bin/env python3
"""
Sincroniza usuários do Moodle a partir de duas fontes:
  - TOTVS RM (SQL Server RMFOLHA) → login = CHAPA
  - Google Sheets (Temporarios_e_PJs) → login = CPF

Regras:
  - Usuário ativo que NÃO existe no Moodle → criado (senha aleatória, troca obrigatória)
  - Usuário ativo que já existe no Moodle  → reativado se estiver suspenso
  - Usuário inativo que existe no Moodle   → suspenso
  - Usuário inativo que NÃO existe         → ignorado (não cria)

Cron sugerido (todo dia às 06:00):
  0 6 * * * cd /opt/moodle_sync && python3 sync_moodle_users.py >> /var/log/moodle_sync.log 2>&1
"""

import csv
import logging
import os
import random
import string
import subprocess
import tempfile

import pyodbc
import gspread
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Configurações
# ---------------------------------------------------------------------------

# SQL Server — TOTVS RM
TOTVS_SERVER = "192.168.1.35"
TOTVS_DB     = "RMFOLHA"
TOTVS_USER   = "ModdleUser"
TOTVS_PASS   = "venttosmoddle"

# SQL Server — Moodle
MOODLE_SERVER = "192.168.1.35"
MOODLE_DB     = "DBModdle"
MOODLE_USER   = "ModdleUser"
MOODLE_PASS   = "venttosmoddle"

# Google Sheets
SHEETS_CREDENTIALS = "/opt/moodle_sync/service_account.json"
SHEETS_ID          = "1T4V7bY0LKJxSBRFxNgafviGRgVVTuIxOSQ1I9f_QYcI"

# Docker / Moodle
DOCKER_COMPOSE_DIR = "/home/engineering/projects/Moddle_VTT/moodle_VTT"
MOODLE_SERVICE     = "moodle"

# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

CONN_OPTS = "TrustServerCertificate=yes;Encrypt=no;"


def _conn_str(server: str, db: str, user: str, pwd: str) -> str:
    return (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={server};DATABASE={db};UID={user};PWD={pwd};{CONN_OPTS}"
    )


def gerar_senha() -> str:
    partes = (
        random.choices(string.ascii_uppercase, k=2)
        + random.choices(string.ascii_lowercase, k=4)
        + random.choices(string.digits, k=2)
        + random.choices("!@#$%", k=2)
    )
    random.shuffle(partes)
    return "".join(partes)


def dividir_nome(nome: str) -> tuple[str, str]:
    partes = nome.strip().split(" ", 1)
    return partes[0], partes[1] if len(partes) > 1 else ""


# ---------------------------------------------------------------------------
# Fontes de dados
# ---------------------------------------------------------------------------

def buscar_totvs() -> list[dict]:
    """Retorna TODOS os funcionários do TOTVS com flag ativo/inativo."""
    query = """
        SELECT f.CHAPA, p.NOME, p.EMAIL, f.CODSITUACAO
        FROM PPESSOA p
        INNER JOIN PFUNC f ON f.CODPESSOA = p.CODIGO
        WHERE p.EMAIL IS NOT NULL AND p.EMAIL <> ''
    """
    usuarios = []
    try:
        with pyodbc.connect(_conn_str(TOTVS_SERVER, TOTVS_DB, TOTVS_USER, TOTVS_PASS)) as conn:
            for row in conn.cursor().execute(query).fetchall():
                chapa     = str(row[0]).strip()
                nome      = str(row[1]).strip() if row[1] else ""
                email     = str(row[2]).strip() if row[2] else ""
                situacao  = str(row[3]).strip().upper() if row[3] else ""
                firstname, lastname = dividir_nome(nome)

                if not chapa:
                    continue

                usuarios.append({
                    "username":  chapa,
                    "firstname": firstname,
                    "lastname":  lastname,
                    "email":     email,
                    "ativo":     situacao == "A",
                })
        log.info("TOTVS RM: %d funcionários carregados.", len(usuarios))
    except Exception:
        log.exception("Erro ao consultar TOTVS RM.")
        raise
    return usuarios


def buscar_planilha() -> list[dict]:
    """Retorna TODOS os funcionários da planilha com flag ativo/inativo."""
    creds = Credentials.from_service_account_file(
        SHEETS_CREDENTIALS,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    linhas = gspread.authorize(creds).open_by_key(SHEETS_ID).sheet1.get_all_values()

    usuarios = []
    for i, linha in enumerate(linhas[1:], start=2):
        if len(linha) < 14:
            continue

        cpf      = linha[1].strip()   # B
        nome     = linha[3].strip()   # D
        situacao = linha[5].strip()   # F
        email    = linha[13].strip()  # N

        if not cpf or not email:
            log.warning("Linha %d: CPF ou e-mail vazio — ignorado.", i)
            continue

        username = cpf.replace(".", "").replace("-", "").replace("/", "")
        firstname, lastname = dividir_nome(nome)

        usuarios.append({
            "username":  username,
            "firstname": firstname,
            "lastname":  lastname,
            "email":     email,
            "ativo":     situacao.strip().upper() == "A",
        })

    log.info("Google Sheets: %d funcionários carregados.", len(usuarios))
    return usuarios


def buscar_moodle_existentes() -> dict[str, bool]:
    """Retorna usuários existentes no Moodle {username: esta_suspenso}."""
    query = """
        SELECT username, suspended
        FROM mdl_user
        WHERE deleted = 0 AND username NOT IN ('guest', 'admin')
    """
    usuarios = {}
    with pyodbc.connect(_conn_str(MOODLE_SERVER, MOODLE_DB, MOODLE_USER, MOODLE_PASS)) as conn:
        for row in conn.cursor().execute(query).fetchall():
            usuarios[str(row[0]).lower()] = bool(row[1])
    log.info("Moodle: %d usuários existentes.", len(usuarios))
    return usuarios


# ---------------------------------------------------------------------------
# Importação no Moodle via CLI
# ---------------------------------------------------------------------------

def _rodar_cli(csv_path: str, uutype: int, extra_args: list[str] = None) -> None:
    container_csv = "/tmp/moodle_sync.csv"

    subprocess.run(
        ["docker", "compose", "cp", csv_path, f"{MOODLE_SERVICE}:{container_csv}"],
        cwd=DOCKER_COMPOSE_DIR, check=True,
    )

    cmd = [
        "docker", "compose", "exec", "-T", MOODLE_SERVICE,
        "php", "admin/tool/uploaduser/cli/uploadusers.php",
        f"--file={container_csv}",
        "--delimiter=comma",
        f"--uutype={uutype}",
        "--uupasswordold=0",
        "--uubulk=0",
    ] + (extra_args or [])

    resultado = subprocess.run(cmd, cwd=DOCKER_COMPOSE_DIR, capture_output=True, text=True)
    log.info("CLI stdout:\n%s", resultado.stdout)
    if resultado.returncode != 0:
        log.error("CLI stderr:\n%s", resultado.stderr)
        raise RuntimeError("Falha na importação via CLI do Moodle.")


def criar_usuarios(usuarios: list[dict]) -> None:
    if not usuarios:
        log.info("Nenhum novo usuário para criar.")
        return

    campos = ["username", "firstname", "lastname", "email",
              "password", "auth", "forcepasswordchange", "suspended"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
    ) as f:
        csv_path = f.name
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        for u in usuarios:
            writer.writerow({
                "username":            u["username"],
                "firstname":           u["firstname"],
                "lastname":            u["lastname"],
                "email":               u["email"],
                "password":            gerar_senha(),
                "auth":                "manual",
                "forcepasswordchange": 1,
                "suspended":           0,
            })

    try:
        log.info("Criando %d novos usuários...", len(usuarios))
        _rodar_cli(csv_path, uutype=0)  # add new only
    finally:
        os.unlink(csv_path)


def atualizar_suspensos(usuarios: list[dict]) -> None:
    """Suspende ou reativa usuários existentes no Moodle."""
    if not usuarios:
        log.info("Nenhuma atualização de status necessária.")
        return

    campos = ["username", "suspended"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
    ) as f:
        csv_path = f.name
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        for u in usuarios:
            writer.writerow({"username": u["username"], "suspended": u["suspended"]})

    try:
        log.info("Atualizando status de %d usuários...", len(usuarios))
        _rodar_cli(csv_path, uutype=1)  # update existing
    finally:
        os.unlink(csv_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=== Início da sincronização ===")

    totvs  = buscar_totvs()
    sheets = buscar_planilha()
    moodle = buscar_moodle_existentes()

    # Mescla: TOTVS tem prioridade sobre Sheets para username duplicado
    externos: dict[str, dict] = {}
    for u in sheets + totvs:
        externos[u["username"].lower()] = u

    novos_ativos   = []  # ativos que NÃO existem no Moodle → criar
    para_suspender = []  # inativos que existem no Moodle e estão ativos → suspender
    para_reativar  = []  # ativos que existem no Moodle e estão suspensos → reativar

    for key, u in externos.items():
        existe      = key in moodle
        suspenso    = moodle.get(key, False)

        if u["ativo"]:
            if not existe:
                novos_ativos.append(u)
            elif suspenso:
                para_reativar.append({"username": u["username"], "suspended": 0})
        else:
            if existe and not suspenso:
                para_suspender.append({"username": u["username"], "suspended": 1})

    log.info(
        "Resumo → Criar: %d | Suspender: %d | Reativar: %d",
        len(novos_ativos), len(para_suspender), len(para_reativar),
    )

    criar_usuarios(novos_ativos)
    atualizar_suspensos(para_suspender + para_reativar)

    log.info("=== Sincronização concluída! ===")


if __name__ == "__main__":
    main()
