@echo off
rem ==========================================================================
rem  citadel.cmd -- run Citadel on this Windows machine with one command.
rem
rem    citadel              build (first time) and start everything, open the UI
rem    citadel models       download the approved models into Ollama (one time, ~7 GB)
rem    citadel stop         stop the containers (your documents and results stay)
rem    citadel status       what is running, is it healthy, are the models in
rem    citadel logs         follow the logs of every service (Ctrl-C to stop)
rem    citadel reset        stop AND delete all Citadel data (asks first)
rem
rem  Needs: Docker Desktop (running), and Ollama for Windows on this machine
rem  (http://127.0.0.1:11434) for the models -- it keeps the GPU; the containers
rem  reach it at host.docker.internal and nowhere else (docs\adr\0006).
rem
rem  Settings (optional, set before running): CITADEL_PORT (default 8000),
rem  CITADEL_REQUIRE_ENFORCEMENT=1 (refuse to start if the egress ruleset cannot
rem  be applied), CITADEL_DB_PASSWORD.
rem ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "COMPOSE=docker compose -f ops\compose\docker-compose.yml --project-name citadel"
if "%CITADEL_PORT%"=="" set "CITADEL_PORT=8000"
set "URL=http://127.0.0.1:%CITADEL_PORT%"
set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

where docker >nul 2>&1
if errorlevel 1 (
  echo [citadel] Docker is not installed. Install Docker Desktop for Windows, start it, then run citadel again.
  exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
  echo [citadel] Docker Desktop is not running. Start it ^(wait for "Engine running"^), then run citadel again.
  exit /b 1
)

if /i "%ACTION%"=="stop"   goto :stop
if /i "%ACTION%"=="status" goto :status
if /i "%ACTION%"=="logs"   goto :logs
if /i "%ACTION%"=="models" goto :models
if /i "%ACTION%"=="reset"  goto :reset
if /i "%ACTION%"=="start"  goto :start
echo [citadel] unknown command "%ACTION%" -- use start, models, stop, status, logs or reset
exit /b 2

:start
call :check_ollama
echo [citadel] building and starting (the first build downloads Python packages and the Postgres image; later starts take seconds)
%COMPOSE% up -d --build
if errorlevel 1 (
  echo [citadel] docker compose failed -- see the messages above. "citadel logs" shows each service.
  exit /b 1
)
echo [citadel] waiting for the API...
set /a tries=0
:wait
set /a tries+=1
set "code="
for /f %%c in ('curl.exe -s -o nul -w "%%{http_code}" %URL%/api/health 2^>nul') do set "code=%%c"
if "!code!"=="200" goto :ready
if !tries! GEQ 150 (
  echo [citadel] the API did not come up within 5 minutes. "citadel logs" will show why.
  exit /b 1
)
timeout /t 2 /nobreak >nul
goto :wait

:ready
echo.
echo [citadel] Citadel is running at %URL%
call :report_models
echo [citadel] Stop with: citadel stop
start "" "%URL%"
exit /b 0

:stop
%COMPOSE% down
exit /b %errorlevel%

:status
%COMPOSE% ps
set "code="
for /f %%c in ('curl.exe -s -o nul -w "%%{http_code}" %URL%/api/health 2^>nul') do set "code=%%c"
if "!code!"=="200" (
  echo [citadel] API healthy at %URL%
  call :report_models
) else (
  echo [citadel] API not answering at %URL%
  call :check_ollama
)
exit /b 0

:logs
%COMPOSE% logs -f --tail 200
exit /b %errorlevel%

:models
where ollama >nul 2>&1
if errorlevel 1 (
  echo [citadel] The ollama command is not on PATH. Install Ollama for Windows, or use the "Pull missing models" button in the UI.
  exit /b 1
)
echo [citadel] reading the approved models from registry\models.demo-local.yaml ...
set "TAGS="
for /f "usebackq delims=" %%t in (`%COMPOSE% run --rm --no-deps -T --entrypoint python init -c "from pathlib import Path; from citadel_platform.registry import load_registry; r = load_registry('demo-local', Path('/app/registry')); print(' '.join(m.tag for m in r.models if m.enabled))"`) do set "TAGS=%%t"
if "%TAGS%"=="" (
  echo [citadel] could not read the registry ^(does the image build? run "citadel" once first^)
  exit /b 1
)
for %%m in (%TAGS%) do (
  echo [citadel] ollama pull %%m
  ollama pull %%m
  if errorlevel 1 (
    echo [citadel] pulling %%m failed -- is Ollama running, and is this machine online for the download?
    exit /b 1
  )
)
echo [citadel] models ready. If Citadel is running, the demo documents are added within a minute.
exit /b 0

:reset
set /p "sure=This deletes every Citadel document, task, deliverable, key and the audit log. Type DELETE to confirm: "
if not "%sure%"=="DELETE" (
  echo [citadel] nothing deleted.
  exit /b 1
)
%COMPOSE% down -v
exit /b %errorlevel%

:check_ollama
set "ollama="
for /f %%c in ('curl.exe -s -o nul -w "%%{http_code}" http://127.0.0.1:11434/api/version 2^>nul') do set "ollama=%%c"
if "!ollama!"=="200" (
  echo [citadel] Ollama is answering on 127.0.0.1:11434.
) else (
  echo [citadel] WARNING: Ollama is not answering on 127.0.0.1:11434. Start the Ollama app ^(or "ollama serve"^).
  echo [citadel]          Citadel will start, but tasks fail with "no model is available" until it does.
)
exit /b 0

:report_models
rem Asks the running stack itself, with the same code the worker uses -- no guessing here.
set "MISSING="
set "LIST=%TEMP%\citadel-missing-models.txt"
%COMPOSE% exec -T api python -m citadel_worker missing-models > "%LIST%" 2>nul
set "rc=%errorlevel%"
if "%rc%"=="2" (
  echo [citadel] Ollama is not answering the containers, so no task can run yet. Start Ollama, then: citadel models
  exit /b 0
)
if not "%rc%"=="0" exit /b 0
for /f "usebackq delims=" %%t in ("%LIST%") do set "MISSING=!MISSING! %%t"
if defined MISSING (
  echo [citadel] Models not downloaded yet:!MISSING!
  echo [citadel] Run "citadel models" once ^(about 7 GB^). The demo documents are added as soon as they are in.
) else (
  echo [citadel] All approved models are installed. The demo documents are ingested in the background ^(a minute or two^).
)
exit /b 0
