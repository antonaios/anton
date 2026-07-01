<#
.SYNOPSIS
  Scaffold an ANTON vault — the Markdown knowledge store ("second brain").
.DESCRIPTION
  Creates the directory tree the operating constitution (vault/CLAUDE.md) expects,
  copies the shipped skeleton (Templates, Projects/_template, the constitution, and
  the _claude examples), and seeds profile.md / firm.md from the examples. Idempotent.
.PARAMETER VaultPath  Target vault directory (created if absent).
#>
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$VaultPath)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$skeleton = Join-Path $root 'vault'
if (-not (Test-Path $skeleton)) { throw "vault skeleton not found at $skeleton" }

New-Item -ItemType Directory -Force -Path $VaultPath | Out-Null

# 1. directory tree (vault/CLAUDE.md section 2 vault map)
$dirs = @(
  '_claude','Daily','Projects','Projects\_template','Projects\_Trackers','Archive',
  'People','Companies','Sectors','Templates',
  'Topics\Valuation','Topics\Process','Topics\Negotiation',
  'Resources\Newsletters','Resources\Earnings',
  'Inbox\HiNotes\incoming','Inbox\HiNotes\processed','Inbox\Emails','Inbox\VDR','Inbox\Captures',
  'Registers','Routines'
)
foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path (Join-Path $VaultPath $d) | Out-Null }

# 2. copy skeleton content into the pre-made dirs
Copy-Item (Join-Path $skeleton 'Templates\*')          (Join-Path $VaultPath 'Templates')          -Recurse -Force
Copy-Item (Join-Path $skeleton 'Projects\_template\*') (Join-Path $VaultPath 'Projects\_template') -Recurse -Force
foreach ($c in 'CLAUDE.md','Projects\CLAUDE.md') {
  $src = Join-Path $skeleton $c
  if (Test-Path $src) { Copy-Item $src (Join-Path $VaultPath $c) -Force }
}
Copy-Item (Join-Path $skeleton '_claude\profile.example.md') (Join-Path $VaultPath '_claude\profile.example.md') -Force
Copy-Item (Join-Path $skeleton '_claude\firm.example.md')    (Join-Path $VaultPath '_claude\firm.example.md')    -Force

# 3. seed profile.md / firm.md from the examples (only if absent)
$profile = Join-Path $VaultPath '_claude\profile.md'
if (-not (Test-Path $profile)) { Copy-Item (Join-Path $VaultPath '_claude\profile.example.md') $profile }
$firm = Join-Path $VaultPath '_claude\firm.md'
if (-not (Test-Path $firm)) { Copy-Item (Join-Path $VaultPath '_claude\firm.example.md') $firm }

# 4. empty append-only registers
foreach ($r in 'Sources','Decisions','Lessons') {
  $rf = Join-Path $VaultPath "Registers\$r.md"
  if (-not (Test-Path $rf)) { Set-Content -LiteralPath $rf -Value "# $r register`r`n" -Encoding utf8 }
}

Write-Host "Vault scaffolded at $VaultPath" -ForegroundColor Green
