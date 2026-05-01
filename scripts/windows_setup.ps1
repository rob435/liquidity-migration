$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Invoke-Checked {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    Write-Host $Name
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE."
    }
}

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher 'py' was not found. Install Python 3.11 with: winget install -e --id Python.Python.3.11"
}

Invoke-Checked "Checking Python 3.11" { & py -3.11 --version }
Invoke-Checked "Creating virtualenv" { & py -3.11 -m venv .venv }

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Virtualenv creation failed: $Python not found."
}

Invoke-Checked "Upgrading pip" { & $Python -m pip install --upgrade pip }
Invoke-Checked "Installing requirements" { & $Python -m pip install -r requirements.txt }
Invoke-Checked "Running tests" { & $Python -m pytest -q }

Write-Host ""
Write-Host "Setup complete. Use .\.venv\Scripts\python.exe for repo commands."
