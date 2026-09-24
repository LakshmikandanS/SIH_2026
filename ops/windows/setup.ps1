<#
  ops\windows\setup.ps1 -- set Citadel up on this Windows machine.

  Run it through setup.cmd in the repository root, which starts Windows PowerShell with
  -ExecutionPolicy Bypass for this one script (nothing on the machine is changed by that):

    setup                  the demonstration machine: Docker Desktop + Ollama for Windows
    setup check            check everything, change nothing
    setup wsl2             the WSL2 distro configuration (docs\adr\0005)
    setup wsl2 -Check      check the WSL2 configuration, change nothing
    setup help

  Options:
    -Yes             answer yes to every question (unattended)
    -NoStart         prepare everything, but do not start Citadel
    -Distro <name>   the WSL2 distro (default: citadel)
    -Port <n>        the browser port (default: CITADEL_PORT, else 8000)

  Every step says what it found. It asks before it installs, downloads or starts
  anything. SETUP.md describes the same steps by hand.

  Written for Windows PowerShell 5.1 (every Windows 10 and 11 has it); pure ASCII, so
  it reads the same whatever the console's code page.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('demo', 'check', 'wsl2', 'help')]
    [string]$Action = 'demo',
    [switch]$Check,
    [switch]$Yes,
    [switch]$NoStart,
    [string]$Distro = 'citadel',
    [int]$Port = 0
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
# Every HTTP call below goes to this machine; a system proxy must not see it.
[System.Net.WebRequest]::DefaultWebProxy = $null
# wsl.exe answers in UTF-16 unless told otherwise.
$env:WSL_UTF8 = '1'

if ($Action -eq 'check') {
    $Action = 'demo'
    $Check = $true
}
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if ($Port -le 0) {
    $Port = 8000
    $fromEnv = 0
    if ($env:CITADEL_PORT -and [int]::TryParse($env:CITADEL_PORT, [ref]$fromEnv) -and $fromEnv -gt 0) { $Port = $fromEnv }
}
$BaseUrl = 'http://127.0.0.1:' + $Port
$OllamaUrl = 'http://127.0.0.1:11434'
$OllamaDir = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'
$DockerDesktopExe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
$DockerBundledCli = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
$Results = New-Object System.Collections.Generic.List[object]
$script:RestartWindows = $false
$script:DockerExe = $null

# ------------------------------------------------------------------ output and questions

function Write-Section([string]$Title) {
    Write-Host ''
    Write-Host ('== ' + $Title) -ForegroundColor White
}

function Clear-Problem([string]$Name) {
    # Fixed along the way (started, installed, downloaded): the summary lists only what
    # is still wrong. The lines printed above stay as the record of what happened.
    foreach ($stale in @($Results | Where-Object { $_.Name -eq $Name -and ($_.Status -eq 'WARN' -or $_.Status -eq 'FAIL') })) {
        [void]$Results.Remove($stale)
    }
}

function Add-Result([string]$Status, [string]$Name, [string]$Detail, [string]$Fix = '') {
    if ($Status -eq 'OK') { Clear-Problem $Name }
    $Results.Add([pscustomobject]@{ Status = $Status; Name = $Name; Detail = $Detail; Fix = $Fix })
    $colors = @{ OK = 'Green'; WARN = 'Yellow'; FAIL = 'Red'; INFO = 'Cyan'; SKIP = 'DarkGray' }
    Write-Host ('  [{0,-4}] {1}: {2}' -f $Status, $Name, $Detail) -ForegroundColor $colors[$Status]
    if ($Fix) { Write-Host ('         -> ' + $Fix) -ForegroundColor DarkGray }
}

function Ask([string]$Question, [bool]$Default = $true) {
    if ($Check) { return $false }
    if ($Yes) {
        Write-Host ('  ' + $Question + ' [yes, -Yes]')
        return $true
    }
    if ($Default) { $hint = '[Y/n]' } else { $hint = '[y/N]' }
    while ($true) {
        $answer = Read-Host ('  ' + $Question + ' ' + $hint)
        if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
        $answer = $answer.Trim().ToLowerInvariant()
        if ($answer -eq 'y' -or $answer -eq 'yes') { return $true }
        if ($answer -eq 'n' -or $answer -eq 'no') { return $false }
        Write-Host '  Please answer y or n.'
    }
}

function Wait-Until([scriptblock]$Condition, [int]$Seconds, [string]$What) {
    Write-Host ('  waiting for {0} (up to {1} s)' -f $What, $Seconds) -NoNewline
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) {
            Write-Host ' ready'
            return $true
        }
        Write-Host '.' -NoNewline
        Start-Sleep -Seconds 3
    }
    Write-Host ' gave up'
    return $false
}

# ------------------------------------------------------------------ running things

function Invoke-Native([string]$File, [string[]]$Arguments = @()) {
    # A native command's output and exit code, never an exception: Windows PowerShell
    # turns a native command's stderr into errors, which must not stop this script.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 1
    $lines = @()
    try {
        $lines = @(& $File @Arguments 2>&1 | ForEach-Object { [string]$_ })
        $code = $LASTEXITCODE
    } catch {
        $lines = @([string]$_)
        $code = 1
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($null -eq $code) { $code = 0 }
    $text = (($lines | Where-Object { $null -ne $_ }) -join "`n") -replace "`0", ''
    return [pscustomobject]@{ Code = $code; Text = $text.Trim() }
}

function Invoke-Interactive([string]$File, [string]$ArgumentLine = '') {
    # For commands a person watches or answers (downloads with progress bars, sudo's
    # password prompt, Ubuntu's first run): the child shares this console directly, and
    # only its exit code comes back -- never its output, which inside a function would
    # otherwise be mixed into the function's return value.
    try {
        if ($ArgumentLine) {
            $p = Start-Process -FilePath $File -ArgumentList $ArgumentLine -NoNewWindow -PassThru -ErrorAction Stop
        } else {
            $p = Start-Process -FilePath $File -NoNewWindow -PassThru -ErrorAction Stop
        }
    } catch {
        Write-Host ('  could not run {0}: {1}' -f $File, $_) -ForegroundColor Red
        return 1
    }
    $null = $p.Handle   # Windows PowerShell only keeps ExitCode readable if the handle was taken
    $p.WaitForExit()
    return $p.ExitCode
}

function Get-Json([string]$Url, [hashtable]$Headers = @{}, [int]$Timeout = 5) {
    try {
        return Invoke-RestMethod -Uri $Url -Headers $Headers -TimeoutSec $Timeout -UseBasicParsing -ErrorAction Stop
    } catch {
        return $null
    }
}

function Send-Json([string]$Url, [hashtable]$Body, [int]$Timeout = 10) {
    try {
        $json = $Body | ConvertTo-Json -Compress
        return Invoke-RestMethod -Uri $Url -Method Post -ContentType 'application/json' -Body $json -TimeoutSec $Timeout -UseBasicParsing -ErrorAction Stop
    } catch {
        return $null
    }
}

function Install-WithWinget([string]$Id, [string]$Label) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    if (-not (Ask ('Install {0} now with winget ({1})?' -f $Label, $Id) $true)) { return $false }
    $code = Invoke-Interactive $winget.Source ('install --exact --id {0} --accept-package-agreements --accept-source-agreements' -f $Id)
    return ($code -eq 0)
}

function Test-Citadel {
    $health = Get-Json ($BaseUrl + '/api/health') @{} 3
    return ($null -ne $health -and $health.service -eq 'citadel-api')
}

# ------------------------------------------------------------------ this machine

function Test-Repository {
    Write-Section 'The repository'
    foreach ($relative in @('citadel.cmd', 'ops\compose\docker-compose.yml', 'registry\models.demo-local.yaml', 'ops\wsl2\provision.sh')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $relative))) {
            Add-Result FAIL 'Repository' ('{0} is missing under {1}' -f $relative, $Root) 'Run setup.cmd from the Citadel repository folder.'
            return $false
        }
    }
    Add-Result OK 'Repository' $Root
    if ($Root -match '\s') {
        Add-Result WARN 'Repository path' 'contains a space' 'Docker copes, but a path without spaces (C:\AI_WORKBENCH\SIH_2026) avoids surprises.'
    }
    return $true
}

function Test-WindowsHost {
    Write-Section 'This machine'
    $os = Get-CimInstance Win32_OperatingSystem
    $build = [int]$os.BuildNumber
    $label = '{0} (build {1})' -f $os.Caption, $build
    if (-not [Environment]::Is64BitOperatingSystem) {
        Add-Result FAIL 'Windows' ($label + ', 32-bit') 'Citadel needs 64-bit Windows 10 22H2 or Windows 11.'
    } elseif ($build -lt 19045) {
        Add-Result WARN 'Windows' $label 'Docker Desktop and WSL 2 want Windows 10 22H2 (build 19045) or Windows 11; run Windows Update.'
    } else {
        Add-Result OK 'Windows' $label
    }
    $cs = Get-CimInstance Win32_ComputerSystem
    $ramGb = [math]::Round($cs.TotalPhysicalMemory / 1GB)
    if ($ramGb -lt 12) {
        Add-Result WARN 'Memory' ('{0} GB' -f $ramGb) '16 GB is comfortable: Docker, Postgres, OCR and the browser share it with Ollama.'
    } else {
        Add-Result OK 'Memory' ('{0} GB' -f $ramGb)
    }
    Add-Result INFO 'Processor' ('{0} logical processors' -f $cs.NumberOfLogicalProcessors)
    $systemDrive = $env:SystemDrive
    $disk = Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='{0}'" -f $systemDrive)
    if ($disk) {
        $freeGb = [math]::Round($disk.FreeSpace / 1GB, 1)
        $why = 'Docker Desktop keeps its disk, and Ollama its models, on {0} by default: about 12 GB in all.' -f $systemDrive
        if ($freeGb -lt 12) {
            Add-Result FAIL ('Free space on ' + $systemDrive) ('{0} GB' -f $freeGb) $why
        } elseif ($freeGb -lt 20) {
            Add-Result WARN ('Free space on ' + $systemDrive) ('{0} GB' -f $freeGb) $why
        } else {
            Add-Result OK ('Free space on ' + $systemDrive) ('{0} GB' -f $freeGb)
        }
    }
}

function Test-Gpu {
    $smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if (-not $smi) {
        Add-Result WARN 'GPU' 'no NVIDIA driver found (nvidia-smi)' 'Install the current NVIDIA driver for your card (nvidia.com/drivers). Without a GPU, Ollama runs on the CPU, slowly.'
        return
    }
    $r = Invoke-Native $smi.Source @('--query-gpu=name,driver_version,memory.total', '--format=csv,noheader,nounits')
    if ($r.Code -ne 0 -or -not $r.Text) {
        Add-Result WARN 'GPU' ('nvidia-smi failed: ' + $r.Text) 'Reinstall or update the NVIDIA driver.'
        return
    }
    $parts = (($r.Text -split "`n")[0]) -split ',\s*'
    $name = $parts[0]
    $driver = ''
    if ($parts.Count -gt 1) { $driver = $parts[1] }
    $vramMb = 0
    if ($parts.Count -gt 2) { [void][int]::TryParse($parts[2].Trim(), [ref]$vramMb) }
    $vramGb = [math]::Round($vramMb / 1024, 1)
    $detail = '{0}, {1} GB, driver {2}' -f $name, $vramGb, $driver
    if ($vramMb -gt 0 -and $vramMb -lt 7500) {
        Add-Result WARN 'GPU' $detail 'The approved models are sized for an 8 GB card; with less, Ollama splits them between GPU and CPU (slower).'
    } else {
        Add-Result OK 'GPU' $detail
    }
}

function Test-Wsl([bool]$Required) {
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    $status = $null
    if ($wsl) { $status = Invoke-Native $wsl.Source @('--status') }
    if ($null -eq $status -or $status.Code -ne 0) {
        if ($Required) { $level = 'FAIL' } else { $level = 'WARN' }
        Add-Result $level 'WSL 2' 'not installed' 'In PowerShell as Administrator: wsl --install --no-distribution, then restart Windows.'
        if (Ask 'Install WSL 2 now? Windows asks for administrator rights; restart Windows afterwards.' $true) {
            $p = $null
            try {
                $p = Start-Process -FilePath 'wsl.exe' -ArgumentList '--install --no-distribution' -Verb RunAs -Wait -PassThru -ErrorAction Stop
            } catch {
                Add-Result FAIL 'WSL 2' 'the install did not start (administrator rights refused?)' 'Run as Administrator: wsl --install --no-distribution'
                return $false
            }
            if ($p -and $p.ExitCode -eq 0) {
                Add-Result INFO 'WSL 2' 'installed: restart Windows, then run setup again'
                $script:RestartWindows = $true
            } else {
                Add-Result FAIL 'WSL 2' ('the install exited with {0}' -f $p.ExitCode) 'On older Windows builds run: wsl --install (as Administrator), then restart.'
            }
        }
        return $false
    }
    if ($status.Text -match 'Default Version:\s*1') {
        Add-Result WARN 'WSL 2' 'installed, but new distros default to WSL 1' 'Run: wsl --set-default-version 2'
    } else {
        $version = Invoke-Native $wsl.Source @('--version')
        $first = ''
        if ($version.Code -eq 0 -and $version.Text) { $first = ($version.Text -split "`n")[0].Trim() }
        if (-not $first) { $first = 'installed' }
        Add-Result OK 'WSL 2' $first
    }
    return $true
}

# ------------------------------------------------------------------ Docker Desktop

function Get-DockerExe {
    $cmd = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if (Test-Path -LiteralPath $DockerBundledCli) { return $DockerBundledCli }
    return $null
}

function Test-Docker {
    Write-Section 'Docker Desktop'
    $script:DockerExe = Get-DockerExe
    if (-not $script:DockerExe -and -not (Test-Path -LiteralPath $DockerDesktopExe)) {
        Add-Result FAIL 'Docker Desktop' 'not installed' 'Install Docker Desktop for Windows (docker.com/products/docker-desktop), keep "Use WSL 2" ticked, start it once and accept its terms.'
        if (Install-WithWinget 'Docker.DockerDesktop' 'Docker Desktop') {
            Add-Result INFO 'Docker Desktop' 'installed: start it from the Start menu, accept its terms, wait for "Engine running", then run setup again'
        }
        return $false
    }
    if (-not $script:DockerExe) {
        Add-Result WARN 'Docker CLI' 'not on PATH yet' 'Start Docker Desktop once (it adds docker to PATH), then open a new window.'
    }
    $format = '{{.ServerVersion}}|{{.OperatingSystem}}|{{.MemTotal}}|{{.NCPU}}'
    $info = $null
    if ($script:DockerExe) { $info = Invoke-Native $script:DockerExe @('info', '--format', $format) }
    if ($null -eq $info -or $info.Code -ne 0) {
        Add-Result WARN 'Docker engine' 'Docker Desktop is not running' 'Start Docker Desktop and wait for "Engine running".'
        if ((Test-Path -LiteralPath $DockerDesktopExe) -and (Ask 'Start Docker Desktop now and wait for its engine?' $true)) {
            Start-Process -FilePath $DockerDesktopExe | Out-Null
            $ready = Wait-Until {
                $exe = Get-DockerExe
                ($null -ne $exe) -and ((Invoke-Native $exe @('info', '--format', '{{.ServerVersion}}')).Code -eq 0)
            } 300 'the Docker engine'
            if ($ready) {
                $script:DockerExe = Get-DockerExe
                $info = Invoke-Native $script:DockerExe @('info', '--format', $format)
            }
        }
        if ($null -eq $info -or $info.Code -ne 0) {
            Add-Result FAIL 'Docker engine' 'not running' 'Open Docker Desktop; if it never reaches "Engine running", see SETUP.md troubleshooting.'
            return $false
        }
    }
    # The formatted line; any warning docker prints goes to its own lines.
    $line = @($info.Text -split "`n" | Where-Object { $_ -match '\|' } | Select-Object -First 1)
    $fields = @()
    if ($line.Count -gt 0) { $fields = @($line[0].Trim() -split '\|') }
    $memGb = 0
    if ($fields.Count -gt 2) {
        $memBytes = [double]0
        [void][double]::TryParse($fields[2], [ref]$memBytes)
        $memGb = [math]::Round($memBytes / 1GB, 1)
    }
    if ($fields.Count -gt 3) {
        Add-Result OK 'Docker engine' ('{0} on {1}, {2} GB, {3} CPUs' -f $fields[0], $fields[1], $memGb, $fields[3])
    } else {
        Add-Result OK 'Docker engine' 'running'
    }
    if ($memGb -gt 0 -and $memGb -lt 6) {
        Add-Result WARN 'Docker memory' ('{0} GB for containers' -f $memGb) 'Give WSL more: in %UserProfile%\.wslconfig set [wsl2] memory=8GB, then run wsl --shutdown and restart Docker Desktop.'
    }
    $compose = Invoke-Native $script:DockerExe @('compose', 'version', '--short')
    if ($compose.Code -ne 0) {
        Add-Result FAIL 'Docker Compose' 'not available' 'Update Docker Desktop: citadel.cmd needs "docker compose" (v2).'
        return $false
    }
    Add-Result OK 'Docker Compose' $compose.Text
    return $true
}

# ------------------------------------------------------------------ Ollama for Windows

function Get-OllamaExe {
    $cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidate = Join-Path $OllamaDir 'ollama.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}

function Test-Ollama {
    Write-Section 'Ollama for Windows'
    $exe = Get-OllamaExe
    if (-not $exe) {
        Add-Result FAIL 'Ollama' 'not installed' 'Install Ollama for Windows (ollama.com/download/windows). It runs in the tray and keeps the GPU.'
        if (Install-WithWinget 'Ollama.Ollama' 'Ollama') { $exe = Get-OllamaExe }
        if (-not $exe) { return $null }
        Add-Result OK 'Ollama' ('installed: ' + $exe)
    }
    $version = Get-Json ($OllamaUrl + '/api/version')
    if (-not $version) {
        Add-Result WARN 'Ollama service' 'not answering on 127.0.0.1:11434' 'Start the Ollama app from the Start menu, or run: ollama serve'
        if (Ask 'Start Ollama now?' $true) {
            $app = Join-Path $OllamaDir 'ollama app.exe'
            if (Test-Path -LiteralPath $app) {
                Start-Process -FilePath $app | Out-Null
            } else {
                Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
            }
            if (Wait-Until { $null -ne (Get-Json ($OllamaUrl + '/api/version') @{} 2) } 60 'Ollama') {
                $version = Get-Json ($OllamaUrl + '/api/version')
            }
        }
        if (-not $version) {
            Add-Result FAIL 'Ollama service' 'not answering' 'Start Ollama, then run setup again.'
            return $exe
        }
    }
    Add-Result OK 'Ollama service' ('version {0} on 127.0.0.1:11434' -f $version.version)
    if ($env:OLLAMA_HOST -and $env:OLLAMA_HOST -match '0\.0\.0\.0') {
        Add-Result WARN 'OLLAMA_HOST' $env:OLLAMA_HOST 'Docker Desktop reaches Ollama on 127.0.0.1; listening on 0.0.0.0 only exposes it to your network. Remove the variable and restart Ollama.'
    }
    return $exe
}

function Test-CitadelPort {
    Write-Section ('Port ' + $Port)
    $listening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listening.Count -eq 0) {
        Add-Result OK ('Port ' + $Port) 'free'
        return
    }
    if (Test-Citadel) {
        Add-Result OK ('Port ' + $Port) 'Citadel is already running here'
        return
    }
    $owner = Get-Process -Id $listening[0].OwningProcess -ErrorAction SilentlyContinue
    $who = 'another program'
    if ($owner) { $who = '{0} (pid {1})' -f $owner.ProcessName, $owner.Id }
    Add-Result WARN ('Port ' + $Port) ('in use by ' + $who) 'Stop that program, or pick another port: set CITADEL_PORT=8080, then run setup and citadel from that same window.'
}

# ------------------------------------------------------------------ the approved models

function Get-ApprovedModels {
    # The registry is the only place a model is named (AGENTS.md, invariant 1): read the
    # entries with enabled: true from it rather than knowing them here.
    $file = Join-Path $Root 'registry\models.demo-local.yaml'
    $models = New-Object System.Collections.Generic.List[object]
    $current = $null
    foreach ($line in (Get-Content -LiteralPath $file -Encoding UTF8)) {
        if ($line -match '^\s*-\s+id:\s*([^\s#]+)') {
            if ($current) { $models.Add([pscustomobject]$current) }
            $current = @{ Id = $Matches[1]; Tag = $null; Enabled = $false }
        } elseif ($current -and $line -match '^\s+enabled:\s*(true|false)') {
            $current.Enabled = ($Matches[1] -eq 'true')
        } elseif ($current -and $line -match '^\s+tag:\s*"?([^"\s#]+)"?') {
            $current.Tag = $Matches[1]
        }
    }
    if ($current) { $models.Add([pscustomobject]$current) }
    return @($models | Where-Object { $_.Enabled -and $_.Tag })
}

function Get-NormalTag([string]$Tag) {
    if ($Tag -notmatch ':') { $Tag = $Tag + ':latest' }
    return $Tag.ToLowerInvariant()
}

function Sync-Models([string]$OllamaExe) {
    Write-Section 'The approved models (registry\models.demo-local.yaml, enabled: true)'
    $approved = @(Get-ApprovedModels)
    if ($approved.Count -eq 0) {
        Add-Result WARN 'Models' 'the registry enables no model' 'See registry\models.demo-local.yaml.'
        return
    }
    $tags = Get-Json ($OllamaUrl + '/api/tags') @{} 10
    if (-not $tags) {
        Add-Result WARN 'Models' 'cannot list them: Ollama is not answering' 'Start Ollama, then run setup again (or: citadel models).'
        return
    }
    $installed = @{}
    foreach ($m in @($tags.models)) { $installed[(Get-NormalTag $m.name)] = $true }
    $missing = @()
    foreach ($m in $approved) {
        if ($installed.ContainsKey((Get-NormalTag $m.Tag))) {
            Add-Result OK ('Model ' + $m.Id) ($m.Tag + ' installed')
        } else {
            Add-Result WARN ('Model ' + $m.Id) ($m.Tag + ' not downloaded') 'setup downloads it with your OK, or: citadel models'
            $missing += $m
        }
    }
    if ($missing.Count -eq 0) { return }
    if (-not $OllamaExe) { return }
    $list = ($missing | ForEach-Object { $_.Tag }) -join ', '
    $question = 'Download the {0} approved model(s) not installed yet ({1})? Several GB; Ollama shows each size as it goes.' -f $missing.Count, $list
    if (-not (Ask $question $true)) { return }
    foreach ($m in $missing) {
        Write-Host ('  ollama pull ' + $m.Tag)
        $code = Invoke-Interactive $OllamaExe ('pull ' + $m.Tag)
        if ($code -ne 0) {
            Add-Result FAIL ('Model ' + $m.Id) ('the download of {0} failed' -f $m.Tag) 'Check the connection (models are the one download that needs the internet), then run setup again.'
            return
        }
        Add-Result OK ('Model ' + $m.Id) ($m.Tag + ' downloaded')
    }
}

# ------------------------------------------------------------------ start and verify

function Start-DemoStack {
    Write-Section 'Start Citadel'
    if (Test-Citadel) {
        Add-Result OK 'Citadel' ('already running at ' + $BaseUrl)
        return $true
    }
    if ($Check -or $NoStart) {
        Add-Result SKIP 'Citadel' 'not started (-Check or -NoStart)' 'Run: citadel'
        return $false
    }
    if (-not (Ask 'Build and start Citadel now? The first build downloads packages and takes a few minutes.' $true)) {
        Add-Result SKIP 'Citadel' 'not started' 'Run: citadel'
        return $false
    }
    $env:CITADEL_PORT = [string]$Port
    $code = Invoke-Interactive 'cmd.exe' ('/c "' + (Join-Path $Root 'citadel.cmd') + '"')
    if ($code -ne 0) {
        Add-Result FAIL 'Citadel' 'did not start' 'Run: citadel logs   (and see SETUP.md troubleshooting)'
        return $false
    }
    return $true
}

function Test-RunningCitadel([string]$Where, [string]$OllamaFix, [string]$ModelsFix) {
    Write-Section ('Citadel at ' + $BaseUrl)
    $health = Get-Json ($BaseUrl + '/api/health') @{} 5
    if (-not $health) {
        Add-Result SKIP 'Citadel' 'not running, so not verified' ('Start it (' + $Where + '), then run setup check.')
        return
    }
    Add-Result OK 'API' ('profile {0}, sovereign {1}, sandbox {2}' -f $health.profile, $health.sovereign, $health.sandbox)
    if ($health.sandbox -ne 'container') {
        Add-Result WARN 'Sandbox' ('kind ' + $health.sandbox) 'This is the no-container developer path (scripts/run.sh); the demonstration runs the sandbox container.'
    }
    $session = Send-Json ($BaseUrl + '/api/auth/session') @{ user_id = 'demo-engineer-1' }
    if (-not $session -or -not $session.token) {
        Add-Result WARN 'Sign-in' 'the demonstration sign-in did not answer' 'Open the browser and sign in by hand.'
        return
    }
    $headers = @{ Authorization = 'Bearer ' + $session.token }
    $panel = Get-Json ($BaseUrl + '/api/sovereignty') $headers 15
    if ($panel) {
        $observed = 0
        [void][int]::TryParse([string]$panel.status.counts.observed, [ref]$observed)
        if ($observed -eq 0) {
            Add-Result OK 'Egress' 'no external connection observed'
        } else {
            Add-Result FAIL 'Egress' ('{0} external connection(s) observed' -f $observed) 'Open the Sovereignty panel: it names the process and task.'
        }
        if ($panel.enforcement.applied -eq $true) {
            Add-Result OK 'Egress ruleset' 'applied in the api container'
        } else {
            # The container's own words: why nft refused, or that this is not the container deployment.
            $why = ([string]$panel.enforcement.note).Trim()
            if ($why.Length -gt 200) { $why = $why.Substring(0, 197) + '...' }
            if (-not $why) { $why = 'not applied' }
            Add-Result WARN 'Egress ruleset' $why 'See SETUP.md, "The egress ruleset is not applied".'
        }
    }
    $models = Get-Json ($BaseUrl + '/api/models') $headers 20
    if ($models -and $models.reachable -eq $false) {
        Add-Result FAIL 'Inference runtime' ('Citadel cannot reach Ollama at ' + $models.endpoint) $OllamaFix
    } elseif ($models) {
        $missing = @($models.missing | Where-Object { $_ })
        if ($missing.Count -eq 0) {
            Add-Result OK 'Models, as Citadel sees them' 'every approved model is installed'
        } else {
            Add-Result WARN 'Models, as Citadel sees them' ('missing: ' + ($missing -join ', ')) $ModelsFix
        }
    }
    $docs = Get-Json ($BaseUrl + '/api/documents') $headers 15
    if ($docs) {
        $all = @($docs.documents)
        $ready = @($all | Where-Object { $_.status -eq 'ready' }).Count
        if ($all.Count -eq 0) {
            Add-Result INFO 'Demo documents' 'not added yet: the worker adds them once the approved models are installed'
        } elseif ($ready -lt $all.Count) {
            Add-Result INFO 'Demo documents' ('{0} of {1} ready; the rest are being read' -f $ready, $all.Count)
        } else {
            Add-Result OK 'Demo documents' ('{0} ready (those R. Kulkarni may read)' -f $ready)
        }
    }
}

# ------------------------------------------------------------------ the WSL2 configuration

function Invoke-Wsl([string[]]$Arguments) {
    return Invoke-Native 'wsl.exe' $Arguments
}

function Get-WslDistros {
    $r = Invoke-Wsl @('--list', '--quiet')
    if ($r.Code -ne 0) { return @() }
    return @($r.Text -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Convert-ToWslPath([string]$WindowsPath) {
    $r = Invoke-Wsl @('-d', $Distro, '-e', 'wslpath', '-a', $WindowsPath)
    if ($r.Code -eq 0 -and $r.Text.StartsWith('/')) { return $r.Text }
    $drive = $WindowsPath.Substring(0, 1).ToLowerInvariant()
    $rest = $WindowsPath.Substring(2) -replace '\\', '/'
    return ('/mnt/' + $drive + $rest)
}

function Test-WslDistro {
    Write-Section ('WSL2 distro "' + $Distro + '"')
    $names = Get-WslDistros
    if ($names -contains $Distro) {
        Add-Result OK ('Distro ' + $Distro) 'present'
        return $true
    }
    $asked = $Distro
    Add-Result WARN ('Distro ' + $Distro) 'not found' ('Create it: wsl --install -d Ubuntu-24.04 --name ' + $Distro)
    if (-not (Ask ('Create the WSL2 distro "{0}" from Ubuntu 24.04 now? It asks you for a Linux user name and password.' -f $Distro) $true)) {
        return $false
    }
    Write-Host '  Ubuntu asks for a new Linux user name and password. When it then shows a Linux'
    Write-Host '  prompt, type  exit  to come back here.'
    $help = Invoke-Wsl @('--help')
    if ($help.Text -match '--name') {
        [void](Invoke-Interactive 'wsl.exe' ('--install -d Ubuntu-24.04 --name ' + $Distro))
    } else {
        Add-Result INFO ('Distro ' + $Distro) 'this WSL cannot name a distro; creating Ubuntu-24.04 and using that'
        $script:Distro = 'Ubuntu-24.04'
        [void](Invoke-Interactive 'wsl.exe' '--install -d Ubuntu-24.04')
    }
    if ((Get-WslDistros) -contains $script:Distro) {
        Clear-Problem ('Distro ' + $asked)
        Add-Result OK ('Distro ' + $script:Distro) 'created'
        return $true
    }
    Add-Result FAIL ('Distro ' + $script:Distro) 'not created' 'Finish the Ubuntu first-run (user name and password), then run setup wsl2 again.'
    return $false
}

function Test-WslNetworking {
    $config = Join-Path $env:USERPROFILE '.wslconfig'
    if ((Test-Path -LiteralPath $config) -and ((Get-Content -LiteralPath $config -Raw) -match '(?im)^\s*networkingMode\s*=\s*mirrored')) {
        Add-Result WARN 'WSL networking' 'mirrored' 'Ollama in the distro listens on 0.0.0.0, which mirrored mode puts on your network too. Use the default NAT mode, or block port 11434 inbound in Windows Firewall.'
    } else {
        Add-Result OK 'WSL networking' 'NAT (the default): the distro is not reachable from other machines'
    }
}

function Test-DesktopStack {
    # The two configurations publish the same port and must not run at once.
    $exe = Get-DockerExe
    if (-not $exe) { return }
    $running = Invoke-Native $exe @('ps', '--quiet', '--filter', 'label=com.docker.compose.project=citadel')
    if ($running.Code -eq 0 -and $running.Text) {
        Add-Result WARN 'Docker Desktop' 'is running the Citadel stack too' 'Stop it first (citadel stop): both configurations use port 8000.'
    }
}

function Invoke-Provision([string]$RepoWsl, [bool]$CheckOnly) {
    $provision = $RepoWsl + '/ops/wsl2/provision.sh'
    $line = '-d {0} -e bash "{1}" --repo "{2}"' -f $Distro, $provision, $RepoWsl
    if ($CheckOnly) { $line += ' --check' }
    return (Invoke-Interactive 'wsl.exe' $line)
}

function Invoke-Wsl2Setup {
    $mode = ''
    if ($Check) { $mode = ' (check only: nothing is changed)' }
    Write-Host ('Citadel setup: the WSL2 distro configuration' + $mode)
    if (-not (Test-Repository)) { return }
    Test-WindowsHost
    Test-Gpu
    Write-Section 'WSL 2'
    if (-not (Test-Wsl $true)) { return }
    Test-WslNetworking
    if (-not (Test-WslDistro)) { return }
    Test-DesktopStack
    $repoWsl = Convert-ToWslPath $Root

    Write-Section ('Inside "' + $Distro + '": ops/wsl2/provision.sh --check')
    $code = Invoke-Provision $repoWsl $true
    if ($code -ne 0) {
        if ($Check) {
            Add-Result WARN 'Distro setup' 'not complete (see above)' 'Run: setup wsl2'
            return
        }
        if (-not (Ask ('Set up "{0}" now (Docker Engine, nftables, NVIDIA Container Toolkit, Ollama, a clone of the repository)? sudo asks for your Linux password.' -f $Distro) $true)) {
            Add-Result SKIP 'Distro setup' 'not done' ('Inside the distro: bash ' + $repoWsl + '/ops/wsl2/provision.sh')
            return
        }
        Write-Section ('Inside "' + $Distro + '": ops/wsl2/provision.sh')
        $code = Invoke-Provision $repoWsl $false
        if ($code -eq 3) {
            Write-Host '  restarting the distro so systemd starts, then running it again'
            [void](Invoke-Wsl @('--terminate', $Distro))
            $code = Invoke-Provision $repoWsl $false
        }
        if ($code -ne 0) {
            Add-Result FAIL 'Distro setup' 'did not finish (see the FAIL lines above)' 'Fix what it says, then run setup wsl2 again.'
            return
        }
        # A restart applies the docker group membership to the next command.
        [void](Invoke-Wsl @('--terminate', $Distro))
    }
    Add-Result OK 'Distro setup' 'Docker Engine, Ollama and the repository are in place'

    Write-Section ('Start Citadel inside "' + $Distro + '"')
    if (Test-Citadel) {
        Add-Result OK 'Citadel' ('already running at ' + $BaseUrl)
    } elseif ($Check -or $NoStart) {
        Add-Result SKIP 'Citadel' 'not started' ('wsl -d ' + $Distro + ' then: cd ~/citadel && scripts/up.sh')
    } elseif (Ask 'Build and start Citadel in the distro now (scripts/up.sh)?' $true) {
        $code = Invoke-Interactive 'wsl.exe' ('-d {0} -e bash -lc "cd ~/citadel && CITADEL_PORT={1} scripts/up.sh"' -f $Distro, $Port)
        if ($code -ne 0) {
            Add-Result FAIL 'Citadel' 'did not start' ('wsl -d ' + $Distro + ' then: cd ~/citadel && scripts/up.sh logs')
        } elseif (Ask 'Download the approved models into the distro''s Ollama now (scripts/up.sh models; several GB)?' $true) {
            $code = Invoke-Interactive 'wsl.exe' ('-d {0} -e bash -lc "cd ~/citadel && scripts/up.sh models"' -f $Distro)
            if ($code -ne 0) {
                Add-Result FAIL 'Models' 'the download did not finish' 'Check the connection, then: scripts/up.sh models'
            }
        }
    }
    Test-RunningCitadel ('wsl -d ' + $Distro + ', then cd ~/citadel && scripts/up.sh') `
        ('Inside "' + $Distro + '": sudo systemctl status ollama. It must listen on 0.0.0.0:11434, which ops/wsl2/provision.sh sets.') `
        ('Inside "' + $Distro + '": cd ~/citadel && scripts/up.sh models   (or setup wsl2 again)')
}

# ------------------------------------------------------------------ the demonstration machine

function Invoke-DemoSetup {
    $mode = ''
    if ($Check) { $mode = ' (check only: nothing is changed)' }
    Write-Host ('Citadel setup: the Windows demonstration machine' + $mode)
    if (-not (Test-Repository)) { return }
    Test-WindowsHost
    Test-Gpu
    $dockerReady = Test-Docker
    if (-not $dockerReady) {
        # Docker Desktop runs on WSL 2; when its engine is up, WSL evidently works.
        Write-Section 'WSL 2 (Docker Desktop runs on it)'
        [void](Test-Wsl $false)
        if ($script:RestartWindows) { return }
    }
    $ollamaExe = Test-Ollama
    Test-CitadelPort
    Sync-Models $ollamaExe
    if ($dockerReady) { [void](Start-DemoStack) }
    Test-RunningCitadel 'citadel' `
        'Start Ollama for Windows (its tray icon); the containers reach it through host.docker.internal:11434. See SETUP.md troubleshooting.' `
        'Run: citadel models   (or setup again)'
}

function Show-Help {
    Write-Host @'
Citadel setup

  setup                  the Windows demonstration machine: Docker Desktop + Ollama for Windows
  setup check            check everything, change nothing
  setup wsl2             the WSL2 distro configuration (docs\adr\0005)
  setup wsl2 -Check      check the WSL2 configuration, change nothing

Options
  -Yes                   answer yes to every question
  -NoStart               prepare everything, but do not start Citadel
  -Distro <name>         the WSL2 distro to use (default: citadel)
  -Port <n>              the browser port (default: CITADEL_PORT, else 8000)

SETUP.md describes every step, and what to do when one fails.
'@
}

function Show-Summary {
    Write-Section 'Summary'
    $fails = @($Results | Where-Object { $_.Status -eq 'FAIL' })
    $warns = @($Results | Where-Object { $_.Status -eq 'WARN' })
    if ($fails.Count -eq 0 -and $warns.Count -eq 0) {
        Write-Host '  Everything checked is in order.' -ForegroundColor Green
    }
    foreach ($r in @($fails + $warns)) {
        $color = 'Yellow'
        if ($r.Status -eq 'FAIL') { $color = 'Red' }
        Write-Host ('  [{0}] {1}: {2}' -f $r.Status, $r.Name, $r.Detail) -ForegroundColor $color
        if ($r.Fix) { Write-Host ('         -> ' + $r.Fix) -ForegroundColor DarkGray }
    }
    if ($script:RestartWindows) {
        Write-Host ''
        Write-Host '  Restart Windows, then run setup again.' -ForegroundColor Yellow
    } elseif (Test-Citadel) {
        Write-Host ''
        Write-Host ('  Citadel is running: open ' + $BaseUrl + ' and sign in as R. Kulkarni.') -ForegroundColor Green
        Write-Host '  README.md walks through the demonstration; SETUP.md lists the everyday commands.'
    }
}

# ------------------------------------------------------------------ main

try {
    switch ($Action) {
        'help' { Show-Help; exit 0 }
        'demo' { Invoke-DemoSetup }
        'wsl2' { Invoke-Wsl2Setup }
    }
    Show-Summary
} catch {
    Write-Host ''
    Write-Host ('setup stopped: ' + $_) -ForegroundColor Red
    exit 1
}
if (@($Results | Where-Object { $_.Status -eq 'FAIL' }).Count -gt 0) { exit 1 }
exit 0
