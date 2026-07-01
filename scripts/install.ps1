<#
.SYNOPSIS
  One-shot installer for ANTON (Windows).
.DESCRIPTION
  Portable: resolves the repo root from this script's own location. Creates a Python
  virtual environment, installs the bridge + valuation engine (editable), builds the
  React dashboard, and scaffolds a vault (directory tree + templates + a starter
  profile). Safe to re-run.
.PARAMETER VaultPath
  Where to create your vault (the Markdown knowledge store). Default: <repo>\vault-data.
.PARAMETER WithExtras
  Also install the optional [markets] (OpenBB, AGPL — not vendored), [learning],
  and [recall] dependency groups. Heavy; not needed for the bridge hot path.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -VaultPath C:\anton-vault
#>
[CmdletBinding()]
param(
  [string]$VaultPath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'vault-data'),
  [switch]$WithExtras
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

Write-Host ""
Write-Host "=== ANTON installer ===" -ForegroundColor Cyan
Write-Host "    repo root : $root"
Write-Host "    vault     : $VaultPath"
Write-Host ""

# --- prerequisites ---------------------------------------------------------
foreach ($cmd in 'python','node','npm') {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    throw "'$cmd' was not found on PATH. Install it first (Python 3.13+, Node 18+), then re-run."
  }
}

# --- 1. Python venv + editable installs ------------------------------------
$venv = Join-Path $root '.venv'
if (-not (Test-Path $venv)) {
  Write-Host "[1/4] creating virtual environment ..." -ForegroundColor Cyan
  python -m venv $venv
}
$py = Join-Path $venv 'Scripts\python.exe'
Write-Host "[1/4] installing bridge + engine (editable) ..." -ForegroundColor Cyan
& $py -m pip install --upgrade pip | Out-Null
$routinesSpec = Join-Path $root 'routines'
if ($WithExtras) { $routinesSpec = "$routinesSpec[markets,learning,recall]" }
& $py -m pip install -e $routinesSpec
if ($LASTEXITCODE -ne 0) { throw "pip install routines failed (exit $LASTEXITCODE)" }
& $py -m pip install -e (Join-Path $root 'engine')
if ($LASTEXITCODE -ne 0) { throw "pip install engine failed (exit $LASTEXITCODE)" }

# --- 2. dashboard build ----------------------------------------------------
Write-Host "[2/4] building dashboard (npm install + build) ..." -ForegroundColor Cyan
Push-Location (Join-Path $root 'dashboard')
try {
  npm install
  if ($LASTEXITCODE -ne 0) { throw "npm install failed (exit $LASTEXITCODE)" }
  npm run build
  if ($LASTEXITCODE -ne 0) { throw "npm run build failed (exit $LASTEXITCODE)" }
} finally { Pop-Location }

# --- 3. scaffold the vault -------------------------------------------------
Write-Host "[3/4] scaffolding vault ..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot 'scaffold-vault.ps1') -VaultPath $VaultPath

# --- 4. .env ---------------------------------------------------------------
Write-Host "[4/4] writing .env ..." -ForegroundColor Cyan
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
  $lines = Get-Content (Join-Path $root '.env.example')
  $lines = $lines -replace '^AGENTIC_VAULT=.*',         ("AGENTIC_VAULT=" + $VaultPath)
  $lines = $lines -replace '^AGENTIC_DASHBOARD_MODE=.*', 'AGENTIC_DASHBOARD_MODE=production'
  Set-Content -LiteralPath $envFile -Value $lines -Encoding utf8
  Write-Host "    wrote $envFile (vault path set; edit it to add API keys)"
} else {
  Write-Host "    $envFile already exists - left untouched"
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Next steps:" -ForegroundColor Green
Write-Host "  1. (optional) install Ollama + pull models for the local reasoning lane:"
Write-Host "       ollama pull qwen3:14b ; ollama pull qwen3:8b ; ollama pull nomic-embed-text"
Write-Host "  2. start ANTON:  powershell -ExecutionPolicy Bypass -File scripts\start-agentic-os.ps1"
Write-Host "  3. open:         http://127.0.0.1:8765/"
Write-Host "  4. set who is operating it: edit $VaultPath\_claude\profile.md"
