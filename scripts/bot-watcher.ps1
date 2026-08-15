# bot-watcher.ps1 — Ties the claude-code-telegram bot lifecycle to Claude Desktop.
#
# Behavior:
#   - Claude Desktop (claude.exe from WindowsApps/AnthropicClaude) running  -> bot runs
#   - Claude Desktop closed                                                 -> bot is stopped
#   - Reopened                                                              -> bot restarts automatically
#
# Installed at Windows login (hidden) via Startup folder launcher ClaudeBotWatcher.vbs.
# Single instance enforced with a named mutex.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File bot-watcher.ps1        (run forever)
#   powershell ... -File bot-watcher.ps1 -DryRun -Once                         (one dry-run cycle)
#   powershell ... -File bot-watcher.ps1 -StopBot                               (stop bot, exit)

param(
    [switch]$DryRun,   # log actions without executing them
    [switch]$Once,     # single cycle, then exit
    [switch]$StopBot,  # stop the bot (tree) and exit
    [switch]$SimulateDesktopOff,          # TEST ONLY: pretend Claude Desktop is closed
    [int]$SimulateSeconds = 10            # TEST ONLY: revert to real detection after N seconds
)

$ErrorActionPreference = 'SilentlyContinue'

# --- Configuration -----------------------------------------------------------
$BotDir     = 'D:\AI-Dev-Data\AI-Tools\claude-code-telegram'
$BotPython  = Join-Path $BotDir '.venv\Scripts\python.exe'
$BotArgs    = @('-m', 'src.main')
$OutLog     = Join-Path $BotDir 'logs\bot.out.log'
$ErrLog     = Join-Path $BotDir 'logs\bot.err.log'
$WatcherLog = Join-Path $BotDir 'logs\bot-watcher.log'
$CheckMs    = 3000
$MinRestartGapSec = 10   # don't restart-loop if the bot crashes instantly

# --- Single-instance guard ----------------------------------------------------
$mutex = New-Object System.Threading.Mutex($false, 'Local\ClaudeBotWatcher')
if (-not $mutex.WaitOne(0)) {
    exit 0   # another watcher is already running
}

function Write-WatcherLog([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $WatcherLog -Value $line
    Write-Host $line
}

function Get-BotProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name LIKE 'claude-telegram-bot%'" |
        Where-Object { $_.CommandLine -match 'src\.main|claude.?telegram.?bot' -or $_.Name -like 'claude-telegram-bot*' }
}

function Get-ClaudeDesktopRunning {
    $procs = Get-CimInstance Win32_Process -Filter "Name='claude.exe'" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        if ($p.ExecutablePath -match 'WindowsApps\\Claude_|AnthropicClaude') { return $true }
    }
    return $false
}

function Stop-BotTree([int]$RootPid) {
    # Collect descendants (up to 3 levels) and kill them deepest-first, then the root.
    $all = Get-CimInstance Win32_Process
    $toKill = New-Object System.Collections.Generic.List[int]
    $frontier = @($RootPid)
    for ($i = 0; $i -lt 3 -and $frontier.Count -gt 0; $i++) {
        $children = @($all | Where-Object { $frontier -contains $_.ParentProcessId })
        $frontier = @($children | ForEach-Object { [int]$_.ProcessId })
        foreach ($c in $children) { $toKill.Add([int]$c.ProcessId) }
    }
    for ($j = $toKill.Count - 1; $j -ge 0; $j--) {
        Stop-Process -Id $toKill[$j] -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $RootPid -Force -ErrorAction SilentlyContinue
}

function Start-Bot {
    if ($DryRun) { Write-WatcherLog "DRY-RUN: would start bot ($BotPython $BotArgs)"; return }
    $p = Start-Process -FilePath $BotPython -ArgumentList $BotArgs `
        -WorkingDirectory $BotDir -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru
    Write-WatcherLog "Bot started (pid $($p.Id))"
}

$script:lastStartAt = 0

function Stop-Bot {
    $bots = @(Get-BotProcesses)
    if ($bots.Count -eq 0) { return $false }
    foreach ($b in $bots) {
        Write-WatcherLog "Claude Desktop closed -> stopping bot (pid $($b.ProcessId))"
        if (-not $DryRun) { Stop-BotTree ([int]$b.ProcessId) }
    }
    return $true
}

# --- Manual stop mode ----------------------------------------------------------
if ($StopBot) {
    Stop-Bot | Out-Null
    Write-WatcherLog "StopBot mode: done."
    exit 0
}

# --- Main loop ------------------------------------------------------------------
if ($SimulateDesktopOff) { $simEnds = (Get-Date).AddSeconds($SimulateSeconds) }
Write-WatcherLog "Watcher started. Claude Desktop <-> bot lifecycle enabled."
while ($true) {
    try {
        $desktopUp = Get-ClaudeDesktopRunning
        if ($SimulateDesktopOff) {
            $desktopUp = $false
            Write-WatcherLog "TEST: simulating Claude Desktop off until $($simEnds.ToString('HH:mm:ss'))"
        }
        $bots      = @(Get-BotProcesses)

        if ($desktopUp) {
            if ($bots.Count -eq 0) {
                $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                if ($now - $script:lastStartAt -ge $MinRestartGapSec) {
                    Start-Bot
                    $script:lastStartAt = $now
                }
            }
        } else {
            Stop-Bot | Out-Null
        }
    } catch {
        Write-WatcherLog "Watcher error: $($_.Exception.Message)"
    }
    if ($Once) { break }
    if ($SimulateDesktopOff -and (Get-Date) -ge $simEnds) { $SimulateDesktopOff = $false }
    Start-Sleep -Milliseconds $CheckMs
}

Write-WatcherLog "Watcher exiting."
