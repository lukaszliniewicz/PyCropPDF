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

Push-Location $repo
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --noconsole `
        --additional-hooks-dir $hooks `
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
    Pop-Location
}
