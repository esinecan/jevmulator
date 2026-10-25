<#
.SYNOPSIS
    Start, inspect and stop the Jevmulator daemon on Windows.

.DESCRIPTION
    Wraps "python -m jevmulator serve". The daemon runs in a hidden background process
    and writes .jevmulator/runtime.json once it has bound its socket. This script adds the
    process start time to that file, so Stop can verify process identity instead of
    trusting a bare process id.

    All file reads and writes use UTF-8 without a byte order mark.

.PARAMETER Command
    start, status, stop, restart, supervise or install.

    install registers the scheduled task jevmulator-daemon. The task runs supervise through
    a waiting wscript shim at logon and every 5 minutes. supervise owns the daemon: it
    respawns the daemon when it exits and kills it when its health route stops answering.
    While that task exists, start, stop, restart and status act through the supervisor.

.PARAMETER Port
    TCP port. Default 8769. Start records the port, so Status and Stop need no -Port.

.PARAMETER ShowKey
    Status only. Print the daemon API key from the runtime file.

.EXAMPLE
    .\jevmulator.ps1 start
    .\jevmulator.ps1 start -Port 8770
    .\jevmulator.ps1 status
    .\jevmulator.ps1 status -ShowKey
    .\jevmulator.ps1 stop
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('start', 'status', 'stop', 'restart', 'supervise', 'install')]
    [string]$Command,

    [ValidateRange(1, 65535)]
    [int]$Port = 0,

    [switch]$ShowKey,

    [int]$ReadyTimeoutSeconds = 45,

    [string]$Python = '',

    [string]$Shim = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$DefaultPort = 8769
$ScriptPath = $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptPath
$StateDir = Join-Path $ProjectRoot '.jevmulator'
$RuntimeFile = Join-Path $StateDir 'runtime.json'
$OutLog = Join-Path $StateDir 'daemon.out.log'
$ErrLog = Join-Path $StateDir 'daemon.err.log'
$StartTrace = Join-Path $StateDir 'start-trace.log'
$ScriptLaunch = (Get-Process -Id $PID).StartTime

$TaskName = 'jevmulator-daemon'
$SupervisorPidFile = Join-Path $StateDir 'supervisor.pid'
$SupervisorLog = Join-Path $StateDir 'supervisor.log'
$SupervisorState = Join-Path $StateDir 'supervisor.json'
$PausedMarker = Join-Path $StateDir 'paused'
$ColdStartCeilingSeconds = 300
$ProbeEverySeconds = 20
$ProbeTimeoutSeconds = 15
$ProbeMissesToKill = 6
$BackoffSeconds = @(3, 10, 30, 60)
$LogRotateBytes = 5MB

# Command-line parsing and state-directory comparison live in one file, which the
# regression tests dot-source directly. The tests therefore exercise this code, not a copy.
$IdentityLib = Join-Path $ProjectRoot 'lib\JevmulatorIdentity.ps1'
if (-not (Test-Path -LiteralPath $IdentityLib)) {
    throw "Missing $IdentityLib. The identity check cannot run, so nothing will be stopped."
}
. $IdentityLib

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

function Write-Failure {
    <#
      Write one line to the error stream WITHOUT terminating.

      This script sets $ErrorActionPreference to Stop, which turns Write-Error into a
      terminating error. That aborted the calling function before its `return <code>`, so
      the documented exit codes 2 to 5 never reached the caller and every failure exited 1.
    #>
    param([string]$Message)
    $Host.UI.WriteErrorLine($Message)
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Write-StartTrace {
    param([string]$Step)
    $now = Get-Date
    $line = '{0} pid={1} +{2}ms {3}' -f $now.ToString('o'), $PID, [int]($now - $ScriptLaunch).TotalMilliseconds, $Step
    try {
        if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
        [System.IO.File]::AppendAllText($StartTrace, $line + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}

function Read-Utf8 {
    param([string]$Path)
    return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

function Get-RuntimeRecord {
    if (-not (Test-Path -LiteralPath $RuntimeFile)) { return $null }
    try {
        $text = Read-Utf8 -Path $RuntimeFile
        if ([string]::IsNullOrWhiteSpace($text)) { return $null }
        return $text | ConvertFrom-Json
    } catch {
        Write-Verbose "Runtime file is unreadable: $($_.Exception.Message)"
        return $null
    }
}

function Set-RuntimeRecord {
    param($Record)
    Write-Utf8NoBom -Path $RuntimeFile -Content ($Record | ConvertTo-Json -Depth 8)
}

function ConvertTo-ProcessArgument {
    <#
      Quote one argument for a native command line.

      Start-Process joins -ArgumentList with spaces and quotes nothing, so a path that
      contains a space arrives at the target process split into two arguments. Every
      argument therefore goes through this function first. A run of backslashes directly
      before the closing quote is doubled, because the Windows command line parser would
      otherwise read them as escapes.
    #>
    param([string]$Value)

    if ($Value -notmatch '[\s"]') { return $Value }
    $escaped = $Value -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Resolve-Python {
    if ($Python -ne '') { return $Python }
    if ($env:JEVMULATOR_PYTHON) { return $env:JEVMULATOR_PYTHON }
    $venv = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw 'No python interpreter found. Pass -Python, or set JEVMULATOR_PYTHON.'
}

function Test-PortInUse {
    param([int]$TestPort)
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $TestPort)
        $listener.Start()
        return $false
    } catch {
        return $true
    } finally {
        if ($null -ne $listener) { try { $listener.Stop() } catch { } }
    }
}

$script:IdentityReason = ''

function Get-DaemonProcess {
    <#
      Return the process ONLY when three proofs all hold, and $null otherwise.

      1. The process id exists.
      2. The recorded creation time is present, parses, and matches the live process
         within two seconds.
      3. The command line is readable, invokes -m jevmulator serve as three adjacent
         arguments, and passes a --state-dir argument whose normalized full path equals
         THIS checkout state directory exactly.

      This function fails CLOSED. If any proof cannot be obtained, for example because
      the creation time was never recorded or the command line cannot be read, it returns
      $null and never hands a process to Stop-Process. An earlier version accepted a
      missing or unparsable creation time, accepted an unreadable command line, and
      accepted any command line containing the word jevmulator. A later version compared
      the state directory with IndexOf, which accepted a prefix collision: a daemon owning
      ...\.jevmulator-other satisfied a check for ...\.jevmulator. The comparison now
      parses the arguments and compares the actual --state-dir value as a full path.

      The refusal reason is left in $script:IdentityReason for the caller to report.
    #>
    param($Record)

    $script:IdentityReason = ''

    if ($null -eq $Record) {
        $script:IdentityReason = 'no runtime record'
        return $null
    }
    if (-not ($Record.PSObject.Properties.Name -contains 'pid')) {
        $script:IdentityReason = 'the runtime file records no process id'
        return $null
    }

    $process = Get-Process -Id $Record.pid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        $script:IdentityReason = "process $($Record.pid) no longer exists"
        return $null
    }

    # -- proof 2: creation time, required ---------------------------------
    if (-not ($Record.PSObject.Properties.Name -contains 'process_start_time')) {
        $script:IdentityReason = "process $($Record.pid) cannot be verified: the runtime file records no creation time"
        return $null
    }
    $recorded = $null
    try {
        $recorded = [datetime]::Parse($Record.process_start_time, [System.Globalization.CultureInfo]::InvariantCulture)
    } catch {
        $recorded = $null
    }
    if ($null -eq $recorded) {
        $script:IdentityReason = "process $($Record.pid) cannot be verified: the recorded creation time is not a date"
        return $null
    }
    $actual = $null
    try { $actual = $process.StartTime } catch { $actual = $null }
    if ($null -eq $actual) {
        $script:IdentityReason = "process $($Record.pid) cannot be verified: its creation time is not readable"
        return $null
    }
    $delta = [math]::Abs(($actual - $recorded).TotalSeconds)
    if ($delta -gt 2) {
        $script:IdentityReason = "process $($Record.pid) is a different process: its creation time differs by $([math]::Round($delta, 1)) seconds"
        return $null
    }

    # -- proof 3: invocation and state-directory ownership, required ------
    $commandLine = $null
    try {
        $commandLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $($Record.pid)" -ErrorAction Stop).CommandLine
    } catch {
        $commandLine = $null
    }
    $verdict = Test-DaemonCommandLine -CommandLine $commandLine -ExpectedStateDir $StateDir
    if (-not $verdict.Ok) {
        $script:IdentityReason = "process $($Record.pid) is not this checkout's daemon: $($verdict.Reason)"
        return $null
    }

    return $process
}

function Get-Health {
    param([string]$BaseUrl, [int]$TimeoutSeconds = 3)
    try {
        $response = Invoke-WebRequest -Uri "$BaseUrl/_jevmulator/health" -UseBasicParsing -TimeoutSec $TimeoutSeconds
        return ($response.Content | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Resolve-Port {
    param($Record)
    if ($Port -ne 0) { return $Port }
    if ($null -ne $Record -and ($Record.PSObject.Properties.Name -contains 'port')) { return [int]$Record.port }
    return $DefaultPort
}

function Remove-StaleRuntime {
    param($Record, [string]$Reason)
    if (Test-Path -LiteralPath $RuntimeFile) {
        Remove-Item -LiteralPath $RuntimeFile -Force
        Write-Host "jevmulator: removed a stale runtime file ($Reason)."
    }
}

function Write-PreviousErrLog {
    param([switch]$ToTrace)
    if (-not (Test-Path -LiteralPath $ErrLog)) { return }
    foreach ($previous in @(Get-Content -LiteralPath $ErrLog -Tail 20 -ErrorAction SilentlyContinue)) {
        if ($ToTrace) { Write-StartTrace "previous daemon.err.log: $previous" }
        else { Write-SupervisorLog "previous daemon.err.log: $previous" }
    }
}

$script:SpawnError = ''

function Start-DaemonChild {
    param([int]$TargetPort, [string]$Prefix)

    if (-not (Test-Path -LiteralPath $StateDir)) {
        New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    }
    foreach ($log in @($OutLog, $ErrLog)) {
        Write-Utf8NoBom -Path $log -Content ''
    }

    $pythonExe = Resolve-Python
    Write-StartTrace "$Prefix python $pythonExe"
    $rawArguments = @('-X', 'utf8', '-m', 'jevmulator', 'serve', '--port', "$TargetPort", '--state-dir', $StateDir)
    $arguments = ($rawArguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' '

    $env:PYTHONPATH = (Join-Path $ProjectRoot 'src') + $(if ($env:PYTHONPATH) { [System.IO.Path]::PathSeparator + $env:PYTHONPATH } else { '' })
    $env:PYTHONUTF8 = '1'

    try {
        $process = Start-Process -FilePath $pythonExe -ArgumentList $arguments `
            -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
        $null = $process.Handle
    } catch {
        $script:SpawnError = $_.Exception.Message
        Write-StartTrace "$Prefix Start-Process failed: $script:SpawnError"
        return $null
    }
    Write-StartTrace "$Prefix daemon process $($process.Id) launched"
    return $process
}

$script:ReadyHealth = $null

function Wait-DaemonReady {
    param($Process, [string]$BaseUrl, [int]$Seconds, [string]$Prefix)

    $script:ReadyHealth = $null
    $deadline = (Get-Date).AddSeconds($Seconds)
    $probe = 0
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) { return 'exited' }
        $probe++
        $probeStarted = Get-Date
        $health = Get-Health -BaseUrl $BaseUrl -TimeoutSeconds 2
        Write-StartTrace "$Prefix probe $probe $(if ($null -eq $health) { 'no answer' } else { 'answered' }) in $([int]((Get-Date) - $probeStarted).TotalMilliseconds) ms"
        if ($null -ne $health) {
            $script:ReadyHealth = $health
            return 'ready'
        }
        Start-Sleep -Milliseconds 250
    }
    Write-StartTrace "$Prefix not ready after $Seconds s, daemon exited: $($Process.HasExited)"
    return 'timeout'
}

function Add-RuntimeIdentity {
    param([string]$LaunchedBy)
    $record = Get-RuntimeRecord
    if ($null -eq $record) { return $null }
    $startTime = (Get-Process -Id $record.pid).StartTime.ToString('o', [System.Globalization.CultureInfo]::InvariantCulture)
    $record | Add-Member -NotePropertyName 'process_start_time' -NotePropertyValue $startTime -Force
    $record | Add-Member -NotePropertyName 'launched_by' -NotePropertyValue $LaunchedBy -Force
    Set-RuntimeRecord -Record $record
    return $record
}

function Write-SupervisorLog {
    param([string]$Message)
    $line = '{0} pid={1} {2}' -f (Get-Date).ToString('o'), $PID, $Message
    try {
        if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
        [System.IO.File]::AppendAllText($SupervisorLog, $line + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}

function Step-SupervisorLog {
    try {
        if ((Test-Path -LiteralPath $SupervisorLog) -and (Get-Item -LiteralPath $SupervisorLog).Length -gt $LogRotateBytes) {
            Move-Item -LiteralPath $SupervisorLog -Destination "$SupervisorLog.1" -Force
        }
    } catch { }
}

function Set-SupervisorState {
    param([string]$Phase, [string]$Verdict, $ChildPid, [int]$Restarts)
    $state = [ordered]@{
        phase          = $Phase
        verdict        = $Verdict
        supervisor_pid = $PID
        child_pid      = $ChildPid
        restarts       = $Restarts
        updated        = (Get-Date).ToString('o')
    }
    try { Write-Utf8NoBom -Path $SupervisorState -Content ($state | ConvertTo-Json) } catch { }
}

function Get-SupervisorProcess {
    if (-not (Test-Path -LiteralPath $SupervisorPidFile)) { return $null }
    $text = ''
    try { $text = "$(Get-Content -LiteralPath $SupervisorPidFile -TotalCount 1 -ErrorAction Stop)".Trim() } catch { return $null }
    $supervisorPid = 0
    if (-not [int]::TryParse($text, [ref]$supervisorPid)) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $supervisorPid" -ErrorAction SilentlyContinue
    if ($null -eq $process -or [string]::IsNullOrEmpty($process.CommandLine)) { return $null }
    if ($process.CommandLine.IndexOf($ScriptPath, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { return $null }
    if ($process.CommandLine -notmatch '\bsupervise\b') { return $null }
    return $process
}

function Stop-DaemonTree {
    param([int]$Id)
    & taskkill.exe /T /F /PID $Id 2>&1 | Out-Null
}

function Request-DaemonShutdown {
    param($Record)
    try {
        Invoke-WebRequest -Uri "$($Record.base_url)/_jevmulator/shutdown" -Method Post -UseBasicParsing `
            -Headers @{ Authorization = "Bearer $($Record.api_key)" } `
            -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        Write-Host "jevmulator: the shutdown route did not accept the request: $($_.Exception.Message)"
        return $false
    }
}

function Stop-StrayDaemons {
    $killed = 0
    foreach ($candidate in @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue)) {
        $verdict = Test-DaemonCommandLine -CommandLine $candidate.CommandLine -ExpectedStateDir $StateDir
        if ($verdict.Ok) {
            Write-SupervisorLog "killing stray daemon process $($candidate.ProcessId)"
            Stop-DaemonTree -Id $candidate.ProcessId
            $killed++
        }
    }
    if ($killed -gt 0) { Start-Sleep -Seconds 1 }
}

function Test-TaskMode {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { return $false }
    foreach ($action in @($task.Actions)) {
        $line = "$($action.Execute) $($action.Arguments)"
        if ($line.IndexOf($ScriptPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and $line -match '\bsupervise\b') { return $true }
    }
    return $false
}

function Get-Verdict {
    if (Test-Path -LiteralPath $PausedMarker) { return 'paused: jevmulator is paused; run start to resume it' }
    if ($null -eq (Get-SupervisorProcess)) { return "down: no supervisor is running; the $TaskName task starts one within 5 minutes, or run start" }
    try {
        $state = Read-Utf8 -Path $SupervisorState | ConvertFrom-Json
        return "$($state.phase): $($state.verdict)"
    } catch {
        return 'starting: the supervisor is running and has not reported yet'
    }
}

function Watch-Daemon {
    param($Process, [string]$BaseUrl, [int]$Restarts)
    $misses = 0
    $waitMs = $ProbeEverySeconds * 1000
    while (-not $Process.WaitForExit($waitMs)) {
        $probeStarted = Get-Date
        $health = Get-Health -BaseUrl $BaseUrl -TimeoutSeconds $ProbeTimeoutSeconds
        $waitMs = [int][Math]::Max(1000, $ProbeEverySeconds * 1000 - ((Get-Date) - $probeStarted).TotalMilliseconds)
        if ($Process.HasExited) { return }
        if ($null -ne $health) {
            if ($misses -gt 0) {
                Write-SupervisorLog "probe answered again after $misses miss(es)"
                Set-SupervisorState -Phase 'ok' -Verdict "jevmulator answers on $BaseUrl as process $($Process.Id)" -ChildPid $Process.Id -Restarts $Restarts
            }
            $misses = 0
            continue
        }
        $misses++
        Write-SupervisorLog "probe missed ($misses/$ProbeMissesToKill)"
        Set-SupervisorState -Phase 'slow' -Verdict "jevmulator did not answer the last probe within $ProbeTimeoutSeconds s ($misses/$ProbeMissesToKill); usually a busy moment, wait" -ChildPid $Process.Id -Restarts $Restarts
        if ($misses -ge $ProbeMissesToKill) {
            Write-SupervisorLog "no answer for $misses probes in a row; killing daemon process $($Process.Id)"
            Stop-DaemonTree -Id $Process.Id
            return
        }
    }
}

function Wait-Backoff {
    param([int]$Seconds, [string]$Verdict, [int]$Restarts)
    Set-SupervisorState -Phase 'respawning' -Verdict $Verdict -ChildPid $null -Restarts $Restarts
    Start-Sleep -Seconds $Seconds
}

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

function Invoke-Supervise {
    if (-not (Test-Path -LiteralPath $StateDir)) {
        New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    }
    $lock = $null
    try {
        $lock = [System.IO.File]::Open($SupervisorPidFile, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::Read)
    } catch [System.IO.IOException] {
        Write-SupervisorLog "another supervisor holds $SupervisorPidFile; not starting a second one"
        return 3
    }
    $lock.SetLength(0)
    $bytes = [System.Text.Encoding]::ASCII.GetBytes("$PID`r`n")
    $lock.Write($bytes, 0, $bytes.Length)
    $lock.Flush()

    $targetPort = Resolve-Port -Record $null
    $baseUrl = "http://127.0.0.1:$targetPort"
    Write-SupervisorLog "supervisor started for port $targetPort"
    $restart = 0
    $step = 0
    $recent = New-Object System.Collections.ArrayList

    try {
        while ($true) {
            if (Test-Path -LiteralPath $PausedMarker) {
                Write-SupervisorLog 'paused marker present; supervisor exiting'
                break
            }
            $first = ($restart -eq 0)
            $prefix = if ($first) { 'start:' } else { "restart ${restart}:" }
            Step-SupervisorLog

            if ($first) {
                Write-StartTrace "start: entered, supervisor launched $($ScriptLaunch.ToString('o'))"
                $record = Get-RuntimeRecord
                Write-StartTrace "start: runtime record $(if ($null -eq $record) { 'absent' } else { "present, pid $($record.pid)" })"
                $existing = Get-DaemonProcess -Record $record
                Write-StartTrace "start: identity check done $(if ($null -eq $existing) { "($script:IdentityReason)" } else { '(daemon alive, replaced by the supervisor)' })"
            }
            Stop-StrayDaemons

            if (Test-PortInUse -TestPort $targetPort) {
                Write-SupervisorLog "port $targetPort is in use by another program"
                if ($first) {
                    Write-StartTrace "start: port $targetPort in use"
                    Write-StartTrace 'start: exit 2'
                }
                $delay = $BackoffSeconds[[Math]::Min($step, $BackoffSeconds.Count - 1)]
                $step++
                $restart++
                Wait-Backoff -Seconds $delay -Verdict "port $targetPort is held by another program; the supervisor tries again in $delay s" -Restarts $restart
                continue
            }
            if ($first) {
                Write-StartTrace "start: port $targetPort free"
                Write-PreviousErrLog -ToTrace
            } else {
                Write-PreviousErrLog
            }

            Set-SupervisorState -Phase 'starting' -Verdict 'the supervisor started jevmulator and waits for its first answer' -ChildPid $null -Restarts $restart
            $spawnedAt = Get-Date
            $child = Start-DaemonChild -TargetPort $targetPort -Prefix $prefix
            if ($null -eq $child) {
                Write-SupervisorLog "could not start python: $script:SpawnError"
                Write-StartTrace "$prefix exit 3"
                $delay = $BackoffSeconds[[Math]::Min($step, $BackoffSeconds.Count - 1)]
                $step++
                $restart++
                Wait-Backoff -Seconds $delay -Verdict "python did not start; the supervisor tries again in $delay s" -Restarts $restart
                continue
            }
            Write-SupervisorLog "spawned daemon process $($child.Id)"
            Set-SupervisorState -Phase 'starting' -Verdict "jevmulator process $($child.Id) is starting" -ChildPid $child.Id -Restarts $restart

            $outcome = Wait-DaemonReady -Process $child -BaseUrl $baseUrl -Seconds $ColdStartCeilingSeconds -Prefix $prefix
            if ($outcome -eq 'ready') {
                $null = Add-RuntimeIdentity -LaunchedBy 'supervisor'
                Write-StartTrace "$prefix exit 0"
                Write-SupervisorLog "daemon process $($child.Id) answered after $([int]((Get-Date) - $spawnedAt).TotalSeconds) s"
                $step = 0
                Set-SupervisorState -Phase 'ok' -Verdict "jevmulator answers on $baseUrl as process $($child.Id)" -ChildPid $child.Id -Restarts $restart
                Watch-Daemon -Process $child -BaseUrl $baseUrl -Restarts $restart
            } elseif ($outcome -eq 'timeout') {
                Write-SupervisorLog "daemon process $($child.Id) did not answer within $ColdStartCeilingSeconds s; killing it"
                Stop-DaemonTree -Id $child.Id
                Write-StartTrace "$prefix exit 4"
            } else {
                Write-StartTrace "$prefix exit 3"
            }

            $child.WaitForExit()
            $code = 'unknown'
            try { $code = $child.ExitCode } catch { }
            Write-SupervisorLog "daemon process $($child.Id) exited with code $code after $([int]((Get-Date) - $spawnedAt).TotalSeconds) s"
            $restart++

            [void]$recent.Add((Get-Date))
            while ($recent.Count -gt 0 -and ((Get-Date) - $recent[0]).TotalSeconds -gt 300) { $recent.RemoveAt(0) }
            if ($recent.Count -ge 10) {
                Write-SupervisorLog 'ten exits within five minutes; pausing 60 s before the next start'
                $delay = 60
            } else {
                $delay = $BackoffSeconds[[Math]::Min($step, $BackoffSeconds.Count - 1)]
                $step++
            }
            Wait-Backoff -Seconds $delay -Verdict "jevmulator exited with code $code; the supervisor starts it again in $delay s" -Restarts $restart
        }
    } finally {
        $lock.Close()
        Remove-Item -LiteralPath $SupervisorPidFile -Force -ErrorAction SilentlyContinue
    }
    return 0
}

function Invoke-Install {
    $shimPath = if ($Shim -ne '') { $Shim } else { Join-Path $env:USERPROFILE 'rig\agent-jobs\runner\run-hidden.vbs' }
    if (-not (Test-Path -LiteralPath $shimPath)) {
        Write-Failure "jevmulator: the waiting shim $shimPath does not exist. Pass -Shim."
        return 2
    }
    if (-not (Test-Path -LiteralPath $StateDir)) {
        New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    }
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        $backup = Join-Path $StateDir ("task-backup-{0}.xml" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
        Write-Utf8NoBom -Path $backup -Content (Export-ScheduledTask -TaskName $TaskName)
        Write-Host "jevmulator: saved the old task definition to $backup"
    }

    $argument = '//B //Nologo "{0}" powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{1}" supervise' -f $shimPath, $ScriptPath
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $argument -WorkingDirectory $ProjectRoot
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"),
        (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5))
    )
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "jevmulator: registered task $TaskName. It runs the supervisor at logon and every 5 minutes."
    Write-Host 'jevmulator: run  .\jevmulator.ps1 start  to start it now.'
    return 0
}

function Wait-TaskReady {
    param([int]$NotPid = 0)
    $baseUrl = "http://127.0.0.1:$(Resolve-Port -Record $null)"
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $record = Get-RuntimeRecord
        $state = $null
        try { $state = Read-Utf8 -Path $SupervisorState | ConvertFrom-Json } catch { }
        $supervised = $null -ne $state -and $state.phase -eq 'ok' -and $null -ne $state.child_pid
        if ($supervised -and $null -ne $record -and [int]$record.pid -eq [int]$state.child_pid -and [int]$record.pid -ne $NotPid) {
            $health = Get-Health -BaseUrl $baseUrl -TimeoutSeconds 2
            if ($null -ne $health) {
                Write-Host "jevmulator: ready on $baseUrl as process $($record.pid), upstream model $($health.upstream_model)."
                if (-not $health.ready) { return 6 }
                return 0
            }
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Failure "jevmulator: not ready within $ReadyTimeoutSeconds seconds. $(Get-Verdict)"
    return 4
}

function Invoke-TaskStart {
    if (Test-Path -LiteralPath $PausedMarker) {
        Remove-Item -LiteralPath $PausedMarker -Force
        Write-Host 'jevmulator: removed the paused marker.'
    }
    $supervisor = Get-SupervisorProcess
    if ($null -eq $supervisor) {
        Write-Host "jevmulator: starting task $TaskName ..."
        Start-ScheduledTask -TaskName $TaskName
    } else {
        Write-Host "jevmulator: supervisor $($supervisor.ProcessId) is running."
    }
    return (Wait-TaskReady)
}

function Invoke-TaskStop {
    Write-Utf8NoBom -Path $PausedMarker -Content "paused by jevmulator.ps1 stop at $((Get-Date).ToString('o'))"
    $supervisor = Get-SupervisorProcess
    if ($null -ne $supervisor) {
        Stop-Process -Id $supervisor.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "jevmulator: stopped supervisor $($supervisor.ProcessId)."
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $code = Invoke-Stop
    Stop-StrayDaemons
    Set-SupervisorState -Phase 'paused' -Verdict 'jevmulator is paused; run start to resume it' -ChildPid $null -Restarts 0
    Write-Host 'jevmulator: paused. The task will not start it again until you run start.'
    return $code
}

function Invoke-TaskRestart {
    if ((Test-Path -LiteralPath $PausedMarker) -or $null -eq (Get-SupervisorProcess)) {
        return (Invoke-TaskStart)
    }
    $record = Get-RuntimeRecord
    $process = Get-DaemonProcess -Record $record
    if ($null -eq $process) {
        Write-Host "jevmulator: no daemon to restart ($script:IdentityReason). The supervisor is starting one."
        return (Wait-TaskReady)
    }
    Write-Host "jevmulator: stopping process $($record.pid); the supervisor starts a new one."
    if (Request-DaemonShutdown -Record $record) { $null = $process.WaitForExit(10000) }
    if (-not $process.HasExited) { Stop-DaemonTree -Id $record.pid }
    return (Wait-TaskReady -NotPid ([int]$record.pid))
}

function Invoke-Start {
    Write-StartTrace "start: entered, powershell launched $($ScriptLaunch.ToString('o'))"
    $record = Get-RuntimeRecord
    Write-StartTrace "start: runtime record $(if ($null -eq $record) { 'absent' } else { "present, pid $($record.pid)" })"
    $existing = Get-DaemonProcess -Record $record
    Write-StartTrace "start: identity check done $(if ($null -eq $existing) { "($script:IdentityReason)" } else { '(daemon alive)' })"

    if ($null -ne $existing) {
        $health = Get-Health -BaseUrl $record.base_url
        Write-StartTrace "start: health of the running daemon $(if ($null -eq $health) { 'none' } else { 'ok' })"
        if ($null -ne $health) {
            Write-Host "jevmulator: already running on $($record.base_url) as process $($record.pid)."
            return 0
        }
        Write-Host "jevmulator: process $($record.pid) is alive but its health route does not answer. Stop it first."
        return 1
    }

    if ($null -ne $record) {
        Remove-StaleRuntime -Record $record -Reason 'the recorded process is gone'
    }

    $targetPort = Resolve-Port -Record $null
    $portInUse = Test-PortInUse -TestPort $targetPort
    Write-StartTrace "start: port $targetPort $(if ($portInUse) { 'in use' } else { 'free' })"
    if ($portInUse) {
        Write-Failure "jevmulator: port $targetPort is already in use. Choose another with -Port."
        return 2
    }

    Write-PreviousErrLog -ToTrace

    Write-Host "jevmulator: starting on port $targetPort ..."
    $process = Start-DaemonChild -TargetPort $targetPort -Prefix 'start:'
    if ($null -eq $process) {
        Write-Failure "jevmulator: cannot start '$(Resolve-Python)': $script:SpawnError"
        return 3
    }

    $baseUrl = "http://127.0.0.1:$targetPort"
    $outcome = Wait-DaemonReady -Process $process -BaseUrl $baseUrl -Seconds $ReadyTimeoutSeconds -Prefix 'start:'
    if ($outcome -eq 'exited') {
        $stderr = ''
        if (Test-Path -LiteralPath $ErrLog) { $stderr = (Read-Utf8 -Path $ErrLog).Trim() }
        $exitCode = 'unknown'
        try { $exitCode = $process.ExitCode } catch { }
        Write-Failure "jevmulator: the daemon exited with code $exitCode before it was ready.`n$stderr"
        return 3
    }
    if ($outcome -eq 'timeout') {
        Write-Failure "jevmulator: the daemon did not become ready within $ReadyTimeoutSeconds seconds. See $ErrLog."
        try { $process.Kill() } catch { }
        return 4
    }
    $health = $script:ReadyHealth

    $record = Add-RuntimeIdentity -LaunchedBy 'jevmulator.ps1'
    if ($null -eq $record) {
        Write-Failure "jevmulator: the daemon answered but wrote no runtime file at $RuntimeFile."
        return 5
    }

    $suffix = ''
    if (-not $health.ready) { $suffix = "  status: $($health.status) -- $($health.problems -join '; ')" }
    Write-Host "jevmulator: ready on $baseUrl as process $($record.pid), upstream model $($health.upstream_model).$suffix"
    Write-Host "jevmulator: read the API key with  .\jevmulator.ps1 status -ShowKey"
    if (-not $health.ready) { return 6 }
    return 0
}

function Invoke-Status {
    if (Test-TaskMode) { Write-Host "verdict           : $(Get-Verdict)" }
    $record = Get-RuntimeRecord
    if ($null -eq $record) {
        Write-Host 'jevmulator: not running. No runtime file.'
        return 1
    }

    $process = Get-DaemonProcess -Record $record
    if ($null -eq $process) {
        Write-Host "jevmulator: not running. Identity check refused: $script:IdentityReason."
        return 1
    }

    $health = Get-Health -BaseUrl $record.base_url
    $ready = $false
    if ($null -ne $health) { $ready = [bool]$health.ready }

    Write-Host "endpoint          : $($record.base_url)"
    Write-Host "evaluate          : $($record.base_url)/v1/systemone"
    Write-Host "models            : $($record.base_url)/v1/models"
    Write-Host "process           : $($record.pid)  started $($process.StartTime.ToString('o'))"
    Write-Host "responding        : $([bool]($null -ne $health))"
    Write-Host "ready             : $ready"
    if ($null -ne $health) {
        Write-Host "status            : $($health.status)"
        Write-Host "provider          : $($health.provider)"
        Write-Host "upstream model    : $($health.upstream_model)"
        Write-Host "reported model    : $($health.resolved_model_name)"
        if ($health.problems.Count -gt 0) {
            Write-Host "problems          : $($health.problems -join '; ')"
        }
    }
    Write-Host "runtime file      : $RuntimeFile"
    Write-Host "logs              : $OutLog"
    Write-Host "                    $ErrLog"

    if ($ShowKey) {
        Write-Host "api key           : $($record.api_key)"
    } else {
        Write-Host "api key           : hidden. Add -ShowKey to print it."
    }

    if (-not $ready) { return 6 }
    return 0
}

function Invoke-Stop {
    $record = Get-RuntimeRecord
    if ($null -eq $record) {
        Write-Host 'jevmulator: not running. No runtime file.'
        return 0
    }

    $process = Get-DaemonProcess -Record $record
    if ($null -eq $process) {
        # Nothing is terminated. The runtime file is removed because it no longer names a
        # process this checkout may act on.
        Remove-StaleRuntime -Record $record -Reason $script:IdentityReason
        Write-Host "jevmulator: nothing terminated. Identity check refused: $script:IdentityReason."
        return 0
    }

    Write-Host "jevmulator: stopping process $($record.pid) ..."
    $graceSeconds = 0
    if (Request-DaemonShutdown -Record $record) { $graceSeconds = 10 }

    $deadline = (Get-Date).AddSeconds($graceSeconds)
    while ((Get-Date) -lt $deadline -and -not $process.HasExited) {
        Start-Sleep -Milliseconds 200
        $process.Refresh()
    }

    if (-not $process.HasExited) {
        # Re-verify identity immediately before the forced stop.
        $stillOurs = Get-DaemonProcess -Record $record
        if ($null -eq $stillOurs) {
            Write-Host "jevmulator: nothing terminated. Identity check refused just before the forced stop: $script:IdentityReason."
            Remove-StaleRuntime -Record $record -Reason 'identity check refused before terminating'
            return 0
        }
        Stop-Process -Id $record.pid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 300
    }

    if (Test-Path -LiteralPath $RuntimeFile) {
        Remove-Item -LiteralPath $RuntimeFile -Force
    }
    Write-Host 'jevmulator: stopped.'
    return 0
}

if ($Command -eq 'supervise') { exit (Invoke-Supervise) }
if ($Command -eq 'install') { exit (Invoke-Install) }
if ($Command -ne 'status' -and (Test-TaskMode)) {
    switch ($Command) {
        'start'   { exit (Invoke-TaskStart) }
        'stop'    { exit (Invoke-TaskStop) }
        'restart' { exit (Invoke-TaskRestart) }
    }
}

switch ($Command) {
    'start'   {
        $code = Invoke-Start
        Write-StartTrace "start: exit $code"
        exit $code
    }
    'status'  { exit (Invoke-Status) }
    'stop'    { exit (Invoke-Stop) }
    'restart' {
        $code = Invoke-Stop
        if ($code -ne 0) { exit $code }
        $code = Invoke-Start
        Write-StartTrace "start: exit $code"
        exit $code
    }
}
