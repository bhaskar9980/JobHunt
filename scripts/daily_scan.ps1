# Daily discovery run for Windows Task Scheduler.
# Python steps run first (no tokens), then one headless Claude run for Gmail alerts + LLM scoring.
# Register once (adjust the time):
#   schtasks /Create /F /TN "JobHunt\DailyScan" /SC DAILY /ST 08:30 /RL LIMITED /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"C:\path\to\JobHunt\scripts\daily_scan.ps1\""
# Run on demand:  schtasks /Run /TN "JobHunt\DailyScan"
# Exit code is 1 when any step failed; data/last_scan.json records every step for `jobhunt plan`.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$claude = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root "data") | Out-Null
$log = Join-Path $root ("logs\scan_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$statusFile = Join-Path $root "data\last_scan.json"
$started = Get-Date -Format o
$steps = [ordered]@{}

function Run-Step($name, $block) {
    $exitSuccess = 0
    $exitFailure = 1
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = $exitSuccess
    $code = $exitSuccess
    "=== $(Get-Date -Format o) $name ===" | Out-File -FilePath $log -Append -Encoding utf8
    try {
        & $block 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord] -and
                $_.FullyQualifiedErrorId -notin @("NativeCommandError", "NativeCommandErrorMessage")) {
                $code = $exitFailure
            }
            $_
        } | Out-File -FilePath $log -Append -Encoding utf8 -ErrorAction Stop
        if ($LASTEXITCODE -ne $exitSuccess) { $code = $LASTEXITCODE }
    } catch {
        $code = $exitFailure
        $_ | Out-File -FilePath $log -Append -Encoding utf8
    }
    $steps[$name] = $code
    "--- $name exit $code" | Out-File -FilePath $log -Append -Encoding utf8
}

Run-Step "scan"      { uv run jobhunt scan }
Run-Step "prefilter" { uv run jobhunt prefilter }

if (Test-Path $claude) {
    Run-Step "claude_scan" {
        & $claude -p "/jobs-scan --skip-scan" `
            --permission-mode acceptEdits `
            --allowedTools "Bash(uv run jobhunt *),Read,Write,Glob,mcp__claude_ai_Gmail__search_threads,mcp__claude_ai_Gmail__get_thread" `
            --permission-prompts none `
            --output-format json
    }
} else {
    $steps["claude_scan"] = "skipped: claude.exe not found at $claude"
    "claude.exe not found at $claude; skipped Gmail + scoring step" | Out-File -FilePath $log -Append -Encoding utf8
}

Run-Step "dashboard" { uv run jobhunt dashboard }

$failed = @($steps.GetEnumerator() | Where-Object { $_.Value -is [int] -and $_.Value -ne 0 } | ForEach-Object { $_.Key })
$skipped = @($steps.GetEnumerator() | Where-Object { $_.Value -is [string] } | ForEach-Object { $_.Key })
$summary = [ordered]@{
    started_at  = $started
    finished_at = Get-Date -Format o
    ok          = ($failed.Count -eq 0 -and $skipped.Count -eq 0)
    failed      = $failed
    skipped     = $skipped
    steps       = $steps
    log         = $log
}
$json = $summary | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($statusFile, $json, [System.Text.UTF8Encoding]::new($false))
"=== $(Get-Date -Format o) done; failed=[$($failed -join ', ')] skipped=[$($skipped -join ', ')] ===" | Out-File -FilePath $log -Append -Encoding utf8
if (-not $summary.ok) { exit 1 }
exit 0
