<#
.SYNOPSIS
  Create or remove the Windows scheduled tasks for live alerts, the daily recap and automatic posting.

.EXAMPLE
  .\schedule_windows.ps1 -Alerts -Recap -RecapTime 21:00
  .\schedule_windows.ps1 -AutoPost
  .\schedule_windows.ps1 -Alerts -DryRun
  .\schedule_windows.ps1 -Remove
#>
param(
    [switch]$Alerts,
    [switch]$Recap,
    [switch]$AutoPost,
    [string]$RecapTime = "21:00",
    [int]$AlertMinutes = 2,
    [int]$AutoPostMinutes = 30,
    [switch]$DryRun,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$Prefix = "Stream Clip Studio"
$Here = $PSScriptRoot
$Python = Join-Path $Here ".venv\Scripts\pythonw.exe"

if ($Remove) {
    foreach ($name in "Live Alert", "Daily Recap", "Auto Post") {
        $task = "$Prefix $name"
        if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
            if ($DryRun) { "Would remove: $task" } else { Unregister-ScheduledTask -TaskName $task -Confirm:$false; "Removed: $task" }
        }
    }
    return
}
if (-not ($Alerts -or $Recap -or $AutoPost)) { throw "Choose at least one of -Alerts, -Recap, -AutoPost, or use -Remove." }
if (-not $DryRun -and -not (Test-Path $Python)) { throw "Python environment not found at $Python. Follow the install steps in the README first." }

function Register-ClipTask($Name, $Script, $Trigger) {
    $task = "$Prefix $Name"
    if ($DryRun) { "Would schedule: $task -> $Python $Script"; return }
    $action = New-ScheduledTaskAction -Execute $Python -Argument "`"$(Join-Path $Here $Script)`"" -WorkingDirectory $Here
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $Trigger -Settings $settings -Force | Out-Null
    "Scheduled: $task"
}

function New-RepeatTrigger($Minutes) {
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
}

if ($Alerts) { Register-ClipTask "Live Alert" "live_alert.py" (New-RepeatTrigger $AlertMinutes) }
if ($Recap) { Register-ClipTask "Daily Recap" "recap.py" (New-ScheduledTaskTrigger -Daily -At $RecapTime) }
if ($AutoPost) { Register-ClipTask "Auto Post" "post_queue.py" (New-RepeatTrigger $AutoPostMinutes) }
