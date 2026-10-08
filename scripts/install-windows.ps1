# Installs Remote Hands for the current Windows user: venv, wheel, logon task. Safe to re-run.
param(
    [Parameter(Mandatory = $true)][string]$Wheel,
    # A per-user python.org install of Python 3.12 by default; pass another 3.12+ python.exe here.
    [string]$BasePython = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    # The service's port; the hands CLI must then use REMOTE_HANDS_URL=http://127.0.0.1:<port>/action.
    [int]$Port = 47821
)
$ErrorActionPreference = "Stop"

$TaskName = "RemoteHands"
$AppHome = Join-Path $env:LOCALAPPDATA "remote-hands"
$Venv = Join-Path $AppHome "venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$User = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $BasePython)) { throw "Python 3.12 not found at $BasePython (pass -BasePython)" }
New-Item -ItemType Directory -Force -Path $AppHome | Out-Null

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
}
if (-not (Test-Path $VenvPython)) {
    & $BasePython -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
# --no-index: install only our own wheel; nothing is fetched from PyPI.
& $VenvPython -m pip install --no-deps --no-index --force-reinstall $Wheel
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$Action = New-ScheduledTaskAction -Execute (Join-Path $Venv "Scripts\pythonw.exe") `
    -Argument "-m remote_hands.server --port $Port" -WorkingDirectory $AppHome
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
# Priority 4: normal CPU, I/O, and memory priority. The default 7 is below normal, and a Cursor the service starts would inherit it.
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -Priority 4
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Principal $Principal -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-List
