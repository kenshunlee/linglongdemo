param(
  [Parameter(Mandatory = $false)]
  [string]$EnvId = "",
  [Parameter(Mandatory = $false)]
  [string]$GatewayDomain = "https://api.example.com",
  [Parameter(Mandatory = $false)]
  [string]$GatewayPrefix = "/asr"
)

Write-Host "=== WeChat Cloud Deploy Next Steps ==="
if ($EnvId -ne "") {
  Write-Host "Cloud Env ID: $EnvId"
}
Write-Host "1. WeChat DevTools -> Cloud Development -> Cloud Hosting: deploy folder backend/ (with Dockerfile)"
Write-Host "2. Cloud Hosting service port: 8765"
Write-Host "3. Environment variable reference: backend/cloud.env.example"
Write-Host "4. Bind Cloud Hosting service in WeChat Gateway and configure routes: $GatewayPrefix/health, $GatewayPrefix/transcribe, $GatewayPrefix/records"
Write-Host "5. Add legal request domain in MP admin: $GatewayDomain"
Write-Host "6. Update miniprogram/app.js serverBase to: $GatewayDomain$GatewayPrefix"
if ($EnvId -ne "") {
  Write-Host "7. Optional CLI check:"
  Write-Host "   cli.bat cloud functions list --project f:/Robots/team66/asr/miniprogram --env $EnvId --lang zh"
}
Write-Host "======================================="
