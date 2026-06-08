param(
  [Parameter(Mandatory = $true)]
  [string]$RemoteUrl
)

$ErrorActionPreference = 'Stop'

Set-Location "f:/Robots/team66/asr"

if (!(Test-Path ".git")) {
  throw "Current folder is not a git repository."
}

$hasOrigin = git remote | Select-String -SimpleMatch "origin"
if ($hasOrigin) {
  git remote set-url origin $RemoteUrl
} else {
  git remote add origin $RemoteUrl
}

git branch -M main
git push -u origin main
