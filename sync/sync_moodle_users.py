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
from collections import Counter
from datetime import date

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

# Relatório de senhas — acesso restrito (chmod 600 aplicado automaticamente)
CREDENTIALS_DIR = "/opt/moodle_sync/credentials"

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

    # Primeira consulta: diagnóstico — todos os registros sem filtro de email
    query_diag = """
        SELECT f.CHAPA, p.NOME, p.EMAIL, f.CODSITUACAO
        FROM PPESSOA p
        INNER JOIN PFUNC f ON f.CODPESSOA = p.CODIGO
    """

    # Contadores para diagnóstico
    total_rows        = 0
    sem_chapa         = 0
    sem_email         = 0
    email_vazio       = 0
    situacao_counter  = Counter()
    situacao_sem_email = Counter()
    amostras_sem_email = []

    usuarios = []

    try:
        with pyodbc.connect(_conn_str(TOTVS_SERVER, TOTVS_DB, TOTVS_USER, TOTVS_PASS)) as conn:
            rows = conn.cursor().execute(query_diag).fetchall()
            total_rows = len(rows)
            log.info("TOTVS RM: total bruto de linhas retornadas pela query: %d", total_rows)

            for row in rows:
                chapa    = str(row[0]).strip() if row[0] else ""
                nome     = str(row[1]).strip() if row[1] else ""
                email    = str(row[2]).strip() if row[2] else ""
                situacao = str(row[3]).strip().upper() if row[3] else ""

                situacao_counter[situacao or "(vazio)"] += 1

                if not chapa:
                    sem_chapa += 1
                    continue

                if row[2] is None:
                    sem_email += 1
                    situacao_sem_email[situacao or "(vazio)"] += 1
                    if len(amostras_sem_email) < 10:
                        amostras_sem_email.append(
                            f"CHAPA={chapa!r} NOME={nome!r} SITUACAO={situacao!r} EMAIL=NULL"
                        )
                    continue

                if email == "":
                    email_vazio += 1
                    situacao_sem_email[situacao or "(vazio)"] += 1
                    if len(amostras_sem_email) < 10:
                        amostras_sem_email.append(
                            f"CHAPA={chapa!r} NOME={nome!r} SITUACAO={situacao!r} EMAIL=''"
                        )
                    continue

                firstname, lastname = dividir_nome(nome)
                usuarios.append({
                    "username":  chapa,
                    "firstname": firstname,
                    "lastname":  lastname,
                    "email":     email,
                    "ativo":     situacao == "A",
                })

        # --- Relatório de diagnóstico ---
        log.info("TOTVS RM — distribuição por CODSITUACAO (todos os registros):")
        for sit, cnt in situacao_counter.most_common():
            log.info("  CODSITUACAO=%-10s → %d registros", sit, cnt)

        log.info(
            "TOTVS RM — descartados: sem CHAPA=%d | EMAIL NULL=%d | EMAIL vazio=%d",
            sem_chapa, sem_email, email_vazio,
        )

        if situacao_sem_email:
            log.info("TOTVS RM — CODSITUACAO dos registros sem e-mail:")
            for sit, cnt in situacao_sem_email.most_common():
                log.info("  CODSITUACAO=%-10s → %d sem e-mail", sit, cnt)

        if amostras_sem_email:
            log.info("TOTVS RM — amostra de registros ignorados (sem e-mail):")
            for amostra in amostras_sem_email:
                log.info("  %s", amostra)

        ativos   = sum(1 for u in usuarios if u["ativo"])
        inativos = sum(1 for u in usuarios if not u["ativo"])
        log.info(
            "TOTVS RM: %d funcionários carregados (ativos=%d | inativos=%d).",
            len(usuarios), ativos, inativos,
        )

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

    total_linhas  = len(linhas) - 1  # descontando cabeçalho
    sem_cpf       = 0
    sem_email     = 0
    situacao_counter = Counter()
    usuarios      = []

    for i, linha in enumerate(linhas[1:], start=2):
        if len(linha) < 14:
            log.warning("Linha %d: menos de 14 colunas (%d) — ignorada.", i, len(linha))
            continue

        chapa    = linha[1].strip()   # B
        cpf      = linha[2].strip()   # C
        nome     = linha[3].strip()   # D
        situacao = linha[5].strip()   # F
        email    = linha[13].strip()  # N

        situacao_counter[situacao.upper() or "(vazio)"] += 1

        if not cpf:
            sem_cpf += 1
            continue

        if not email:
            sem_email += 1
            log.debug("Linha %d: CPF=%s sem e-mail — ignorado.", i, cpf)
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

    log.info(
        "Google Sheets: %d linhas totais | sem CPF=%d | sem e-mail=%d | carregados=%d",
        total_linhas, sem_cpf, sem_email, len(usuarios),
    )
    log.info("Google Sheets — distribuição por Situação:")
    for sit, cnt in situacao_counter.most_common():
        log.info("  Situacao=%-10s → %d registros", sit, cnt)

    return usuarios


def buscar_moodle_existentes() -> dict[str, bool]:
    """Retorna usuários existentes no Moodle {username: esta_suspenso}."""

    # Primeiro verifica quais tabelas existem no banco Moodle
    query_tabelas = """
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
          AND TABLE_NAME LIKE '%user%'
        ORDER BY TABLE_NAME
    """

    usuarios = {}
    try:
        with pyodbc.connect(_conn_str(MOODLE_SERVER, MOODLE_DB, MOODLE_USER, MOODLE_PASS)) as conn:
            tabelas = [r[0] for r in conn.cursor().execute(query_tabelas).fetchall()]
            log.info("Moodle DB — tabelas com 'user' no nome: %s", tabelas)

            # Tenta mdl_user; se não existir, loga e retorna vazio
            tabela_user = "mdl_user" if "mdl_user" in tabelas else None
            if not tabela_user:
                log.warning(
                    "Tabela mdl_user não encontrada em %s. Nenhum usuário existente será considerado.",
                    MOODLE_DB,
                )
                return {}

            query = f"""
                SELECT username, suspended
                FROM {tabela_user}
                WHERE deleted = 0 AND username NOT IN ('guest', 'admin')
            """
            for row in conn.cursor().execute(query).fetchall():
                usuarios[str(row[0]).lower()] = bool(row[1])

        log.info("Moodle: %d usuários existentes.", len(usuarios))

    except Exception:
        log.exception("Erro ao consultar usuários existentes no Moodle.")
        raise

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
        "docker", "compose", "exec", "-T", "-w", "/var/www/html", MOODLE_SERVICE,
        "php", "/var/www/html/admin/tool/uploaduser/cli/uploaduser.php",
        f"--file={container_csv}",
        "--delimiter_name=comma",
        f"--uutype={uutype}",
        "--uupasswordold=0",
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

    campos = ["username", "firstname", "lastname", "email", "password", "auth", "suspended"]

    # Gera senhas e monta lista com credenciais em texto puro (salvas antes do envio ao Moodle)
    rows = []
    for u in usuarios:
        rows.append({
            "username":  u["username"],
            "firstname": u["firstname"],
            "lastname":  u["lastname"],
            "email":     u["email"],
            "password":  gerar_senha(),
            "auth":      "manual",
            "suspended": 0,
        })

    # Salva relatório de credenciais com acesso restrito
    os.makedirs(CREDENTIALS_DIR, exist_ok=True)
    cred_path = os.path.join(CREDENTIALS_DIR, f"{date.today()}_novos_usuarios.csv")
    cred_campos = ["username", "firstname", "lastname", "email", "senha_temporaria"]
    file_exists = os.path.exists(cred_path)
    with open(cred_path, "a", encoding="utf-8", newline="") as cf:
        writer_cred = csv.DictWriter(cf, fieldnames=cred_campos)
        if not file_exists:
            writer_cred.writeheader()
        for r in rows:
            writer_cred.writerow({
                "username":         r["username"],
                "firstname":        r["firstname"],
                "lastname":         r["lastname"],
                "email":            r["email"],
                "senha_temporaria": r["password"],
            })
    os.chmod(cred_path, 0o600)
    log.info("Credenciais salvas em %s (acesso restrito)", cred_path)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
    ) as f:
        csv_path = f.name
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        writer.writerows(rows)

    try:
        log.info("Criando %d novos usuários...", len(usuarios))
        # uuforcepasswordchange=2 → força troca de senha para todos os novos
        _rodar_cli(csv_path, uutype=0, extra_args=["--uuforcepasswordchange=2"])
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
        existe   = key in moodle
        suspenso = moodle.get(key, False)

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
