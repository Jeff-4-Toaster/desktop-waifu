#requires -Version 7.0
# Dot-source this file. Changes apply only to the current PowerShell process.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$ldcDirectory = Join-Path $projectRoot '.tools/ldc2-1.43.0-windows-x64/bin'
$toolDirectories = @($ldcDirectory)
$env:DUB_HOME = Join-Path $projectRoot '.tools/dub'
$env:DUB_CACHE_PATH = Join-Path $projectRoot '.tools/dub/cache'
$env:PATH = ($toolDirectories -join ';') + ';' + $env:PATH

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
if (Test-Path -LiteralPath $vswhere) {
    $installations = @(& $vswhere -all -prerelease -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -format json | ConvertFrom-Json)
    $visualStudio = $installations | Sort-Object @{ Expression = 'isPrerelease'; Ascending = $true }, @{ Expression = { [version]$_.installationVersion }; Descending = $true } | Select-Object -First 1
    if ($visualStudio) {
        $msvcRoot = Join-Path $visualStudio.installationPath 'VC/Tools/MSVC'
        $msvc = Get-ChildItem -LiteralPath $msvcRoot -Directory | Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
        $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits/10'
        $sdk = Get-ChildItem -LiteralPath (Join-Path $kits 'Include') -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'um/Windows.h') } | Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
        if ($msvc -and $sdk) {
            $env:VCINSTALLDIR = Join-Path $visualStudio.installationPath 'VC/'
            $env:VSINSTALLDIR = $visualStudio.installationPath
            $env:VCToolsInstallDir = $msvc.FullName
            $env:WindowsSdkDir = $kits
            $env:WindowsSDKVersion = "$($sdk.Name)\"
            $env:PATH = "$(Join-Path $msvc.FullName 'bin/Hostx64/x64');$(Join-Path $kits "bin/$($sdk.Name)/x64");$env:PATH"
            $env:INCLUDE = @((Join-Path $msvc.FullName 'include'), (Join-Path $sdk.FullName 'ucrt'), (Join-Path $sdk.FullName 'shared'), (Join-Path $sdk.FullName 'um'), (Join-Path $sdk.FullName 'winrt')) -join ';'
            $env:LIB = @((Join-Path $msvc.FullName 'lib/x64'), (Join-Path $kits "Lib/$($sdk.Name)/ucrt/x64"), (Join-Path $kits "Lib/$($sdk.Name)/um/x64")) -join ';'
        }
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $ldcDirectory 'ldc2.exe'))) { throw 'LDC is missing. Run tools/bootstrap-environment.ps1 first.' }
