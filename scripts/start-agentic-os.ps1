<#
.SYNOPSIS
  Portable ANTON launcher — serves the bridge + built dashboard at http://127.0.0.1:8765.
.DESCRIPTION
  Resolves the repo root from this script's own location, loads .env into the process
  environment (the bridge reads os.environ directly; it does not parse .env itself),
  forces dashboard production mode, and starts the FastAPI bridge in the foreground.
  Ctrl-C to stop (or run scripts\stop-agentic-os.ps1 from another shell).
.PARAMETER EnvFile
  Path to the .env file. Default: <repo>\.env.
#>
[CmdletBinding()]
param([string]$EnvFile)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (-not $EnvFile) { $EnvFile = Join-Path $root '.env' }

# Load .env -> process environment (KEY=VALUE lines; '#' comments ignored).
if (Test-Path $EnvFile) {
  foreach ($line in Get-Content $EnvFile) {
    $t = $line.Trim()
    if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
      $i = $t.IndexOf('=')
      $k = $t.Substring(0, $i).Trim()
      $v = $t.Substring($i + 1).Trim()
      if ($k) { [Environment]::SetEnvironmentVariable($k, $v, 'Process') }
    }
  }
} else {
  Write-Host "note: no .env at $EnvFile - using defaults / existing environment." -ForegroundColor DarkYellow
}

if (-not $env:AGENTIC_DASHBOARD_MODE) { $env:AGENTIC_DASHBOARD_MODE = 'production' }

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }   # fall back to PATH python

$port = if ($env:AGENTIC_API_PORT) { $env:AGENTIC_API_PORT } else { '8765' }
Write-Host "ANTON bridge -> http://127.0.0.1:$port/" -ForegroundColor Cyan
Write-Host "    vault: $env:AGENTIC_VAULT" -ForegroundColor DarkGray
Write-Host "    Ctrl-C to stop." -ForegroundColor DarkGray
& $py -m routines.api.app
