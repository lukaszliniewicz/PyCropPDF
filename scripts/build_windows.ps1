param(
    [string]$Python = "python",
    [string]$Name = "PyCropPDF-windows-x86_64",
    [string]$DistPath = "dist"
)

$ErrorActionPreference = "Stop"
$repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$hooks = Join-Path $repo "packaging\pyinstaller_hooks"
$work = Join-Path $repo "build\pyinstaller\$Name"
$spec = Join-Path $work "spec"
New-Item -ItemType Directory -Force -Path $work, $spec | Out-Null

$pythonPrefix = (& $Python -c "import sys; print(sys.prefix)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $pythonPrefix) {
    throw "Could not determine the selected Python environment."
}
$runtimePaths = @(
    $pythonPrefix,
    (Join-Path $pythonPrefix "Library\bin"),
    (Join-Path $pythonPrefix "DLLs")
) | Where-Object { Test-Path -LiteralPath $_ }
$originalPath = $env:PATH
$env:PATH = (($runtimePaths + $originalPath) -join [System.IO.Path]::PathSeparator)

Push-Location $repo
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --noconsole `
        --additional-hooks-dir $hooks `
        --hidden-import deskew `
        --name $Name `
        --distpath $DistPath `
        --workpath $work `
        --specpath $spec `
        run.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
}
finally {
    $env:PATH = $originalPath
    Pop-Location
}
