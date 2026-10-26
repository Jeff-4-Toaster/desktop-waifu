#requires -Version 7.0
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'enter-dev-env.ps1')
$projectRoot = Split-Path $PSScriptRoot -Parent
$sdkRoot = Join-Path $projectRoot '.tools/src/inochi2d-v0.8.6'
if ((& git -C $sdkRoot rev-parse HEAD) -ne 'dda3a8f005f808104caa43183ab8753fb989e30f') { throw 'Unexpected SDK revision' }
& git -C $sdkRoot diff --quiet
if ($LASTEXITCODE -ne 0) { throw 'SDK has local changes; refusing to overwrite it' }
$logRoot = Join-Path $projectRoot 'build/desktop'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$previousDFlags = $env:DFLAGS
Push-Location (Join-Path $projectRoot 'app/native')
try {
        $env:DFLAGS = '-O3 -g -release -boundscheck=off -link-defaultlib-shared=false -dllimport=none'
        $output = & dub.exe build --compiler=ldc2 --build=release --cache=local 2>&1
        $code = $LASTEXITCODE
        $output | Set-Content -LiteralPath (Join-Path $logRoot 'renderer-build.log') -Encoding utf8
        if ($code -ne 0) {
            $output | Select-Object -Last 35 | Write-Output
            throw "Renderer build failed ($code)"
        }
} finally {
    if ($null -eq $previousDFlags) { Remove-Item Env:DFLAGS -ErrorAction SilentlyContinue }
    else { $env:DFLAGS = $previousDFlags }
    Pop-Location
}
& git -C $sdkRoot diff --quiet
if ($LASTEXITCODE -ne 0) { throw 'SDK source changed during build' }
Copy-Item -LiteralPath 'C:/Program Files/Inochi Creator/SDL2.dll' -Destination (Join-Path $logRoot 'SDL2.dll')
Write-Output 'Native renderer built; official SDK source unchanged.'
