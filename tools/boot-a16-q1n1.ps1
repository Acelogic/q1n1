# Arm one boot of the q1n1 shell entry (which runs the boot-loop script) and
# restart. The normal boot order stays Windows-first; BootNext is consumed by
# that single boot, and only the q1n1 boot window re-arms it.
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$root=$PSScriptRoot
$record=Get-Content (Join-Path $root 'q1n1-boot.json') -Raw | ConvertFrom-Json
$esp=Get-Content (Join-Path $root 'esp-install.json') -Raw | ConvertFrom-Json
if($env:COMPUTERNAME -ne 'MCRUZ-ASUS' -or $record.State -ne 'installed' -or
   $record.BootEntry -ne '{cef50169-b1ff-11f1-94fe-c41375be4bea}' -or $esp.State -ne 'installed'){
    throw 'Expected the verified A16 q1n1 boot installation.'
}
function Hash([string]$Path){(Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()}
$entry=(& bcdedit.exe /enum $record.BootEntry | Out-String)
if($LASTEXITCODE -ne 0 -or $entry -notmatch '\\EFI\\q1n1\\shellaa64\.efi'){
    throw 'The q1n1 firmware entry no longer points to the verified shell.'
}
if(Test-Path 'Q:\'){throw 'Q drive busy.'}
& mountvol.exe Q: /S
if($LASTEXITCODE -ne 0){throw 'Could not mount ESP.'}
try {
    if((Hash ('Q:'+$record.Payload)) -ne $record.PayloadSHA256){throw 'Installed q1n1 payload changed.'}
    if((Hash ('Q:'+$record.StartupPath)) -ne $record.StartupSHA256){throw 'Boot script changed.'}
    if((Hash 'Q:\EFI\Microsoft\Boot\bootmgfw.efi') -ne $record.WindowsBootManagerSHA256){throw 'Windows loader changed.'}
} finally {& mountvol.exe Q: /D | Out-Null}
& bcdedit.exe /set '{fwbootmgr}' bootsequence $record.BootEntry
if($LASTEXITCODE -ne 0){throw 'Could not select the one-time boot entry.'}
$firmware=(& bcdedit.exe /enum '{fwbootmgr}' | Out-String)
if($LASTEXITCODE -ne 0 -or $firmware -notmatch ('bootsequence\s+'+[regex]::Escape($record.BootEntry))){
    throw 'One-time boot selection did not verify.'
}
[ordered]@{State='reboot-requested';BootEntry=$record.BootEntry;Target='q1n1 boot window';
    RequestedUtc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json |
    Set-Content (Join-Path $root 'q1n1-boot-request.json')
'{"State":"rebooting","Target":"q1n1 boot window"}'
# A zero timeout without /f does not request forcibly closing applications.
& shutdown.exe /r /t 0
if($LASTEXITCODE -ne 0){throw 'Windows rejected the restart request; one-time boot remains selected.'}
