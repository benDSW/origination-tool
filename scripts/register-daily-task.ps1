# Registers the daily origination run (scripts\daily-run.cmd) in Windows Task Scheduler.
# Runs every day at 08:00 as the current user; if the PC was off or asleep at 08:00 it
# runs as soon as possible afterwards. Re-running this script updates the existing task.
# Remove it with:  Unregister-ScheduledTask -TaskName "DSW Origination Daily Run"
param([string]$At = "08:00")

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\daily-run.cmd"
if (-not (Test-Path $script)) { throw "Cannot find $script" }

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$script`"`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "DSW Origination Daily Run" -Force `
    -Description "Origination tool: new Evertrace signals, drafts, RocketReach backlog and reply tracking. Output in logs\daily.log." `
    -Action $action -Trigger $trigger -Settings $settings | Out-Null

$next = (Get-ScheduledTaskInfo -TaskName "DSW Origination Daily Run").NextRunTime
Write-Host "Registered 'DSW Origination Daily Run'. Next run: $next"
