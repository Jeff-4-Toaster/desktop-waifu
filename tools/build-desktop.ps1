#requires -Version 7.0
param([switch]$SkipRenderer)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'enter-dev-env.ps1')
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $SkipRenderer) { & (Join-Path $PSScriptRoot 'build-renderer.ps1') }
$output = Join-Path $projectRoot 'build/desktop'
New-Item -ItemType Directory -Path $output -Force | Out-Null
Push-Location $output
try {
    & cl.exe /nologo /std:c++17 /O2 /MT /EHsc /utf-8 /W4 (Join-Path $projectRoot 'app/desktop/main.cpp') /Fe:DesktopWaifu.exe /link /SUBSYSTEM:WINDOWS user32.lib gdi32.lib shell32.lib ole32.lib advapi32.lib psapi.lib winmm.lib gdiplus.lib comctl32.lib
    if ($LASTEXITCODE -ne 0) { throw 'Desktop host build failed' }
} finally { Pop-Location }
Copy-Item -LiteralPath (Join-Path $projectRoot 'app/desktop/assets/menu-expressions.png') -Destination (Join-Path $output 'menu-expressions.png')
Write-Output (Join-Path $output 'DesktopWaifu.exe')
