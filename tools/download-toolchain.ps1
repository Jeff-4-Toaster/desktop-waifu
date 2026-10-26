#requires -Version 7.0
param(
    [string]$AssetName
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$downloadRoot = Join-Path $projectRoot '.tools/downloads'
$assets = @(Get-Content -LiteralPath (Join-Path $downloadRoot 'release-assets.json') -Raw | ConvertFrom-Json)
if ($AssetName) {
    $assets = @($assets | Where-Object name -eq $AssetName)
    if ($assets.Count -ne 1) { throw "Unknown or ambiguous tool asset: $AssetName" }
}
foreach ($asset in $assets) {
    $destination = Join-Path $downloadRoot $asset.name
    $expectedHash = $asset.digest -replace '^sha256:', ''
    if ($expectedHash -notmatch '^[a-fA-F0-9]{64}$') { throw 'Missing official SHA256 digest' }
    $valid = (Test-Path -LiteralPath $destination) -and ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -eq $expectedHash)
    if (-not $valid) {
        & curl.exe --fail --location --retry 3 --connect-timeout 20 --max-time 1800 --silent --show-error --output "$destination.partial" $asset.browser_download_url
        if ($LASTEXITCODE -ne 0) { throw "Download failed: $($asset.name)" }
        if ((Get-FileHash -LiteralPath "$destination.partial" -Algorithm SHA256).Hash -ne $expectedHash) {
            throw "Official SHA256 mismatch: $($asset.name)"
        }
        Move-Item -LiteralPath "$destination.partial" -Destination $destination -Force
    }
    Write-Output "Verified $($asset.name): $expectedHash"
}
