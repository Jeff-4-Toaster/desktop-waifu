#requires -Version 7.0
param([switch]$ChatOnly)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'enter-dev-env.ps1')
$projectRoot=Split-Path $PSScriptRoot -Parent
$output=Join-Path $projectRoot 'build/desktop'
$sdk=Join-Path $projectRoot '.tools/webview2-sdk-1.0.4258.31/build/native'
$asrOutput=Join-Path $output 'backend/whisper'
$whisperHeaders=Join-Path $projectRoot '.tools/whisper-v1.8.3/headers'
if (-not $ChatOnly) {
New-Item -ItemType Directory -Path $asrOutput -Force | Out-Null
$headerLock=Get-Content -LiteralPath (Join-Path $projectRoot 'tools/whisper-headers-lock.json') -Raw | ConvertFrom-Json -AsHashtable
foreach($entry in $headerLock.GetEnumerator()) {if((Get-FileHash -LiteralPath (Join-Path $whisperHeaders $entry.Key) -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value.sha256){throw 'Unexpected whisper header hash'}}
}
New-Item -ItemType Directory -Path $output -Force | Out-Null
Push-Location $output
try {
    if (-not $ChatOnly) {
    & cl.exe /nologo /O2 /MT /EHsc /std:c++17 /utf-8 /W4 (Join-Path $projectRoot 'app/desktop/speech_helper.cpp') /Fe:SpeechHelper.exe /link sapi.lib ole32.lib oleaut32.lib advapi32.lib
    if($LASTEXITCODE -ne 0){throw 'Speech helper build failed'}
    }
    & cl.exe /nologo /O2 /MT /EHsc /std:c++17 /utf-8 /W4 /I (Join-Path $sdk 'include') (Join-Path $projectRoot 'app/desktop/chat_host.cpp') /Fe:CompanionChat.exe /link /SUBSYSTEM:WINDOWS (Join-Path $sdk 'x64/WebView2LoaderStatic.lib') user32.lib gdi32.lib ole32.lib oleaut32.lib shell32.lib shlwapi.lib advapi32.lib version.lib
    if($LASTEXITCODE -ne 0){throw 'Chat host build failed'}
    if (-not $ChatOnly) {
    & lib.exe /nologo /machine:x64 "/def:$(Join-Path $projectRoot 'app/desktop/whisper-imports.def')" "/out:$(Join-Path $asrOutput 'whisper.lib')"
    if($LASTEXITCODE -ne 0){throw 'Whisper import library failed'}
    & cl.exe /nologo /O2 /MT /EHsc /std:c++17 /utf-8 /W4 /DWHISPER_SHARED /I $whisperHeaders (Join-Path $projectRoot 'app/desktop/asr_helper.cpp') "/Fe:$(Join-Path $asrOutput 'AsrHelper.exe')" /link (Join-Path $asrOutput 'whisper.lib')
    if($LASTEXITCODE -ne 0){throw 'Unicode ASR helper build failed'}
    foreach($name in @('whisper.dll','ggml.dll','ggml-base.dll','ggml-cpu.dll')) {Copy-Item -LiteralPath (Join-Path $projectRoot ".tools/whisper-v1.8.3/Release/$name") -Destination $asrOutput}
    $redistRoot=Get-ChildItem -LiteralPath (Join-Path $env:VCINSTALLDIR 'Redist/MSVC') -Directory | Where-Object {$_.Name -match '^\d'} | Sort-Object {[version]$_.Name} -Descending | Select-Object -First 1
    Get-ChildItem -LiteralPath (Join-Path $redistRoot.FullName 'x64') -Directory | Where-Object {$_.Name -match '^Microsoft\.VC\d+\.(CRT|OpenMP)$'} | ForEach-Object {Get-ChildItem -LiteralPath $_.FullName -Filter '*.dll' | ForEach-Object {Copy-Item -LiteralPath $_.FullName -Destination $asrOutput}}
    }
} finally {Pop-Location}
if ($ChatOnly) {Write-Output 'Companion chat built'} else {Write-Output 'Companion chat and offline speech built'}
