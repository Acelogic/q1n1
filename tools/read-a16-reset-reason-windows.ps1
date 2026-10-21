# SPDX-License-Identifier: MIT
# Read, in Windows, what firmware and Windows recorded about the last reset of the A16.
# READ-ONLY: no registry writes, no BCD/boot-entry changes, no drive letters assigned, nothing cleared.
# Run it in the first Windows boot after an unexplained reset (an A16 reset lands in Windows because
# BootNext is one-shot), e.g. over ssh: powershell -NoProfile -ExecutionPolicy Bypass -File <this> -Hours 2
#
# What it collects (docs/A16-ADRENO-STAGE3-RUNBOOK.md, "After an A16 reset"):
#  - System event log entries around the reset: Kernel-Power 41, EventLog 6008 (unexpected shutdown),
#    Kernel-Boot and Kernel-General boot records, and every WHEA-Logger event. Firmware that reports a
#    fatal error of the previous boot through the ACPI BERT table shows up as WHEA-Logger events.
#    INFERRED: whether the A16's firmware fills BERT after a TrustZone-detected fault is not known.
#  - Lines mentioning PON / reset / watchdog / PS_HOLD / crash in the text logs of the firmware's
#    "logfs" FAT volume (Qualcomm UEFI keeps its boot logs there; INFERRED that the PMIC power-on
#    and reset reasons appear in them on this unit). The volume is read through its \\?\Volume{..}\
#    path; no drive letter is assigned.
param([double]$Hours = 6, [string]$OutputPath = (Join-Path $PSScriptRoot 'a16-reset-reason.json'))
$ErrorActionPreference = 'Stop'
if ((Get-CimInstance Win32_ComputerSystem).Model -notlike '*UX3607OA*') { throw 'Unexpected machine.' }
$since = (Get-Date).AddHours(-$Hours)
$report = [ordered]@{ CapturedUtc = [DateTime]::UtcNow.ToString('o'); Since = $since.ToUniversalTime().ToString('o') }

$os = Get-CimInstance Win32_OperatingSystem
$report.LastBootUpTimeUtc = $os.LastBootUpTime.ToUniversalTime().ToString('o')

$providers = @('Microsoft-Windows-Kernel-Power', 'EventLog', 'Microsoft-Windows-Kernel-Boot',
               'Microsoft-Windows-Kernel-General', 'Microsoft-Windows-WHEA-Logger')
$events = @()
foreach ($provider in $providers) {
    try {
        $events += Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = $provider; StartTime = $since } `
            -ErrorAction Stop
    } catch [System.Exception] {
        if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { $report["error_$provider"] = $_.ToString() }
    }
}
$report.Events = @($events | Sort-Object TimeCreated | ForEach-Object {
    [ordered]@{ TimeUtc = $_.TimeCreated.ToUniversalTime().ToString('o'); Provider = $_.ProviderName; Id = $_.Id;
                Level = $_.LevelDisplayName; Message = ($_.Message -split "`r?`n" | Select-Object -First 12) -join ' | ' }
})

# Firmware logs on the logfs volume (read-only, no drive letter).
$volumes = @(Get-Volume | Where-Object { $_.FileSystemLabel -match '^(?i)logfs$' })
$report.LogfsVolumes = @($volumes | ForEach-Object { $_.Path })
$pattern = '(?i)(PON|POFF|power.?on.?reason|reset.?reason|warm.?reset|RESIN|PS_HOLD|watchdog|wdog|bite|crash|fatal|XPU|NOC.?err|dload|ramdump)'
$hits = @()
foreach ($volume in $volumes) {
    $files = Get-ChildItem -LiteralPath $volume.Path -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Length -lt 8MB } | Sort-Object LastWriteTime -Descending | Select-Object -First 12
    foreach ($file in $files) {
        $entry = [ordered]@{ File = $file.FullName; LastWriteUtc = $file.LastWriteTimeUtc.ToString('o'); Lines = @() }
        try {
            $entry.Lines = @(Select-String -LiteralPath $file.FullName -Pattern $pattern -ErrorAction Stop |
                Select-Object -First 80 | ForEach-Object { "$($_.LineNumber): $($_.Line.Trim())" })
        } catch { $entry.Error = $_.ToString() }
        $hits += $entry
    }
}
$report.LogfsHits = $hits
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Output "wrote $OutputPath ($($report.Events.Count) events, $($hits.Count) logfs files scanned)"
