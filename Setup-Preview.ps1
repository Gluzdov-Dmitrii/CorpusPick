# Extract a verified, dedicated LibreOffice runtime. No system installation.
$ErrorActionPreference = 'Stop'
$dependency = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime-dependencies.json') -Raw | ConvertFrom-Json).libreoffice
$version = $dependency.version
$expected = $dependency.sha256
$downloads = Join-Path $PSScriptRoot 'downloads'
$target = Join-Path $PSScriptRoot $dependency.install_directory
$package = Join-Path $downloads $dependency.package
$receiptPath = Join-Path $target '.corpuspick-runtime.json'
$required = @('program\soffice.com', 'program\soffice.bin', 'program\mergedlo.dll', 'share\registry\main.xcd',
              'program\uno.py', 'program\pyuno.pyd', 'program\fundamental.ini')
$complete = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $target $_)) }).Count -eq 0
if ($complete) {
    $installedVersion = (Get-Item -LiteralPath (Join-Path $target 'program\soffice.bin')).VersionInfo.FileVersion
    $complete = $installedVersion -and $installedVersion.StartsWith($version + '.')
}
if ($complete -and (Test-Path -LiteralPath $receiptPath)) {
    try {
        $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
        if ($receipt.version -eq $version -and $receipt.sha256 -eq $expected) {
            Write-Output "CorpusPick preview runtime ready: LibreOffice $version"
            exit 0
        }
    } catch { }
}
Write-Output 'Preparing local Office preview (first setup: approximately 356 MiB download, 1.5 GiB disk).'
New-Item -ItemType Directory -Path $downloads -Force | Out-Null
if (-not (Test-Path -LiteralPath $package)) {
    $partial = $package + '.partial'
    $downloaded = $false
    foreach ($url in $dependency.urls) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $partial -UseBasicParsing
            if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $expected) {
                throw 'Downloaded LibreOffice checksum mismatch.'
            }
            Move-Item -LiteralPath $partial -Destination $package -Force
            $downloaded = $true
            break
        } catch { Write-Warning 'Could not fetch the verified package from this source.' }
    }
    if (-not $downloaded) { throw 'Could not download verified LibreOffice. Retry Setup-Preview.ps1 when online.' }
}
if ((Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash -ne $expected) {
    throw 'LibreOffice package checksum mismatch; extraction cancelled.'
}
$signature = Get-AuthenticodeSignature -LiteralPath $package
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=The Document Foundation') {
    throw 'LibreOffice signature validation failed; extraction cancelled.'
}
if (-not $complete -or (Test-Path -LiteralPath $receiptPath)) {
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    $arguments = '/a "{0}" /qn TARGETDIR="{1}"' -f $package, $target
    $process = Start-Process msiexec.exe -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "LibreOffice extraction failed: $($process.ExitCode)" }
}
if (@($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $target $_)) }).Count -ne 0) {
    throw 'LibreOffice runtime was not extracted.'
}
@{version=$version;sha256=$expected} | ConvertTo-Json | Set-Content -LiteralPath $receiptPath -Encoding UTF8
Write-Output "CorpusPick preview runtime ready: LibreOffice $version"
