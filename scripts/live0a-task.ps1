# FORWARD_PAPER / NON_EXECUTING. Default action ONLY generates a disabled task XML.
[CmdletBinding()]
param(
    [ValidateSet('Generate','RegisterDisabled')][string]$Action = 'Generate',
    [Parameter(Mandatory=$true)][string]$StartBoundary,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [string]$PythonwPath = 'C:\dev\trading-agent\.venv\Scripts\pythonw.exe'
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonTarget = [IO.Path]::GetFullPath($PythonwPath)
if ($pythonTarget -match 'codex-runtimes' -or $pythonTarget -ne (Join-Path $projectRoot '.venv\Scripts\pythonw.exe')) {
    throw 'Use the reviewed project-owned .venv pythonw.exe; cache runtimes are forbidden.'
}
if (-not $StartBoundary.EndsWith('Z')) { throw 'Explicit UTC StartBoundary required.' }
$start = [DateTimeOffset]::Parse($StartBoundary)
if ($start -le [DateTimeOffset]::UtcNow -or $start.Second -ne 0 -or $start.Millisecond -ne 0 -or ($start.Minute % 15) -ne 3) {
    throw 'Choose a future UTC :03/:18/:33/:48 boundary.'
}
$taskName = 'TradingAgent-LIVE-0A-live-0a-001'
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
function XmlEscape([string]$Value) { [System.Security.SecurityElement]::Escape($Value) }
$xml = @"
<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>FORWARD_PAPER / NON_EXECUTING public-only LIVE-0A.1</Description></RegistrationInfo>
  <Triggers><TimeTrigger><Repetition><Interval>PT15M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$(XmlEscape $StartBoundary)</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Principals><Principal id="Observer"><UserId>$(XmlEscape $taskUser)</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><Enabled>false</Enabled><RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT10M</ExecutionTimeLimit></Settings>
  <Actions Context="Observer"><Exec><Command>$(XmlEscape $pythonTarget)</Command><Arguments>-m forward poll --run-id live-0a-001 --public-data-only</Arguments><WorkingDirectory>$(XmlEscape $projectRoot)</WorkingDirectory></Exec></Actions>
</Task>
"@
if ($Action -eq 'Generate') {
    $stream = [IO.File]::Open([IO.Path]::GetFullPath($OutputPath), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try {
        $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($xml)
        $stream.Write($bytes,0,$bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
    Write-Output 'FORWARD_PAPER / NON_EXECUTING: disabled XML generated; NOTHING REGISTERED.'
    if (-not (Test-Path -LiteralPath $pythonTarget)) {
        Write-Output 'FORWARD_PAPER / NON_EXECUTING: proposed project runtime is missing; registration blocked.'
    }
    return
}
# Registration is an explicit later action. Never overwrite an existing task.
if (-not (Test-Path -LiteralPath $pythonTarget)) { throw 'Reviewed stable Python runtime is not installed.' }
if ([IO.File]::ReadAllText([IO.Path]::GetFullPath($OutputPath)) -ne $xml) { throw 'Task XML differs from this reviewed configuration.' }
$consolePython = Join-Path (Split-Path $pythonTarget) 'python.exe'
Push-Location -LiteralPath $projectRoot
try {
    & $consolePython -c "from forward.manifest import ROOT,load; load(ROOT/'artifacts/forward-paper/live-0a-001')"
    if ($LASTEXITCODE -ne 0) { throw 'Operational binding or stable-runtime identity not approved.' }
} finally { Pop-Location }
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Task already exists; no replacement allowed.' }
Register-ScheduledTask -TaskName $taskName -Xml $xml | Out-Null
Write-Output 'FORWARD_PAPER / NON_EXECUTING: task registered DISABLED; enabling requires separate authorization.'
