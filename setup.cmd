@echo off
rem ==========================================================================
rem  setup.cmd -- set Citadel up on this Windows machine, or check that it is.
rem
rem    setup              the demonstration machine: Docker Desktop + Ollama for Windows
rem    setup check        check everything, change nothing
rem    setup wsl2         the WSL2 distro configuration (docs\adr\0005)
rem    setup wsl2 -Check  check the WSL2 configuration, change nothing
rem    setup help         the options (-Yes, -NoStart, -Distro, -Port)
rem
rem  It says what it finds and asks before it installs, downloads or starts
rem  anything. SETUP.md has the same steps by hand. The work is done by
rem  ops\windows\setup.ps1, started here with -ExecutionPolicy Bypass for that
rem  one script only -- the machine's policy is not changed.
rem ==========================================================================
setlocal
cd /d "%~dp0"
where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo [setup] Windows PowerShell was not found; see SETUP.md to set Citadel up by hand.
  exit /b 1
)
set "SETUP_ARGS=%*"
if "%~1"=="/?" set "SETUP_ARGS=help"
if "%~1"=="-?" set "SETUP_ARGS=help"
if /i "%~1"=="-h" set "SETUP_ARGS=help"
if /i "%~1"=="--help" set "SETUP_ARGS=help"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\windows\setup.ps1" %SETUP_ARGS%
exit /b %errorlevel%
