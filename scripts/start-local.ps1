param(
    [string]$PythonPath,
    [ValidateRange(1024, 65535)][int]$Port = 8000,
    [switch]$Install,
    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectPath '.venv\Scripts\python.exe'
$requirementsPath = Join-Path $projectPath 'requirements.txt'

if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonArgs = @()
    if (-not $PythonPath) {
        $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $bundledPython) {
            $PythonPath = $bundledPython
        } elseif (Get-Command py -ErrorAction SilentlyContinue) {
            $PythonPath = (Get-Command py).Source
            $pythonArgs = @('-3')
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            $PythonPath = (Get-Command python).Source
        } else {
            throw 'Python 3.11 or newer is required. Supply -PythonPath with the full path to python.exe.'
        }
    }
    & $PythonPath @pythonArgs -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer required"'
    if ($LASTEXITCODE -ne 0) { throw 'A working Python 3.11+ installation is required.' }
    & $PythonPath @pythonArgs -m venv (Join-Path $projectPath '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python environment.' }
    $Install = $true
}

if ($Install) {
    & $venvPython -m pip install -r $requirementsPath
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}

if (-not $env:XBLOOM_DATA_DIR) { $env:XBLOOM_DATA_DIR = Join-Path $projectPath 'data' }
if (-not $env:XBLOOM_ALLOWED_HOSTS) { $env:XBLOOM_ALLOWED_HOSTS = 'localhost,127.0.0.1' }
if (-not $env:XBLOOM_SECURE_COOKIES) { $env:XBLOOM_SECURE_COOKIES = 'false' }
$env:XBLOOM_LAN_ONLY = 'true'

$browserJob = $null
$previewUrl = "http://127.0.0.1:$Port"
Push-Location -LiteralPath $projectPath
try {
    Write-Host "Open $previewUrl in your browser. Press Ctrl+C to stop."
    Write-Host 'This local preview is reachable only from this Windows computer.'
    if ($OpenBrowser) {
        # Wait for the app's session endpoint, rather than opening a browser on a
        # timer while Python is still starting. This helper never handles secrets.
        $browserJob = Start-Job -ArgumentList $previewUrl -ScriptBlock {
            param($PreviewUrl)
            for ($attempt = 0; $attempt -lt 60; $attempt++) {
                try {
                    $session = Invoke-RestMethod -Uri "$PreviewUrl/api/session" -TimeoutSec 1
                    $fields = @($session.PSObject.Properties.Name)
                    if ($fields -contains 'configured' -and $fields -contains 'authenticated') {
                        Start-Process -FilePath $PreviewUrl
                        return
                    }
                } catch {
                    # The foreground server logs report startup failures; keep
                    # this readiness helper quiet while the server is starting.
                }
                Start-Sleep -Milliseconds 500
            }
        }
    }
    & $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'The application stopped with an error.' }
} finally {
    if ($browserJob) {
        if ($browserJob.State -eq 'Running') { Stop-Job -Job $browserJob }
        Remove-Job -Job $browserJob -Force
    }
    Pop-Location
}
