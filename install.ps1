<#
CrawlRX installer for Windows (PowerShell 5.1+).

  irm https://raw.githubusercontent.com/LiquidLava1990/crawlrx/main/install.ps1 | iex

Unattended / scripted use: set environment variables first, or download the
script and pass parameters:
  $env:CRAWLRX_INSTALL_MODE = 'docker'      # -Docker      run the Docker stack instead
  $env:CRAWLRX_WITH_OLLAMA  = '1'           # -WithOllama  Docker mode: also start Ollama + a small model
  $env:CRAWLRX_SKIP_BROWSER = '1'           # -NoBrowser   skip the Chromium download
  $env:CRAWLRX_INSTALL_DIR  = 'D:\crawlrx'  # -Dir         Docker mode: where to put the compose files
  $env:CRAWLRX_PIP_SPEC     = 'crawlrx'     # what to pip-install (default: this repo's main branch)
#>
[CmdletBinding()]
param(
    [switch]$Docker,
    [switch]$WithOllama,
    [switch]$NoBrowser,
    [string]$Dir
)

# Everything runs in its own scope so `irm | iex` leaves the caller's session untouched.
& {
    param([bool]$Docker, [bool]$WithOllama, [bool]$NoBrowser, [string]$Dir)

    $ErrorActionPreference = 'Stop'
    $Repo = 'LiquidLava1990/crawlrx'
    $Mode = if ($Docker) { 'docker' } elseif ($env:CRAWLRX_INSTALL_MODE) { $env:CRAWLRX_INSTALL_MODE } else { 'pip' }
    $UseOllama = $WithOllama -or ($env:CRAWLRX_WITH_OLLAMA -eq '1')
    $SkipBrowser = $NoBrowser -or ($env:CRAWLRX_SKIP_BROWSER -eq '1')
    $InstallDir = if ($Dir) { $Dir } elseif ($env:CRAWLRX_INSTALL_DIR) { $env:CRAWLRX_INSTALL_DIR } else { Join-Path $HOME 'crawlrx' }
    $PipSpec = if ($env:CRAWLRX_PIP_SPEC) { $env:CRAWLRX_PIP_SPEC } else { "https://github.com/$Repo/archive/refs/heads/main.tar.gz" }
    $DataHome = Join-Path $env:LOCALAPPDATA 'crawlrx'
    $Venv = Join-Path $DataHome 'venv'
    $BinDir = Join-Path $DataHome 'bin'

    function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
    # throw, not exit: `exit` would close the window of someone running `irm | iex`.
    function Stop-Install([string]$Message) { throw "CrawlRX install failed: $Message" }

    function New-RandomSecret {
        $bytes = New-Object byte[] 24
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        -join ($bytes | ForEach-Object { $_.ToString('x2') })
    }

    function Install-DockerStack {
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            Stop-Install 'Docker is not installed. Get Docker Desktop from https://docs.docker.com/desktop/ or re-run without -Docker.'
        }
        $ErrorActionPreference = 'Continue'
        docker compose version *> $null
        $ErrorActionPreference = 'Stop'
        if ($LASTEXITCODE -ne 0) { Stop-Install 'Docker Compose v2 is missing. Update Docker Desktop.' }
        Write-Step "Downloading CrawlRX to $InstallDir"
        $zip = Join-Path ([System.IO.Path]::GetTempPath()) 'crawlrx-main.zip'
        $unpack = Join-Path ([System.IO.Path]::GetTempPath()) 'crawlrx-main'
        Invoke-WebRequest -UseBasicParsing "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip
        if (Test-Path $unpack) { Remove-Item -Recurse -Force $unpack }
        Expand-Archive -Path $zip -DestinationPath $unpack
        New-Item -ItemType Directory -Force $InstallDir | Out-Null
        Copy-Item -Recurse -Force (Join-Path (Get-ChildItem $unpack | Select-Object -First 1).FullName '*') $InstallDir
        Remove-Item -Recurse -Force $unpack, $zip
        Push-Location $InstallDir
        try {
            if (-not (Test-Path '.env')) {
                $secret = New-RandomSecret
                (Get-Content '.env.example') -replace '^SEARXNG_SECRET=.*', "SEARXNG_SECRET=$secret" | Set-Content '.env' -Encoding ascii
            }
            $composeArgs = if ($UseOllama) { @('--profile', 'ollama', 'up', '-d', '--build') } else { @('up', '-d', '--build') }
            Write-Step ('Starting CrawlRX + SearXNG' + $(if ($UseOllama) { ' + Ollama (first model download can take a few minutes)' } else { '' }))
            $ErrorActionPreference = 'Continue'  # docker writes progress to stderr
            docker compose @composeArgs
            $ErrorActionPreference = 'Stop'
            if ($LASTEXITCODE -ne 0) { Stop-Install 'docker compose failed; see the output above.' }
        } finally { Pop-Location }
        Write-Step 'CrawlRX is starting at http://127.0.0.1:7337  (dashboard: /dashboard, API docs: /docs)'
        Write-Step "Model check: cd $InstallDir; docker compose exec crawlrx crawlrx doctor"
    }

    function Find-Python {
        # The Microsoft Store "python.exe" stubs live in \WindowsApps\ and only print an
        # install hint, so skip them; probe failures must never abort the installer.
        foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
            $command = Get-Command $candidate[0] -ErrorAction SilentlyContinue
            if (-not $command -or $command.Source -like '*\WindowsApps\*') { continue }
            $probeArgs = @($candidate | Select-Object -Skip 1) + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)')
            try {
                $ErrorActionPreference = 'Continue'
                & $command.Source @probeArgs *> $null
                if ($LASTEXITCODE -eq 0) {
                    $found = @($command.Source) + @($candidate | Select-Object -Skip 1)
                    return , $found
                }
            } catch {
                continue
            } finally {
                $ErrorActionPreference = 'Stop'
            }
        }
        return $null
    }

    function Add-UserPath([string]$Folder) {
        # Read the raw (unexpanded) value so %VARIABLES% in the user PATH survive.
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
        try {
            $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if (($raw -split ';') -contains $Folder) { return $false }
            $updated = if ($raw) { $raw.TrimEnd(';') + ';' + $Folder } else { $Folder }
            $key.SetValue('Path', $updated, [Microsoft.Win32.RegistryValueKind]::ExpandString)
        } finally { $key.Close() }
        $env:Path += ";$Folder"
        return $true
    }

    function Install-Local {
        $python = Find-Python
        if (-not $python) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                Write-Step 'Python 3.10+ not found; installing Python 3.12 with winget'
                $ErrorActionPreference = 'Continue'
                winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
                $ErrorActionPreference = 'Stop'
                $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
                $python = Find-Python
            }
            if (-not $python) { Stop-Install 'Python 3.10+ is required. Install it from https://www.python.org/downloads/ (tick "Add to PATH"), or re-run with -Docker.' }
        }
        $pyExe = $python[0]; $pyArgs = @($python | Select-Object -Skip 1)
        Write-Step "Creating virtual environment in $Venv"
        New-Item -ItemType Directory -Force $DataHome | Out-Null
        & $pyExe @pyArgs -m venv $Venv
        if ($LASTEXITCODE -ne 0) { Stop-Install 'Could not create the virtual environment.' }
        $venvPython = Join-Path $Venv 'Scripts\python.exe'
        $ErrorActionPreference = 'Continue'  # pip prints notices to stderr
        & $venvPython -m pip install --quiet --upgrade pip
        Write-Step "Installing CrawlRX from $PipSpec"
        & $venvPython -m pip install --quiet $PipSpec
        $pipCode = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($pipCode -ne 0) { Stop-Install 'pip install failed; see the output above.' }

        New-Item -ItemType Directory -Force $BinDir | Out-Null
        $crawlrxExe = Join-Path $Venv 'Scripts\crawlrx.exe'
        Set-Content -Path (Join-Path $BinDir 'crawlrx.cmd') -Value "@`"$crawlrxExe`" %*" -Encoding ascii
        if (Add-UserPath $BinDir) { Write-Step "Added $BinDir to your user PATH (new terminals will find 'crawlrx')." }
        $ErrorActionPreference = 'Continue'
        if ($SkipBrowser) { & $crawlrxExe setup --skip-browser } else { & $crawlrxExe setup }
        $ErrorActionPreference = 'Stop'
        Write-Step 'Done. Start the API with:  crawlrx serve    (then open http://127.0.0.1:7337/dashboard)'
    }

    switch ($Mode) {
        'docker' { Install-DockerStack }
        { $_ -in 'pip', 'local' } { Install-Local }
        default { Stop-Install "CRAWLRX_INSTALL_MODE must be 'pip' or 'docker' (got '$Mode')" }
    }
} ([bool]$Docker) ([bool]$WithOllama) ([bool]$NoBrowser) $Dir
