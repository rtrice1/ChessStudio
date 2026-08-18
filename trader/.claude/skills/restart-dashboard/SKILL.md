---
name: restart-dashboard
description: Restart the desk dashboard (agent.dashboard on :8899) — kills any running instance, relaunches it with the project venv and Schwab env vars hydrated from the Windows User scope, and verifies it serves. Use when the dashboard code changed, the page is stale/dead, or the user asks to restart/start the dashboard.
---

# Restart the desk dashboard

The dashboard is a stateless view (`agent/dashboard.py`) — killing and
relaunching it is always safe; it owns no desk state.

Run these steps with the PowerShell tool from the repo root
(`c:\Users\rtric\ChessStudio\trader`):

## 1. Stop any running instance

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -like "python*" -and
                 $_.CommandLine -like "*agent.dashboard*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 1
```

The `$_.Name -like "python*"` guard is load-bearing: without it the
filter matches the PowerShell process running this very command (the
pattern text appears in its own CommandLine) and the shell kills itself.
The sleep lets the port clear before relaunch.

No error if none was running. (This also stops a copy the user started in
their own terminal — that is expected for a restart; tell them it now
runs in the background instead.)

## 2. Hydrate Schwab env vars if the shell lacks them

Tool shells may predate the user-scope variables. Never print the
values.

```powershell
foreach ($n in "SCHWAB_APP_KEY","SCHWAB_APP_SECRET","SCHWAB_REDIRECT_URI") {
  if (-not (Get-Item "env:$n" -ErrorAction SilentlyContinue)) {
    $v = [Environment]::GetEnvironmentVariable($n, "User")
    if ($v) { Set-Item "env:$n" $v }
  }
}
```

## 3. Launch, headless, on the project venv

```powershell
Start-Process -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList "-m","agent.dashboard" -WindowStyle Hidden
```

If diagnosing a dashboard that dies at startup, add
`-RedirectStandardError "$env:TEMP\dash_err.txt"` and read that file —
a hidden window swallows the traceback otherwise.

## 4. Verify (probe again after a pause if the first races the startup)

```powershell
Start-Sleep -Seconds 3
(Invoke-RestMethod "http://127.0.0.1:8899/state").ts -gt 0
$auth = Invoke-RestMethod "http://127.0.0.1:8899/auth/schwab"
"configured: $($auth.configured)  has_tokens: $($auth.has_tokens)"
```

Report to the user: the dashboard URL (http://127.0.0.1:8899/), whether
Schwab shows `configured`, and remind them to hard-refresh the browser
tab (Ctrl+Shift+R) so the new page assets load. If `configured` is
false, the keys are missing from the User scope — point them at
deploy/SCHWAB.md rather than asking for secrets in chat.
