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
    start, status, stop or restart.

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
    [ValidateSet('start', 'status', 'stop', 'restart')]
    [string]$Command,

    [ValidateRange(1, 65535)]
    [int]$Port = 0,

    [switch]$ShowKey,

    [int]$ReadyTimeoutSeconds = 45,

    [string]$Python = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$DefaultPort = 8769
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateDir = Join-Path $ProjectRoot '.jevmulator'
$RuntimeFile = Join-Path $StateDir 'runtime.json'
$OutLog = Join-Path $StateDir 'daemon.out.log'
$ErrLog = Join-Path $StateDir 'daemon.err.log'

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

function Get-DaemonProcess {
    <#
      Returns the process only when it is really this daemon: the process id must exist,
      its recorded start time must match, and its command line must name jevmulator.
      Anything else returns $null, so an unrelated process that reused the id is safe.
    #>
    param($Record)

    if ($null -eq $Record) { return $null }
    if (-not ($Record.PSObject.Properties.Name -contains 'pid')) { return $null }

    $process = Get-Process -Id $Record.pid -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }

    if ($Record.PSObject.Properties.Name -contains 'process_start_time') {
        $recorded = $null
        try { $recorded = [datetime]::Parse($Record.process_start_time, [System.Globalization.CultureInfo]::InvariantCulture) } catch { $recorded = $null }
        if ($null -ne $recorded) {
            $delta = [math]::Abs(($process.StartTime - $recorded).TotalSeconds)
            if ($delta -gt 2) {
                Write-Verbose "Process $($Record.pid) start time differs by $delta seconds; treating as an unrelated process."
                return $null
            }
        }
    }

    $commandLine = $null
    try {
        $commandLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $($Record.pid)" -ErrorAction SilentlyContinue).CommandLine
    } catch {
        $commandLine = $null
    }
    if ($null -ne $commandLine -and $commandLine -notmatch 'jevmulator') {
        Write-Verbose "Process $($Record.pid) command line does not name jevmulator; treating as an unrelated process."
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

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

function Invoke-Start {
    $record = Get-RuntimeRecord
    $existing = Get-DaemonProcess -Record $record

    if ($null -ne $existing) {
        $health = Get-Health -BaseUrl $record.base_url
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
    if (Test-PortInUse -TestPort $targetPort) {
        Write-Failure "jevmulator: port $targetPort is already in use. Choose another with -Port."
        return 2
    }

    if (-not (Test-Path -LiteralPath $StateDir)) {
        New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    }
    foreach ($log in @($OutLog, $ErrLog)) {
        Write-Utf8NoBom -Path $log -Content ''
    }

    $pythonExe = Resolve-Python
    $rawArguments = @('-X', 'utf8', '-m', 'jevmulator', 'serve', '--port', "$targetPort", '--state-dir', $StateDir)
    $arguments = ($rawArguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' '

    $env:PYTHONPATH = (Join-Path $ProjectRoot 'src') + $(if ($env:PYTHONPATH) { [System.IO.Path]::PathSeparator + $env:PYTHONPATH } else { '' })
    $env:PYTHONUTF8 = '1'

    Write-Host "jevmulator: starting on port $targetPort ..."
    try {
        $process = Start-Process -FilePath $pythonExe -ArgumentList $arguments `
            -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
    } catch {
        Write-Failure "jevmulator: cannot start '$pythonExe': $($_.Exception.Message)"
        return 3
    }

    $baseUrl = "http://127.0.0.1:$targetPort"
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    $health = $null
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            $stderr = ''
            if (Test-Path -LiteralPath $ErrLog) { $stderr = (Read-Utf8 -Path $ErrLog).Trim() }
            $exitCode = 'unknown'
            try { $exitCode = $process.ExitCode } catch { }
            Write-Failure "jevmulator: the daemon exited with code $exitCode before it was ready.`n$stderr"
            return 3
        }
        $health = Get-Health -BaseUrl $baseUrl -TimeoutSeconds 2
        if ($null -ne $health) { break }
        Start-Sleep -Milliseconds 250
    }

    if ($null -eq $health) {
        Write-Failure "jevmulator: the daemon did not become ready within $ReadyTimeoutSeconds seconds. See $ErrLog."
        try { $process.Kill() } catch { }
        return 4
    }

    $record = Get-RuntimeRecord
    if ($null -eq $record) {
        Write-Failure "jevmulator: the daemon answered but wrote no runtime file at $RuntimeFile."
        return 5
    }

    $startTime = (Get-Process -Id $record.pid).StartTime.ToString('o', [System.Globalization.CultureInfo]::InvariantCulture)
    $record | Add-Member -NotePropertyName 'process_start_time' -NotePropertyValue $startTime -Force
    $record | Add-Member -NotePropertyName 'launched_by' -NotePropertyValue 'jevmulator.ps1' -Force
    Set-RuntimeRecord -Record $record

    $suffix = ''
    if (-not $health.ready) { $suffix = "  status: $($health.status) -- $($health.problems -join '; ')" }
    Write-Host "jevmulator: ready on $baseUrl as process $($record.pid), upstream model $($health.upstream_model).$suffix"
    Write-Host "jevmulator: read the API key with  .\jevmulator.ps1 status -ShowKey"
    if (-not $health.ready) { return 6 }
    return 0
}

function Invoke-Status {
    $record = Get-RuntimeRecord
    if ($null -eq $record) {
        Write-Host 'jevmulator: not running. No runtime file.'
        return 1
    }

    $process = Get-DaemonProcess -Record $record
    if ($null -eq $process) {
        Write-Host "jevmulator: not running. The runtime file names process $($record.pid), which is gone or belongs to something else."
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
        Remove-StaleRuntime -Record $record -Reason "process $($record.pid) is gone or belongs to something else"
        Write-Host 'jevmulator: not running.'
        return 0
    }

    Write-Host "jevmulator: stopping process $($record.pid) ..."
    try {
        $process.CloseMainWindow() | Out-Null
    } catch { }

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and -not $process.HasExited) {
        Start-Sleep -Milliseconds 200
        $process.Refresh()
    }

    if (-not $process.HasExited) {
        # Re-verify identity immediately before the forced stop.
        $stillOurs = Get-DaemonProcess -Record $record
        if ($null -eq $stillOurs) {
            Write-Host 'jevmulator: the process is no longer ours. Nothing was terminated.'
            Remove-StaleRuntime -Record $record -Reason 'identity check failed before terminating'
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

switch ($Command) {
    'start'   { exit (Invoke-Start) }
    'status'  { exit (Invoke-Status) }
    'stop'    { exit (Invoke-Stop) }
    'restart' {
        $code = Invoke-Stop
        if ($code -ne 0) { exit $code }
        exit (Invoke-Start)
    }
}
