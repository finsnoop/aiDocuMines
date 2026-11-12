<#
.SYNOPSIS
  Build and start the whole aiDocuMines platform (API layer + Node backend + frontend)
  with one command.

.EXAMPLE
  ./deploy/platform-up.ps1            # build + start everything
  ./deploy/platform-up.ps1 -Down      # stop everything
  ./deploy/platform-up.ps1 -Logs      # follow logs

  Requires Docker Desktop. Expects the three repos side by side:
    GitHub/aiDocuMines            (this repo)
    GitHub/aiDocuMines-Backend
    GitHub/aiDocuMines-Frontend
  and a .env file in aiDocuMines and aiDocuMines-Backend.
#>
param(
  [switch]$Down,
  [switch]$Logs,
  [string]$BackendRepo = (Join-Path $PSScriptRoot '..\..\aiDocuMines-Backend'),
  [string]$FrontendRepo = (Join-Path $PSScriptRoot '..\..\aiDocuMines-Frontend')
)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $root

$env:BACKEND_REPO = (Resolve-Path $BackendRepo).Path
$env:FRONTEND_REPO = (Resolve-Path $FrontendRepo).Path
$env:BACKEND_ENV_FILE = Join-Path $env:BACKEND_REPO '.env'

$compose = @('compose', '-p', 'aidocumines', '-f', 'docker-compose.yml', '-f', 'deploy/docker-compose.platform.yml')

if ($Down) { docker @compose down; exit $LASTEXITCODE }
if ($Logs) { docker @compose logs -f --tail 100; exit $LASTEXITCODE }

foreach ($f in @((Join-Path $root '.env'), $env:BACKEND_ENV_FILE)) {
  if (-not (Test-Path $f)) { throw "Missing $f - copy it from your secrets store before starting." }
}

# The API stack joins CapRover's overlay network in production; locally a bridge will do.
docker network inspect captain-overlay-network *> $null
if ($LASTEXITCODE -ne 0) { docker network create --attachable captain-overlay-network | Out-Null }

docker @compose up -d --build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ''
Write-Host 'aiDocuMines is starting:' -ForegroundColor Green
Write-Host "  Frontend      http://localhost:$(if ($env:FRONTEND_PORT) { $env:FRONTEND_PORT } else { 4300 })"
Write-Host "  API layer     http://localhost:$(if ($env:API_PORT) { $env:API_PORT } else { 8020 })/api/v1/swagger/"
Write-Host "  Node backend  http://localhost:$(if ($env:BACKEND_PORT) { $env:BACKEND_PORT } else { 7000 })/api/db-health"
Write-Host '  Logs:  ./deploy/platform-up.ps1 -Logs     Stop:  ./deploy/platform-up.ps1 -Down'
