# Rollback: removes the logon task and the venv. Token, audit log, and kill switch remain.
param([int]$Port = 47821)
$ErrorActionPreference = "Stop"
$TaskName = "RemoteHands"
$AppHome = Join-Path $env:LOCALAPPDATA "remote-hands"
$Venv = Join-Path $AppHome "venv"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Start-Sleep -Seconds 2
$Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($Listener) {
    throw "Port $Port still has a listener (pid $($Listener.OwningProcess)); stop it, then rerun."
}
if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
Write-Output "Removed $TaskName and its venv; port $Port is free. Delete $AppHome to remove the token and audit log too."
