# Install the LLM Cost forwarder as a Windows scheduled task (runs at logon, headless).
#
#   $env:AGD_LLM_COST_TOKEN = 'agdlc_...'
#   .\llm-cost-install-task.ps1 -Url https://agd.example.com
#   .\llm-cost-install-task.ps1 -Remove
#
# Copies the forwarder into %LOCALAPPDATA%\agd-llm-cost (from next to this script,
# else downloads it from the dashboard) and caches the token with DPAPI.

[CmdletBinding()]
param(
    [string]$Url        = $env:AGD_URL,
    [string]$TaskName   = "AGD LLM Cost Forwarder",
    [string]$InstallDir = "$env:LOCALAPPDATA\agd-llm-cost",
    [int]   $IntervalSec = 60,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed existing task '$TaskName'"
}
if ($Remove) { exit 0 }
if (-not $Url) { throw "pass -Url or set `$env:AGD_URL" }
$Url = $Url.TrimEnd("/")

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($name in @("llm-cost-forward.py", "llm-cost-forwarder.ps1")) {
    $target = Join-Path $InstallDir $name
    $local = if ($PSScriptRoot) { Join-Path $PSScriptRoot $name } else { "" }
    if ($local -and (Test-Path $local) -and ((Resolve-Path $local).Path -ne $target)) {
        Copy-Item $local $target -Force
    } elseif (-not $local -or -not (Test-Path $local)) {
        Invoke-WebRequest -UseBasicParsing "$Url/api/llm-cost/forwarder/$name" -OutFile $target
    }
}

$wrapper = Join-Path $InstallDir "llm-cost-forwarder.ps1"
$cache = Join-Path $InstallDir "token.dpapi"
if ($env:AGD_LLM_COST_TOKEN) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wrapper -SaveToken -CachePath $cache
} elseif (-not (Test-Path $cache)) {
    throw "no token cache at $cache. Set `$env:AGD_LLM_COST_TOKEN and re-run."
}

# conhost --headless: Windows Terminal ignores -WindowStyle Hidden and would show a tab.
$argument = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$wrapper`" " +
            "-Url `"$Url`" -IntervalSec $IntervalSec -CachePath `"$cache`""
$action = New-ScheduledTaskAction -Execute "conhost.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 5) -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Pushes local Claude Code token usage to the AgeniusDesk LLM Cost view." | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "registered and started '$TaskName' -> $Url"
