$ErrorActionPreference = 'Stop'
$state = Join-Path $PSScriptRoot '.pixel-uploader'
New-Item -ItemType Directory -Path $state -Force | Out-Null
Set-Content -LiteralPath (Join-Path $state 'STOP.txt') -Value 'User requested graceful stop'
Write-Host 'Stop requested. Keep the phone connected until the uploader exits.'
