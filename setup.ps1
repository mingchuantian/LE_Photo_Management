param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    if (-not $PythonPath) {
        $runtimePython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $runtimePython) { $PythonPath = $runtimePython }
        else { $PythonPath = 'python' }
    }
    & $PythonPath -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create Python environment. Use Python 3.11-3.13.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& '.\.venv\Scripts\python.exe' -c "import flask, PIL, openai, httpx; print('Dependencies ready.')"
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed. Use a standard CPython installation rather than an Anaconda environment.' }
Write-Host 'Ready. Run start.ps1 or double-click start.bat.'
