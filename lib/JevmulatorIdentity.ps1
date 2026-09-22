<#
.SYNOPSIS
    Pure helpers that decide whether a command line belongs to this checkout's daemon.

.DESCRIPTION
    These functions touch no process and no file, so they can be tested directly. The
    scriptlet dot-sources this file, and the regression tests dot-source the same file, so
    the tests exercise the code that actually runs rather than a copy of it.

    The earlier check used $commandLine.IndexOf($StateDir), which accepted a prefix
    collision: a daemon owning C:\probe\.jevmulator-other satisfied a check for
    C:\probe\.jevmulator. It also matched the state directory appearing anywhere in the
    command line, including inside an unrelated argument. Both are fixed here by parsing
    the command line into arguments and comparing the actual --state-dir value as a
    normalized full path.
#>

function ConvertFrom-ProcessCommandLine {
    <#
      Split a Windows command line into arguments, following the CommandLineToArgvW rules.

      A run of 2n backslashes before a quote yields n backslashes and toggles quoting.
      A run of 2n+1 backslashes before a quote yields n backslashes and a literal quote.
      Backslashes not followed by a quote are literal. Whitespace outside quotes separates
      arguments.
    #>
    param([string]$CommandLine)

    $tokens = New-Object System.Collections.Generic.List[string]
    if ([string]::IsNullOrEmpty($CommandLine)) { return $tokens.ToArray() }

    $current = New-Object System.Text.StringBuilder
    $inQuotes = $false
    $started = $false
    $backslashes = 0

    foreach ($character in $CommandLine.ToCharArray()) {
        if ($character -eq [char]'\') {
            $backslashes++
            continue
        }

        if ($character -eq [char]'"') {
            if ($backslashes -ge 2) {
                $null = $current.Append([string]([char]'\') * [math]::Floor($backslashes / 2))
            }
            if ($backslashes % 2 -eq 1) {
                $null = $current.Append([char]'"')
            } else {
                $inQuotes = -not $inQuotes
            }
            $backslashes = 0
            $started = $true
            continue
        }

        if ($backslashes -gt 0) {
            $null = $current.Append([string]([char]'\') * $backslashes)
            $backslashes = 0
            $started = $true
        }

        if ((-not $inQuotes) -and ($character -eq [char]' ' -or $character -eq [char]"`t")) {
            if ($started) {
                $tokens.Add($current.ToString())
                $null = $current.Clear()
                $started = $false
            }
            continue
        }

        $null = $current.Append($character)
        $started = $true
    }

    if ($backslashes -gt 0) {
        $null = $current.Append([string]([char]'\') * $backslashes)
        $started = $true
    }
    if ($started) { $tokens.Add($current.ToString()) }

    return $tokens.ToArray()
}

function Get-ArgumentValue {
    <#
      The value of a named argument, taken from parsed arguments.

      Accepts both the separated form, --state-dir C:\path, and the joined form,
      --state-dir=C:\path. Returns $null when the argument is absent or has no value.
    #>
    param(
        [string[]]$Arguments,
        [string]$Name
    )

    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        $token = $Arguments[$index]
        if ($token -ceq $Name) {
            if ($index + 1 -lt $Arguments.Count) { return $Arguments[$index + 1] }
            return $null
        }
        if ($token.StartsWith($Name + '=', [System.StringComparison]::Ordinal)) {
            return $token.Substring($Name.Length + 1)
        }
    }
    return $null
}

function Get-NormalizedDirectory {
    <#
      A comparable absolute path, or $null when the value is not a usable path.

      Returning $null is a refusal, so a caller that fails closed stays closed.
    #>
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    try {
        $full = [System.IO.Path]::GetFullPath($Path)
    } catch {
        return $null
    }
    return $full.TrimEnd([char]'\', [char]'/')
}

function Test-DaemonCommandLine {
    <#
      Decide whether a command line is this checkout's daemon.

      Returns an object with Ok and Reason. Ok is $true only when the arguments contain
      -m jevmulator serve as three separate adjacent arguments, and a --state-dir argument
      whose normalized full path equals the expected one exactly. Every other outcome is a
      refusal carrying the reason.
    #>
    param(
        [string]$CommandLine,
        [string]$ExpectedStateDir
    )

    $verdict = [pscustomobject]@{ Ok = $false; Reason = ''; StateDir = $null }

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        $verdict.Reason = 'its command line is not readable'
        return $verdict
    }

    $arguments = ConvertFrom-ProcessCommandLine -CommandLine $CommandLine

    # Anchor the module invocation at argument boundaries, so a path or a document that
    # merely contains the text "-m jevmulator serve" does not satisfy it.
    $invokes = $false
    for ($index = 0; $index -lt $arguments.Count - 2; $index++) {
        if ($arguments[$index] -ceq '-m' -and
            $arguments[$index + 1] -ceq 'jevmulator' -and
            $arguments[$index + 2] -ceq 'serve') {
            $invokes = $true
            break
        }
    }
    if (-not $invokes) {
        $verdict.Reason = 'its command line does not invoke -m jevmulator serve'
        return $verdict
    }

    $rawStateDir = Get-ArgumentValue -Arguments $arguments -Name '--state-dir'
    if ($null -eq $rawStateDir) {
        $verdict.Reason = 'its command line passes no --state-dir argument'
        return $verdict
    }

    $actual = Get-NormalizedDirectory -Path $rawStateDir
    $expected = Get-NormalizedDirectory -Path $ExpectedStateDir
    if ($null -eq $actual -or $null -eq $expected) {
        $verdict.Reason = 'its --state-dir argument is not a usable path'
        return $verdict
    }

    $verdict.StateDir = $actual
    if (-not [string]::Equals($actual, $expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        $verdict.Reason = "it owns $actual, not $expected"
        return $verdict
    }

    $verdict.Ok = $true
    return $verdict
}
