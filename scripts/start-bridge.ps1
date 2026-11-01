param(
    [string]$ServerUrl = $env:XBLOOM_SERVER_URL,
    [string]$ProxmarkExecutable = $env:PM3_EXECUTABLE,
    [string]$Port = 'COM6',
    [switch]$Hardware,
    [switch]$AllowWrites,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Run scripts\start-local.ps1 once to create the local Python environment.'
}
if ($AllowWrites -and -not $Hardware) { throw '-AllowWrites requires -Hardware.' }
if (-not $ServerUrl) { $ServerUrl = Read-Host 'Application URL (for example https://192.168.1.50)' }
if ($Hardware) {
    if (-not $ProxmarkExecutable) { $ProxmarkExecutable = Read-Host 'Full path to the actual Proxmark client .exe' }
    if (-not (Test-Path -LiteralPath $ProxmarkExecutable -PathType Leaf)) { throw 'Proxmark client executable was not found.' }
    if ([IO.Path]::GetExtension($ProxmarkExecutable) -ne '.exe') { throw 'Supply the actual Proxmark .exe; batch wrappers are not supported.' }
}

$previousToken = $env:XBLOOM_BRIDGE_TOKEN
$promptedToken = $false
if (-not $env:XBLOOM_BRIDGE_TOKEN) {
    $secureToken = Read-Host 'Bridge token from Settings (input hidden)' -AsSecureString
    $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    try {
        $env:XBLOOM_BRIDGE_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
        $promptedToken = $true
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    }
}
$env:XBLOOM_SERVER_URL = $ServerUrl
$env:PM3_PORT = $Port
if ($ProxmarkExecutable) { $env:PM3_EXECUTABLE = $ProxmarkExecutable }
if (-not $env:XBLOOM_BRIDGE_DATA_DIR) { $env:XBLOOM_BRIDGE_DATA_DIR = Join-Path $projectPath 'data\bridge' }

$bridgeArgs = @('-m', 'bridge.main')
if ($Hardware) { $bridgeArgs += '--hardware' }
if ($AllowWrites) { $bridgeArgs += '--allow-writes' }
if ($Once) { $bridgeArgs += '--once' }
Push-Location -LiteralPath $projectPath
try {
    if (-not $Hardware) { Write-Host 'SIMULATION MODE: this process will not access the Proxmark.' }
    elseif (-not $AllowWrites) { Write-Host 'HARDWARE READ MODE: card writes are disabled.' }
    else { Write-Host 'HARDWARE WRITE MODE: each write still needs dashboard and local confirmation.' }
    & $venvPython @bridgeArgs
    if ($LASTEXITCODE -ne 0) { throw 'The bridge stopped with an error.' }
} finally {
    Pop-Location
    if ($promptedToken) { $env:XBLOOM_BRIDGE_TOKEN = $previousToken }
}
