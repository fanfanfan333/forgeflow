# ForgeFlow — idempotent local launch (backend :8010, Vite :5173) on Windows.
#
# Must be launched through WMI: the sandbox job object forbids
# CREATE_BREAKAWAY_FROM_JOB (WinError 5), so a plain Start-Process child is
# reaped as soon as the shell exits. Win32_Process.Create is the best escape available here -- but NOT a full one: inside the agent sandbox the child stays in the job (its PPID is the invoking shell host) so the whole tree is reaped ~90-100s after launch; in a normal user console (no job object) it persists. Do not read a transient UP as durable.
#
# The two wrapper .py files own the per-service detail (environment, logging,
# and the --reload flag the backend needs to get a Selector event loop). They
# derive the repo root from their own __file__, and this launcher passes the
# WMI working directory ($cwd) as $PSScriptRoot — so the whole trio is
# relocatable and the project can be moved again without editing anything.
#
# This file MUST stay UTF-8-with-BOM. Windows PowerShell 5.1 decodes a BOM-less
# .ps1 as ANSI; that mangles non-ASCII text in the script and can make the WMI
# launch fail silently (no error, nothing starts).

$ErrorActionPreference = 'Stop'

# Derive the working directory from this script's own location instead of
# hardcoding an absolute path: the project has already been moved once (the old
# root no longer exists), and a hardcoded path silently breaks every future
# move. $PSScriptRoot is available in PowerShell 3+.
if ([string]::IsNullOrEmpty($PSScriptRoot)) {
    throw '$PSScriptRoot is empty; run this script from a file path, not stdin/eval.'
}
$cwd = $PSScriptRoot
$py  = 'C:\Users\18769\.workbuddy\binaries\python\versions\3.13.12\python.exe'

function Test-Port([int]$Port) {
    $c = New-Object System.Net.Sockets.TcpClient
    try   { $c.Connect('127.0.0.1', $Port); return $true }
    catch { return $false }
    finally { $c.Close() }
}

function Start-Detached([string]$Script) {
    $cmd = '"' + $py + '" "' + (Join-Path $cwd $Script) + '"'
    $r = ([wmiclass]'Win32_Process').Create($cmd, $cwd)
    if ($r.ReturnValue -ne 0) { throw "WMI Create failed for $Script (rc=$($r.ReturnValue))" }
    return $r.ProcessId
}

if (-not (Test-Port 8010)) {
    $b = Start-Detached '_run_backend_real.py'
    "backend  : launched pid=$b"
} else {
    "backend  : already up on :8010"
}

if (-not (Test-Port 5173)) {
    $f = Start-Detached '_run_frontend_real.py'
    "frontend : launched pid=$f"
} else {
    "frontend : already up on :5173"
}

""
"waiting for listeners..."
$deadline = (Get-Date).AddSeconds(150)
while ((Get-Date) -lt $deadline) {
    if ((Test-Port 8010) -and (Test-Port 5173)) { break }
    Start-Sleep -Milliseconds 1500
}
"backend  :8010 " + $(if (Test-Port 8010) { 'UP' } else { 'DOWN' })
"frontend :5173 " + $(if (Test-Port 5173) { 'UP' } else { 'DOWN' })
