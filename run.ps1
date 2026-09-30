<#
.SYNOPSIS
    Start Citadel -- both processes, with prerequisites checked first.

.DESCRIPTION
    Citadel runs as two OS processes on purpose (design doc section 2):

      * execution_service -- the ONLY holder of a Docker socket
      * app.main          -- the trusted workflow zone and the /ui console

    This script verifies what is actually needed, frees the ports if a previous
    run is still holding them, starts both, waits for each to report healthy,
    and prints where to go.

.PARAMETER Stop
    Stop both processes and exit.

.PARAMETER Check
    Verify prerequisites and exit without starting anything.

.PARAMETER StableSecrets
    Generate signing secrets once into var/secrets.env and reuse them on every
    later run, so restarting the server does not invalidate your session.
    DEVELOPMENT CONVENIENCE ONLY -- see the note where it is used.

.PARAMETER NoBrowser
    Do not open the console in a browser.

.EXAMPLE
    .\run.ps1
.EXAMPLE
    .\run.ps1 -Check
.EXAMPLE
    .\run.ps1 -Stop
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Check,
    [switch]$StableSecrets,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$Root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python     = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir     = Join-Path $Root 'var\logs'
$SecretFile = Join-Path $Root 'var\secrets.env'
$AppPort    = if ($env:CITADEL_SERVER_PORT) { [int]$env:CITADEL_SERVER_PORT } else { 8420 }
$ExecPort   = if ($env:CITADEL_EXECUTION_PORT) { [int]$env:CITADEL_EXECUTION_PORT } else { 8901 }

function Say  ($m) { Write-Host "  $m" }
function Ok   ($m) { Write-Host "  [ ok ] $m"   -ForegroundColor Green }
function Warn ($m) { Write-Host "  [warn] $m"   -ForegroundColor Yellow }
function Bad  ($m) { Write-Host "  [fail] $m"   -ForegroundColor Red }
function Head ($m) { Write-Host ""; Write-Host $m -ForegroundColor Cyan }

function Stop-Port([int]$Port, [string]$Label) {
    $killed = 0
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object {
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
            $killed++
        }
    if ($killed) { Ok "stopped $Label (port $Port)" } else { Say "$Label was not running" }
}

function Wait-Healthy([string]$Url, [int]$Seconds, [string]$Label) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Milliseconds 700 }
    }
    return $false
}

# ---------------------------------------------------------------- stop
if ($Stop) {
    Head "Stopping Citadel"
    Stop-Port $AppPort  'trusted zone'
    Stop-Port $ExecPort 'execution service'
    Write-Host ""
    exit 0
}

Write-Host ""
Write-Host "  CITADEL -- sovereign on-premise agentic AI workbench" -ForegroundColor White
Write-Host "  ---------------------------------------------------" -ForegroundColor DarkGray

# ---------------------------------------------------------------- prerequisites
Head "Checking prerequisites"
$fatal = @()

# Python virtualenv
if (-not (Test-Path $Python)) {
    Warn "no virtualenv at .venv -- creating one"
    try {
        & python -m venv (Join-Path $Root '.venv')
        & $Python -m pip install --quiet --upgrade pip
        & $Python -m pip install --quiet -r (Join-Path $Root 'requirements-dev.txt')
        Ok "virtualenv created and dependencies installed"
    } catch {
        $fatal += "could not create the virtualenv: $_"
    }
} else {
    $v = (& $Python --version 2>&1)
    Ok "python $v"
    # Cheap import check: a missing dependency here is far clearer than a
    # traceback thirty seconds into startup.
    & $Python -c "import fastapi, uvicorn, sqlalchemy, pydantic, jwt, bcrypt, httpx, docker, typer" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Warn "dependencies missing or out of date -- installing from requirements-dev.txt"
        & $Python -m pip install --quiet -r (Join-Path $Root 'requirements-dev.txt')
        Ok "dependencies installed"
    } else {
        Ok "dependencies present"
    }
}

# Docker -- needed by the execution zone, which is the one real security boundary
$dockerExe = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $dockerExe) {
    $candidate = "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe"
    if (Test-Path $candidate) { $dockerExe = $candidate }
}
if (-not $dockerExe) {
    $fatal += "Docker CLI not found. Install Docker Desktop: https://docs.docker.com/desktop/"
} else {
    $serverVersion = & $dockerExe version --format '{{.Server.Version}}' 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $serverVersion) {
        $fatal += "Docker is installed but the daemon is not running. Start Docker Desktop and wait for it to finish starting (it can take a minute or two), then re-run."
    } else {
        Ok "docker daemon $serverVersion"
    }
}

# Ollama -- local inference. No cloud calls are made, by design.
try {
    $tags = Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 5
    $models = @($tags.models.name)
    Ok "ollama serving ($($models.Count) models)"
    foreach ($needed in 'hermes3', 'nomic-embed-text') {
        if ($models -match "^$needed") { Ok "model $needed" }
        else { $fatal += "model '$needed' not pulled. Run:  ollama pull $needed" }
    }
} catch {
    $fatal += "Ollama is not responding on localhost:11434. Install from https://ollama.com/ and run 'ollama serve', then: ollama pull hermes3 && ollama pull nomic-embed-text"
}

if ($fatal.Count) {
    Head "Cannot start"
    foreach ($f in $fatal) { Bad $f }
    Write-Host ""
    exit 1
}

if ($Check) { Head "All prerequisites satisfied."; Write-Host ""; exit 0 }

# ---------------------------------------------------------------- secrets
# Unset signing secrets mean a random per-process key, so restarting the server
# invalidates every session. That is the correct fail-closed default and no dev
# secret is committed. -StableSecrets trades it for convenience during a demo.
if ($StableSecrets) {
    New-Item -ItemType Directory -Force -Path (Split-Path $SecretFile) | Out-Null
    if (-not (Test-Path $SecretFile)) {
        $gen = {
            $b = New-Object byte[] 48
            [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
            [Convert]::ToBase64String($b)
        }
        @("CITADEL_SESSION_SECRET=$(& $gen)", "CITADEL_CAPABILITY_SECRET=$(& $gen)") |
            Set-Content -Path $SecretFile -Encoding utf8
        Ok "generated var/secrets.env (gitignored, development only)"
    }
    Get-Content $SecretFile | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path "env:$($Matches[1])" -Value $Matches[2] }
    }
    Ok "stable signing secrets loaded -- sessions survive a restart"
} else {
    Say "signing secrets are per-process (restarting the server ends your session)"
    Say "use -StableSecrets to keep sessions across restarts during a demo"
}

# ---------------------------------------------------------------- start
Head "Starting"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Stop-Port $ExecPort 'execution service'
Stop-Port $AppPort  'trusted zone'
Start-Sleep -Seconds 1

# 1. Isolated execution zone. Started first: the trusted zone's python.execute
#    calls fail fast and confusingly if nothing is listening for them.
Start-Process -FilePath $Python -ArgumentList '-m', 'execution_service' `
    -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir 'execution_service.log') `
    -RedirectStandardError  (Join-Path $LogDir 'execution_service.err') | Out-Null

if (Wait-Healthy "http://127.0.0.1:$ExecPort/health" 45 'execution service') {
    Ok "execution service   http://127.0.0.1:$ExecPort  (the only Docker-socket holder)"
} else {
    Bad "execution service did not become healthy -- see var/logs/execution_service.err"
    Get-Content (Join-Path $LogDir 'execution_service.err') -Tail 15 -ErrorAction SilentlyContinue
    exit 1
}

# 2. Trusted workflow zone. On first start it seeds the demo personas and
#    ingests the demo corpus, so this can take a few seconds longer.
Start-Process -FilePath $Python -ArgumentList '-m', 'app.main' `
    -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir 'app.log') `
    -RedirectStandardError  (Join-Path $LogDir 'app.err') | Out-Null

if (Wait-Healthy "http://127.0.0.1:$AppPort/ui" 90 'trusted zone') {
    Ok "trusted zone        http://127.0.0.1:$AppPort"
} else {
    Bad "trusted zone did not become healthy -- see var/logs/app.err"
    Get-Content (Join-Path $LogDir 'app.err') -Tail 15 -ErrorAction SilentlyContinue
    exit 1
}

# ---------------------------------------------------------------- ready
Head "Citadel is running"
Write-Host ""
Write-Host "  Console      http://127.0.0.1:$AppPort/ui" -ForegroundColor White
Write-Host ""
Write-Host "  Sign in as   j.rao / engineer-pw    submits tasks"        -ForegroundColor DarkGray
Write-Host "               a.singh / approver-pw  releases artifacts"   -ForegroundColor DarkGray
Write-Host "               s.mehta / admin-pw     kill-switch"          -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Or drive it from a shell:" -ForegroundColor DarkGray
Write-Host "    .venv\Scripts\python.exe -m cli login" -ForegroundColor DarkGray
Write-Host "    .venv\Scripts\python.exe -m cli task ""...""  --classification CONFIDENTIAL" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Logs   var\logs\      Stop   .\run.ps1 -Stop" -ForegroundColor DarkGray
Write-Host ""

if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$AppPort/ui" }

# Explicit, so anything wrapping this script sees a clean success.
exit 0
