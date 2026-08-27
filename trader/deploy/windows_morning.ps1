# The desk's Windows morning ritual - scheduled for 09:00 ET (08:00 local,
# this box runs US Central; ET-4/CT-5 shift together through DST so the
# 1-hour offset holds year-round). Registered by deploy/install_windows.ps1
# as a Task Scheduler job, weekdays. A fresh clean slate every morning:
# stale processes killed, logs rotated, scan run, both processes relaunched.
# The BOOK is not reset - the $10k allocation and its record carry forward;
# "clean slate" means processes and charts, not the ledger.

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot   # deploy/ -> repo root
Set-Location $repo
$py = Join-Path $repo ".venv\Scripts\python.exe"
$log = Join-Path $repo "data\morning.log"
New-Item -ItemType Directory -Force (Join-Path $repo "data") | Out-Null

function Log($msg) {
    "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))  $msg" | Add-Content $log
}
Log "=== morning ritual start ==="

# Weekend guard (task is weekday-scheduled, but belt and suspenders).
if ((Get-Date).DayOfWeek -in "Saturday", "Sunday") { Log "weekend - skip"; exit 0 }

# 1. Hydrate user-scope credentials (scheduled tasks usually inherit them,
#    but a stale session's env must never decide the desk's morning).
foreach ($n in "SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_REDIRECT_URI",
              "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET") {
    $v = [Environment]::GetEnvironmentVariable($n, "User")
    if ($v) { Set-Item "env:$n" $v }
}

# 2. Fresh slate: stop yesterday's processes, rotate the session log.
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like "python*" -and
                   ($_.CommandLine -like "*run_live*" -or
                    $_.CommandLine -like "*agent.dashboard*") } |
    ForEach-Object { Log "stopping stale PID $($_.ProcessId)";
                     Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$sessionLog = Join-Path $repo "data\run_live.log"
if (Test-Path $sessionLog) {
    Move-Item $sessionLog (Join-Path $repo "data\run_live.prev.log") -Force
}

# 3. Overnight/pre-market rumor scan (targets today when run before 09:30).
& $py -m agent.rumors scan 2>&1 | ForEach-Object { Log "rumors: $_" }

# 4. Checkpoint into the log - the morning's self-inspection, on the record.
& $py -m agent.checkpoint 2>&1 | ForEach-Object { Log $_ }

# 4b. Prove data access BEFORE launching. Schwab expires refresh tokens
#     every ~7 days; a dead token means a blind desk (2026-08-25: a full
#     session of failed polls). The warning below must be impossible to
#     miss in the log; we still launch, so a mid-morning re-auth from the
#     dashboard panel lets the session recover on its own.
#     ASCII ONLY in this file: PS 5.1 reads BOM-less files as ANSI, and a
#     UTF-8 em-dash inside a string decodes into a smart quote that
#     TERMINATES THE STRING (2026-08-26/27: parse error, task ran nothing).
$schwabOut = & $py -m agent.schwab test 2>&1
$schwabOk = ($LASTEXITCODE -eq 0)
$schwabOut | Select-Object -First 2 | ForEach-Object { Log "schwab: $_" }
if (-not $schwabOk) {
    1..3 | ForEach-Object {
        Log "!!! SCHWAB DATA ACCESS FAILED - THE DESK IS BLIND UNTIL RE-AUTH !!!"
    }
    Log "!!! Fix now: dashboard :8899 -> Schwab connection -> weekly login !!!"
}

# 5. Launch the day: dashboard first, then the runner (it idles until 09:30).
Start-Process -FilePath $py -ArgumentList "-m", "agent.dashboard" -WindowStyle Hidden
Start-Process -FilePath $py -ArgumentList "-u", "-m", "agent.run_live", "--starting-cash", "10000" `
    -WindowStyle Hidden `
    -RedirectStandardOutput $sessionLog `
    -RedirectStandardError (Join-Path $repo "data\run_live.err.log")
Log "dashboard + run_live launched; runner waits for the bell"
Log "=== morning ritual done ==="
