param(
    [string]$IsaacRoot = "E:\software\nvidia\isaac-sim-standalone-5.1.0-windows-x86_64"
)

$ErrorActionPreference = "Stop"

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "=== $Title ==="
}

function Get-NvidiaSmiInfo {
    $cmd = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        return $null
    }

    try {
        $line = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1
        if ([string]::IsNullOrWhiteSpace($line)) {
            return $null
        }

        $parts = $line.Split(',') | ForEach-Object { $_.Trim() }
        if ($parts.Count -lt 3) {
            return $null
        }

        return [pscustomobject]@{
            Name = $parts[0]
            DriverVersion = $parts[1]
            MemoryTotalMB = [int]$parts[2]
        }
    }
    catch {
        return $null
    }
}

function Get-DriverMajor {
    param([string]$Version)
    if ([string]::IsNullOrWhiteSpace($Version)) {
        return $null
    }
    if ($Version -match '^([0-9]{3})') {
        return [int]$Matches[1]
    }
    return $null
}

$report = [ordered]@{}
$risks = New-Object System.Collections.Generic.List[string]
$actions = New-Object System.Collections.Generic.List[string]

Write-Section "Isaac Sim Root"
$report.IsaacRoot = $IsaacRoot
$exists = Test-Path $IsaacRoot
$report.IsaacRootExists = $exists
Write-Host "Path: $IsaacRoot"
Write-Host "Exists: $exists"

if (-not $exists) {
    $risks.Add("Isaac Sim package path does not exist.")
    $actions.Add("Run script with correct path: .\\isaac-sim-precheck.ps1 -IsaacRoot 'actual-path'")
}

$requiredBat = @(
    "post_install.bat",
    "isaac-sim.selector.bat",
    "isaac-sim.bat",
    "isaac-sim.compatibility_check.bat",
    "clear_caches.bat"
)

if ($exists) {
    Write-Section "Key Scripts"
    foreach ($name in $requiredBat) {
        $fullPath = Join-Path $IsaacRoot $name
        $ok = Test-Path $fullPath
        $report["Script:$name"] = $ok
        Write-Host "$name => $ok"
        if (-not $ok) {
            $risks.Add("Missing required script: $name")
        }
    }
}

Write-Section "OS"
$os = Get-CimInstance Win32_OperatingSystem
$report.OS = $os.Caption
$report.OSBuild = $os.BuildNumber
Write-Host "OS: $($os.Caption)"
Write-Host "Build: $($os.BuildNumber)"

Write-Section "CPU and RAM"
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$ramBytes = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
$ramGb = [math]::Round($ramBytes / 1GB, 1)
$report.CPU = $cpu.Name
$report.RAM_GB = $ramGb
Write-Host "CPU: $($cpu.Name)"
Write-Host "RAM: $ramGb GB"
if ($ramGb -lt 31.5) {
    $risks.Add("System RAM is below 32GB minimum recommendation.")
}

Write-Section "NVIDIA GPU and Driver"
$smi = Get-NvidiaSmiInfo
$gpu = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -like "*NVIDIA*" } | Select-Object -First 1
if ($null -eq $gpu -and $null -eq $smi) {
    $report.GPU = "Not Found"
    Write-Host "NVIDIA GPU: Not Found"
    $risks.Add("No NVIDIA GPU detected.")
}
else {
    $gpuName = if ($null -ne $smi) { $smi.Name } else { $gpu.Name }
    $driverVersion = if ($null -ne $smi) { $smi.DriverVersion } else { $gpu.DriverVersion }
    $vramGb = if ($null -ne $smi) { [math]::Round(($smi.MemoryTotalMB / 1024.0), 1) } else { [math]::Round(($gpu.AdapterRAM / 1GB), 1) }

    $report.GPU = $gpuName
    $report.GPUDriverVersion = $driverVersion
    $report.GPUVRAM_GB = $vramGb
    Write-Host "GPU: $gpuName"
    Write-Host "Driver: $driverVersion"
    Write-Host "VRAM: $vramGb GB"

    if ($vramGb -lt 10) {
        $risks.Add("VRAM is below 10GB minimum for compatibility checker.")
        $actions.Add("Current machine is likely to fail compatibility checks and startup.")
    }
    elseif ($vramGb -lt 16) {
        $risks.Add("VRAM is below 16GB recommended level for stable workloads.")
        $actions.Add("Use lightweight scenes only. Avoid heavy multi-sensor workloads.")
    }

    $driverMajor = Get-DriverMajor -Version $driverVersion
    $report.DriverMajor = $driverMajor
    if ($null -eq $driverMajor -or $driverMajor -lt 537) {
        $risks.Add("Driver appears below minimum supported range.")
        $actions.Add("Upgrade NVIDIA driver immediately before launching Isaac Sim.")
    }
    elseif ($driverMajor -lt 580) {
        $actions.Add("Driver is supported but below tested baseline 580.88. Upgrade for best stability.")
    }
}

Write-Section "VC++ Redistributable"
$uninstallRoots = @(
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*"
)

$installedPrograms = foreach ($root in $uninstallRoots) {
    Get-ItemProperty $root -ErrorAction SilentlyContinue
}

$vcpp = $installedPrograms | Where-Object {
    $_.DisplayName -match "Visual C\+\+" -and $_.DisplayName -match "(2015-2022|2015)" -and $_.DisplayName -match "x64"
} | Select-Object -First 1

if ($null -eq $vcpp) {
    $report.VCppX64 = "Not Found"
    Write-Host "VC++ 2015-2022 x64: Not Found"
    $actions.Add("If launch fails with runtime DLL errors, install latest VC++ 2015-2022 x64 runtime.")
}
else {
    $report.VCppX64 = $vcpp.DisplayVersion
    Write-Host "VC++ 2015-2022 x64: $($vcpp.DisplayVersion)"
}

Write-Section "Recommended Commands"
$commands = @(
    "cd /d `"$IsaacRoot`"",
    ".\\post_install.bat",
    ".\\isaac-sim.selector.bat",
    ".\\isaac-sim.compatibility_check.bat",
    ".\\isaac-sim.bat --reset-user"
)
$report.RecommendedCommands = $commands
$commands | ForEach-Object { Write-Host $_ }

if ($actions.Count -eq 0) {
    $actions.Add("Ready to run install and first launch sequence.")
}

Write-Section "Summary"
$report.Risks = $risks
$report.Actions = $actions
Write-Host "Risk count: $($risks.Count)"
if ($risks.Count -gt 0) {
    $risks | ForEach-Object { Write-Host "- $_" }
}
Write-Host "Action count: $($actions.Count)"
$actions | ForEach-Object { Write-Host "- $_" }

Write-Section "JSON Report"
$report | ConvertTo-Json -Depth 6
