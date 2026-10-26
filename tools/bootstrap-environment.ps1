#requires -Version 7.0
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$downloadRoot = Join-Path $projectRoot '.tools/downloads'
New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
$lock = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'toolchain-lock.json') -Raw | ConvertFrom-Json
$lock.assets | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $downloadRoot 'release-assets.json') -Encoding utf8
& (Join-Path $PSScriptRoot 'download-toolchain.ps1')
$ldcRoot = Join-Path $projectRoot '.tools/ldc2-1.43.0-windows-x64'
if (-not (Test-Path -LiteralPath (Join-Path $ldcRoot 'bin/ldc2.exe'))) {
    $sevenZip = Join-Path $env:ProgramFiles '7-Zip/7z.exe'
    if (-not (Test-Path -LiteralPath $sevenZip)) { throw '7-Zip is required to unpack LDC.' }
    & $sevenZip x (Join-Path $downloadRoot 'ldc2-1.43.0-windows-x64.7z') "-o$(Join-Path $projectRoot '.tools')" -y -bso0 -bsp0
    if ($LASTEXITCODE -ne 0) { throw 'LDC extraction failed' }
}
$sdkRoot = Join-Path $projectRoot '.tools/src/inochi2d-v0.8.6'
if (-not (Test-Path -LiteralPath (Join-Path $sdkRoot '.git'))) {
    New-Item -ItemType Directory -Path (Split-Path $sdkRoot -Parent) -Force | Out-Null
    & git clone --depth 1 --branch v0.8.6 https://github.com/Inochi2D/inochi2d.git $sdkRoot
    if ($LASTEXITCODE -ne 0) { throw 'SDK clone failed' }
}
if ((& git -C $sdkRoot rev-parse HEAD) -ne 'dda3a8f005f808104caa43183ab8753fb989e30f') { throw 'SDK revision mismatch' }
. (Join-Path $PSScriptRoot 'enter-dev-env.ps1')
Write-Output 'Native renderer build dependencies ready.'
