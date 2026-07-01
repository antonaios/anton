<#
.SYNOPSIS  Stop the ANTON bridge (the process listening on the bridge port, default 8765).
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'SilentlyContinue'
$port = if ($env:AGENTIC_API_PORT) { [int]$env:AGENTIC_API_PORT } else { 8765 }
$procIds = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
           Select-Object -ExpandProperty OwningProcess -Unique
if ($procIds) {
  $procIds | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
  Write-Host "ANTON bridge on :$port stopped."
} else {
  Write-Host "No ANTON bridge listening on :$port."
}
