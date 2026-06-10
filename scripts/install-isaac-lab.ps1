param(
    [string]$IsaacRoot = "E:\software\nvidia\isaac-sim-standalone-5.1.0-windows-x86_64",
    [string]$IsaacLabRoot = "",
    [string]$Framework = "none",
    [switch]$UpdateRepo
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($IsaacLabRoot)) {
    $IsaacLabRoot = Join-Path (Split-Path -Parent $PSScriptRoot) "IsaacLab"
}

Write-Host "IsaacRoot    : $IsaacRoot"
Write-Host "IsaacLabRoot : $IsaacLabRoot"
Write-Host "Framework    : $Framework"

if (-not (Test-Path $IsaacRoot)) {
    throw "Isaac Sim path not found: $IsaacRoot"
}
if (-not (Test-Path (Join-Path $IsaacRoot "python.bat"))) {
    throw "python.bat not found under Isaac Sim path: $IsaacRoot"
}

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if ($null -eq $gitCmd) {
    throw "Git is not installed or not in PATH. Install Git first."
}

if (-not (Test-Path (Join-Path $IsaacLabRoot "isaaclab.bat"))) {
    Write-Host "[INFO] Cloning IsaacLab..."
    $cloneOk = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if (Test-Path $IsaacLabRoot) {
            Remove-Item $IsaacLabRoot -Recurse -Force -ErrorAction SilentlyContinue
        }

        Write-Host "[INFO] Clone attempt $attempt/3 ..."
        & git -c http.version=HTTP/1.1 -c http.sslBackend=schannel clone --recursive --depth 1 --filter=blob:none --shallow-submodules https://github.com/isaac-sim/IsaacLab.git "$IsaacLabRoot"
        if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $IsaacLabRoot "isaaclab.bat"))) {
            $cloneOk = $true
            break
        }

        Write-Warning "Clone attempt $attempt failed."
    }

    if (-not $cloneOk) {
        throw "Failed to clone IsaacLab after 3 attempts. Please check network/Git access to github.com and retry."
    }
} elseif ($UpdateRepo) {
    Write-Host "[INFO] Updating existing IsaacLab repo..."
    Push-Location $IsaacLabRoot
    try {
        & git -c http.sslBackend=schannel pull --ff-only
        & git -c http.sslBackend=schannel submodule update --init --recursive
    }
    finally {
        Pop-Location
    }
}

$simLink = Join-Path $IsaacLabRoot "_isaac_sim"
if (Test-Path $simLink) {
    Write-Host "[INFO] Removing existing _isaac_sim link/folder..."
    Remove-Item $simLink -Recurse -Force
}

Write-Host "[INFO] Creating _isaac_sim junction -> $IsaacRoot"
New-Item -ItemType Junction -Path $simLink -Target $IsaacRoot | Out-Null

$bat = Join-Path $IsaacLabRoot "isaaclab.bat"
if (-not (Test-Path $bat)) {
    throw "isaaclab.bat not found at: $bat"
}

Write-Host "[INFO] Installing Isaac Lab packages (framework=$Framework)..."
Push-Location $IsaacLabRoot
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$oldPipCache = $env:PIP_CACHE_DIR
$oldPipNoCache = $env:PIP_NO_CACHE_DIR
$oldPipNoBuildIsolation = $env:PIP_NO_BUILD_ISOLATION
try {
    $workspaceRoot = Split-Path -Parent $PSScriptRoot
    $tempRoot = Join-Path $workspaceRoot "output\pip-temp"
    $cacheRoot = Join-Path $workspaceRoot "output\pip-cache"
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
    Get-ChildItem -Path $tempRoot -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $cacheRoot -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    $env:TEMP = $tempRoot
    $env:TMP = $tempRoot
    $env:PIP_CACHE_DIR = $cacheRoot
    $env:PIP_NO_CACHE_DIR = "1"
    $env:PIP_NO_BUILD_ISOLATION = "1"

    Write-Host "[INFO] TEMP redirected to: $tempRoot"
    Write-Host "[INFO] PIP cache dir      : $cacheRoot"

    & $bat --install $Framework
    if ($LASTEXITCODE -ne 0) {
        throw "isaaclab.bat install failed with exit code $LASTEXITCODE"
    }
}
finally {
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    $env:PIP_CACHE_DIR = $oldPipCache
    $env:PIP_NO_CACHE_DIR = $oldPipNoCache
    $env:PIP_NO_BUILD_ISOLATION = $oldPipNoBuildIsolation
    Pop-Location
}

Write-Host "[DONE] Isaac Lab installation completed."
Write-Host "[NEXT] Verification command:"
Write-Host "       cd /d \"$IsaacLabRoot\""
Write-Host "       .\\isaaclab.bat -p scripts\\tutorials\\00_sim\\create_empty.py"
