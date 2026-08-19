# Register the desk's morning wake-up with Windows Task Scheduler.
# Fires weekdays at the LOCAL equivalent of 09:00 ET — 30 minutes before
# the open — computed at install time from the machine's own timezone, so
# it is correct whether the box thinks it's Eastern, Central, or Pacific.
# IF THE MACHINE'S TIMEZONE EVER CHANGES, RERUN THIS SCRIPT — Task
# Scheduler triggers are local-time and will NOT adjust themselves.
# Idempotent: re-running replaces the task. The PC must be awake —
# Windows sleep suspends everything (DEPLOY.md).

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "deploy\windows_morning.ps1"

$eastern = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$etNine = [datetime]::SpecifyKind((Get-Date).Date.AddHours(9), "Unspecified")
$localAt = [System.TimeZoneInfo]::ConvertTime($etNine, $eastern,
                                              [System.TimeZoneInfo]::Local)
Write-Host ("Machine timezone: {0}; 09:00 ET = {1} local" -f `
    (Get-TimeZone).Id, $localAt.ToString("HH:mm"))

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At $localAt.ToString("HH:mm")
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName "TraderDesk-Morning" -Action $action `
    -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host ("Registered 'TraderDesk-Morning': weekdays {0} local (09:00 ET)." `
    -f $localAt.ToString("HH:mm"))
Write-Host "Verify:  Get-ScheduledTask TraderDesk-Morning | Get-ScheduledTaskInfo"
Write-Host "Run now: Start-ScheduledTask TraderDesk-Morning"
