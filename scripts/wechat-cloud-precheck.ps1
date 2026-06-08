param(
  [string]$ProjectPath = "f:/Robots/team66/asr/miniprogram",
  [string]$CliPath = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($CliPath)) {
  $roots = @("$env:ProgramFiles(x86)/Tencent", "$env:ProgramFiles/Tencent")
  foreach ($root in $roots) {
    if (Test-Path $root) {
      $found = Get-ChildItem -Path $root -Recurse -Filter cli.bat -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($found) {
        $CliPath = $found.FullName
        break
      }
    }
  }
}

if ([string]::IsNullOrWhiteSpace($CliPath) -or !(Test-Path $CliPath)) {
  Write-Error "WeChat DevTools CLI not found. Please pass -CliPath explicitly."
}

Write-Host "[1/3] Checking login status..."
& $CliPath islogin --project $ProjectPath --lang zh

Write-Host "[2/3] Listing cloud environments..."
& $CliPath cloud env list --project $ProjectPath --lang zh

Write-Host "[3/3] Skipping cloud functions list (requires --env)."
Write-Host "Use this command after you know env id:"
Write-Host "  cli.bat cloud functions list --project $ProjectPath --env <your-env-id> --lang zh"

Write-Host "Precheck finished. If you still see IDE service port errors, enable service port in DevTools settings and retry."
