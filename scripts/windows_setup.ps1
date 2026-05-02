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

function Get-CompatiblePython {
    $Candidates = @(
        @{ Exe = "py"; Args = @("-3") },
        @{ Exe = "python"; Args = @() },
        @{ Exe = "python3"; Args = @() }
    )

    foreach ($Candidate in $Candidates) {
        if (-not (Get-Command $Candidate.Exe -ErrorAction SilentlyContinue)) {
            continue
        }

        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $Candidate.Exe @($Candidate.Args) -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
            if ($LASTEXITCODE -eq 0) {
                return $Candidate
            }
        }
        catch {
            continue
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
    }

    throw "No compatible Python found. Install Python 3.11 or newer from https://www.python.org/downloads/windows/ and enable the Python launcher. Microsoft Store is not required."
}

$BasePython = Get-CompatiblePython
Invoke-Checked "Checking Python >=3.11" {
    & $BasePython.Exe @($BasePython.Args) -c "import sys; print(sys.executable); print(sys.version)"
}
Invoke-Checked "Creating Python virtualenv" {
    & $BasePython.Exe @($BasePython.Args) -m venv --clear .venv
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Virtualenv creation failed: $Python not found."
}

Invoke-Checked "Upgrading pip" { & $Python -m pip install --upgrade pip }
Invoke-Checked "Installing requirements" { & $Python -m pip install -r requirements.txt }
Invoke-Checked "Running tests" { & $Python -m pytest -q }

Write-Host ""
Write-Host "Setup complete. Use .\.venv\Scripts\python.exe for repo commands."
