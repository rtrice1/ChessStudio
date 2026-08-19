# Register the desk's morning wake-up with Windows Task Scheduler.
# Fires weekdays 08:00 local (= 09:00 ET on this Central-time box) — 30
# minutes before the open. Idempotent: re-running replaces the task.
# The PC must be awake — Windows sleep suspends everything (DEPLOY.md).

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "deploy\windows_morning.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 08:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName "TraderDesk-Morning" -Action $action `
    -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Registered 'TraderDesk-Morning': weekdays 08:00 local (09:00 ET)."
Write-Host "Verify:  Get-ScheduledTask TraderDesk-Morning | Get-ScheduledTaskInfo"
Write-Host "Run now: Start-ScheduledTask TraderDesk-Morning"
