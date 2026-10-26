#requires -Version 7.0
param([string]$PreparedDirectory,[string]$OutputDirectory)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path $PSScriptRoot -Parent
if(-not $PreparedDirectory){$PreparedDirectory=Join-Path $projectRoot '.tools/local-tts-melo-2.0.0'}
if(-not $OutputDirectory){$OutputDirectory=Join-Path $projectRoot 'build/desktop/backend/tts'}

function Assert-WorkspacePath([string]$Value) {
    $absolute=[IO.Path]::GetFullPath($Value)
    $boundary=[IO.Path]::GetFullPath($projectRoot).TrimEnd('\')
    if(-not $absolute.StartsWith($boundary+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Path must stay in this workspace'}
    $cursor=$absolute
    while(-not $cursor.Equals($boundary,[StringComparison]::OrdinalIgnoreCase)){
        if(Test-Path -LiteralPath $cursor){
            if((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reparse points are not supported'}
        }
        $cursor=[IO.Path]::GetDirectoryName($cursor)
        if(-not $cursor){throw 'Cannot establish workspace path boundary'}
    }
    return $absolute
}

$preparedRoot=Assert-WorkspacePath $PreparedDirectory
$targetRoot=Assert-WorkspacePath $OutputDirectory
$buildBoundary=[IO.Path]::GetFullPath((Join-Path $projectRoot 'build')).TrimEnd('\')+'\'
if(-not $targetRoot.StartsWith($buildBoundary,[StringComparison]::OrdinalIgnoreCase)){throw 'Helper output must be inside build/'}
if(Test-Path -LiteralPath $targetRoot){throw 'Helper destination already exists; select a new output directory'}
$python=Join-Path $projectRoot '.tools/python-3.13.13-embed/python.exe'
& $python -B (Join-Path $projectRoot 'tools/prepare-local-tts.py') --verify $preparedRoot
if($LASTEXITCODE -ne 0){throw 'Local TTS preparation SHA256 verification failed'}
$preparedManifest=Join-Path $preparedRoot 'local-tts-manifest.json'
$prepared=Get-Content -LiteralPath $preparedManifest -Raw | ConvertFrom-Json -AsHashtable
if($prepared.feature -ne 'local-tts-melo-20261025' -or $prepared.runtime_version -ne '2.0.0'){throw 'Unexpected prepared dependency identity'}
$runtimeRoot=Assert-WorkspacePath (Join-Path $preparedRoot $prepared.runtime_root)
$header=Join-Path $runtimeRoot 'include/sherpa-onnx/c-api/c-api.h'
if(-not (Test-Path -LiteralPath $header)){throw 'The unchanged upstream C API header is missing'}

. (Join-Path $PSScriptRoot 'enter-dev-env.ps1')
$compileRoot=Assert-WorkspacePath (Join-Path $projectRoot ('build/local-tts-build-'+[guid]::NewGuid().ToString('N')))
$payloadRoot=Join-Path $compileRoot 'payload'
New-Item -ItemType Directory -Path $payloadRoot | Out-Null
foreach($entry in $prepared.runtime_dlls.GetEnumerator()){
    if([IO.Path]::GetFileName($entry.Key) -ne $entry.Key -or -not $entry.Key.EndsWith('.dll')){throw 'Invalid runtime DLL basename'}
    $source=Assert-WorkspacePath (Join-Path $preparedRoot $entry.Value.path)
    if((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value.sha256){throw 'Runtime DLL changed after preparation'}
    Copy-Item -LiteralPath $source -Destination (Join-Path $payloadRoot $entry.Key)
}
foreach($name in $prepared.files.Keys){
    if(-not ($name.StartsWith('model/') -or $name.StartsWith('licenses/'))){continue}
    $source=Assert-WorkspacePath (Join-Path $preparedRoot $name)
    $destination=Assert-WorkspacePath (Join-Path $payloadRoot $name)
    New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination
}
$sourceFile=Join-Path $projectRoot 'app/desktop/local_tts_helper.cpp'
$executable=Join-Path $payloadRoot 'LocalTtsHelper.exe'
$objectFile=Join-Path $compileRoot 'LocalTtsHelper.obj'
& cl.exe /nologo /O2 /MT /EHsc /std:c++17 /utf-8 /W4 /DSHERPA_ONNX_BUILD_SHARED_LIBS /I (Join-Path $runtimeRoot 'include') $sourceFile "/Fo:$objectFile" "/Fe:$executable" /link kernel32.lib
if($LASTEXITCODE -ne 0){throw 'Local TTS helper build failed; isolated build artifacts retained'}
$probeText=& $executable --probe
if($LASTEXITCODE -ne 0){throw 'Local TTS helper probe failed'}
$probe=$probeText | ConvertFrom-Json
if(-not $probe.available -or $probe.runtime_version -ne '2.0.0' -or $probe.model_loaded -or $probe.playback){throw 'Local TTS runtime probe was unavailable; helper was not published'}
$files=@{}
Get-ChildItem -LiteralPath $payloadRoot -File -Recurse | ForEach-Object {
    $relative=[IO.Path]::GetRelativePath($payloadRoot,$_.FullName).Replace('\','/')
    $files[$relative]=@{size=$_.Length;sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
}
$manifest=@{schema_version=1;feature='local-tts-melo-20261025';runtime_version='2.0.0';provider='cpu';model=$prepared.voice;
    includes_project_adapter=$true;installed_to_system=$false;audio_played=$false;model_loaded_during_build=$false;
    lock_sha256=$prepared.lock_sha256;preparation_manifest_sha256=(Get-FileHash -LiteralPath $preparedManifest -Algorithm SHA256).Hash.ToLowerInvariant();
    source_sha256=@{'app/desktop/local_tts_helper.cpp'=(Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash.ToLowerInvariant();
        'tools/build-local-tts.ps1'=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()};
    files=$files;license_coverage=$prepared.license_coverage;probe=$probe}
$manifestPath=Join-Path $payloadRoot 'local-tts-manifest.json'
[IO.File]::WriteAllText($manifestPath,($manifest|ConvertTo-Json -Depth 16)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))

# Both resolved absolute paths are checked again immediately before the native
# directory move. This never moves/deletes a previously published helper.
$payloadRoot=Assert-WorkspacePath $payloadRoot
$targetRoot=Assert-WorkspacePath $targetRoot
if(Test-Path -LiteralPath $targetRoot){throw 'Helper destination appeared during the build'}
New-Item -ItemType Directory -Path (Split-Path $targetRoot -Parent) -Force | Out-Null
Move-Item -LiteralPath $payloadRoot -Destination $targetRoot
Write-Output (@{passed=$true;output=$targetRoot;files=$files.Count;audio_played=$false;model_loaded=$false}|ConvertTo-Json)
