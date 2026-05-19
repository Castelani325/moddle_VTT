# Deploy Moodle VTT para o servidor 192.168.3.26
# Uso: .\deploy.ps1 -User <usuario-ssh>
# Exemplo: .\deploy.ps1 -User ubuntu

param(
    [Parameter(Mandatory=$true)]
    [string]$User,

    [string]$Server = "192.168.3.26",
    [string]$RemoteDir = "/opt/moodle_vtt"
)

$ErrorActionPreference = "Stop"

Write-Host "==> Sincronizando arquivos para $User@$Server`:$RemoteDir ..." -ForegroundColor Cyan

# Copia o projeto para o servidor (requer rsync via WSL ou Git Bash, ou usa scp como fallback)
$rsyncAvailable = $null -ne (Get-Command rsync -ErrorAction SilentlyContinue)

if ($rsyncAvailable) {
    rsync -avz --exclude='.git' --exclude='node_modules' --exclude='teste' `
        ./ "${User}@${Server}:${RemoteDir}/"
} else {
    Write-Host "rsync nao encontrado. Usando scp para copiar os arquivos-chave..." -ForegroundColor Yellow
    ssh "${User}@${Server}" "mkdir -p ${RemoteDir}"
    scp -r Dockerfile docker-compose.yml .env.example .docker `
        "${User}@${Server}:${RemoteDir}/"
    # Para copiar o projeto completo, instale rsync ou use git pull no servidor
    Write-Warning "Somente os arquivos Docker foram copiados. Para o codigo-fonte, use 'git clone' ou 'git pull' no servidor."
}

Write-Host "==> Configurando .env no servidor..." -ForegroundColor Cyan
ssh "${User}@${Server}" @"
    cd ${RemoteDir}
    if [ ! -f .env ]; then
        cp .env.example .env
        echo ''
        echo 'ATENCAO: Arquivo .env criado a partir do .env.example.'
        echo 'Verifique e ajuste as variaveis se necessario.'
    fi
"@

Write-Host "==> Iniciando build e container no servidor..." -ForegroundColor Cyan
ssh "${User}@${Server}" @"
    cd ${RemoteDir}
    docker compose down --remove-orphans
    docker compose build --no-cache
    docker compose up -d
    docker compose ps
"@

Write-Host ""
Write-Host "==> Deploy concluido!" -ForegroundColor Green
Write-Host "    Acesse: http://${Server}:8095" -ForegroundColor Green
