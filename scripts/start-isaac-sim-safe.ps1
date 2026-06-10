param(
    [string]$IsaacRoot = "E:\software\nvidia\isaac-sim-standalone-5.1.0-windows-x86_64",
    [ValidateSet("streaming", "full", "fabric")]
    [string]$Mode = "streaming",
    [switch]$ResetUser,
    [switch]$NoRosEnv,
    [switch]$RunCompatibilityCheck,
    [switch]$CheckOnly,
    [string]$WorkspaceRoot = "F:\Robots\team66"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $IsaacRoot)) {
    throw "Isaac root not found: $IsaacRoot"
}

# Use ASCII-only Omniverse directories to avoid profile path encoding issues.
$ovUser = Join-Path $WorkspaceRoot "asr\output\ov_user"
$ovCache = Join-Path $WorkspaceRoot "asr\output\ov_cache"
$ovLogs = Join-Path $WorkspaceRoot "asr\output\ov_logs"

New-Item -ItemType Directory -Force -Path $ovUser, $ovCache, $ovLogs | Out-Null
$env:OMNI_USER_DIR = $ovUser
$env:OMNI_CACHE_DIR = $ovCache
$env:OMNI_LOG_DIR = $ovLogs

$launcher = switch ($Mode) {
    "streaming" { "isaac-sim.streaming.bat" }
    "fabric" { "isaac-sim.fabric.bat" }
    default { "isaac-sim.bat" }
}

$launcherPath = Join-Path $IsaacRoot $launcher
if (-not (Test-Path $launcherPath)) {
    throw "Launcher not found: $launcherPath"
}

if ($RunCompatibilityCheck) {
    $checker = Join-Path $IsaacRoot "isaac-sim.compatibility_check.bat"
    if (Test-Path $checker) {
        Write-Host "Running compatibility checker..."
        & $checker
    }
}

if ($CheckOnly) {
    Write-Host "CheckOnly is enabled. Skipping app launch."
    exit 0
}

$args = @()
if ($ResetUser) {
    $args += "--reset-user"
}
if ($NoRosEnv) {
    $args += "--no-ros-env"
}

Write-Host "IsaacRoot: $IsaacRoot"
Write-Host "Mode: $Mode"
Write-Host "OMNI_USER_DIR: $($env:OMNI_USER_DIR)"
Write-Host "OMNI_CACHE_DIR: $($env:OMNI_CACHE_DIR)"
Write-Host "OMNI_LOG_DIR: $($env:OMNI_LOG_DIR)"
Write-Host "Launcher: $launcher"
Write-Host "Args: $($args -join ' ')"

Push-Location $IsaacRoot
try {
    & $launcherPath @args
}
finally {
    Pop-Location
}
