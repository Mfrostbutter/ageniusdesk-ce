# Run the LLM Cost forwarder on a Windows workstation.
#
# Token resolution, in order:
#   1. $env:AGD_LLM_COST_TOKEN
#   2. a DPAPI-encrypted cache, readable only by this user on this machine
#
# A scheduled task does not inherit an interactive shell's environment, so the
# installer saves the token to the DPAPI cache once (-SaveToken).
#
#   $env:AGD_LLM_COST_TOKEN = 'agdlc_...'; .\llm-cost-forwarder.ps1 -SaveToken
#   .\llm-cost-forwarder.ps1 -Url https://agd.example.com

[CmdletBinding()]
param(
    [string]$Url         = $env:AGD_URL,
    [int]   $IntervalSec = 60,
    [string]$Python      = "",
    [string]$Forwarder   = (Join-Path $PSScriptRoot "llm-cost-forward.py"),
    [string]$CachePath   = "$env:LOCALAPPDATA\agd-llm-cost\token.dpapi",
    [switch]$SaveToken,
    [switch]$Once
)

$ErrorActionPreference = "Stop"

function Get-TokenFromCache {
    if (-not (Test-Path $CachePath)) { return $null }
    try {
        $secure = Get-Content $CachePath | ConvertTo-SecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try   { return [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    } catch {
        Write-Warning "token cache unreadable: $_"
        return $null
    }
}

function Find-Python {
    if ($Python -and (Test-Path $Python)) { return $Python }
    foreach ($candidate in @("$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
                             "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch "WindowsApps" } | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    throw "python 3 not found; pass -Python C:\path\to\python.exe"
}

if ($SaveToken) {
    if (-not $env:AGD_LLM_COST_TOKEN) { throw "set `$env:AGD_LLM_COST_TOKEN first" }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $CachePath) | Out-Null
    ConvertTo-SecureString $env:AGD_LLM_COST_TOKEN -AsPlainText -Force |
        ConvertFrom-SecureString |
        Set-Content -Path $CachePath -Encoding ascii
    Write-Host "saved DPAPI-encrypted token cache to $CachePath"
    exit 0
}

if (-not $Url) { throw "pass -Url or set `$env:AGD_URL" }
if (-not (Test-Path $Forwarder)) { throw "forwarder not found at $Forwarder" }
$py = Find-Python

# Single instance: an orphaned child from a stopped task would fight the new one.
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match "llm-cost-forward" -and $_.ProcessId -ne $PID } |
    ForEach-Object {
        Write-Host "stopping stale forwarder pid $($_.ProcessId)"
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
    }

$token = $env:AGD_LLM_COST_TOKEN
if (-not $token) { $token = Get-TokenFromCache }
if (-not $token) { throw "no device token. Run with -SaveToken after setting `$env:AGD_LLM_COST_TOKEN." }

# Passed through the environment, never argv, so it stays out of the process list.
$env:AGD_LLM_COST_TOKEN = $token

$forwarderArgs = @($Forwarder, "--url", $Url, "--interval", $IntervalSec)
if ($Once) { $forwarderArgs += "--once" }

Write-Host "starting LLM Cost forwarder -> $Url (every ${IntervalSec}s)"
& $py @forwarderArgs
