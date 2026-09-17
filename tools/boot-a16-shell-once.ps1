# Select the installed q1n1 shell for one boot; leave the normal boot order alone.
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$record=Get-Content (Join-Path $PSScriptRoot 'esp-install.json') -Raw | ConvertFrom-Json
if($env:COMPUTERNAME -ne 'MCRUZ-ASUS' -or $record.State -ne 'installed' -or
   $record.BootEntry -ne '{cef50169-b1ff-11f1-94fe-c41375be4bea}'){
    throw 'Expected the verified A16 q1n1 installation.'
}
$entry=(& bcdedit.exe /enum $record.BootEntry | Out-String)
if($LASTEXITCODE -ne 0 -or $entry -notmatch '\\EFI\\q1n1\\shellaa64\.efi'){
    throw 'The q1n1 firmware entry no longer points to the verified shell.'
}
& bcdedit.exe /set '{fwbootmgr}' bootsequence $record.BootEntry
if($LASTEXITCODE -ne 0){throw 'Could not select the one-time boot entry.'}
$firmware=(& bcdedit.exe /enum '{fwbootmgr}' | Out-String)
if($LASTEXITCODE -ne 0 -or $firmware -notmatch ('bootsequence\s+'+[regex]::Escape($record.BootEntry))){
    throw 'One-time boot selection did not verify.'
}
$firmware
[ordered]@{State='reboot-requested';BootEntry=$record.BootEntry;
    RequestedUtc=[DateTime]::UtcNow.ToString('o');ForceAppsClosed=$false} |
    ConvertTo-Json | Set-Content (Join-Path $PSScriptRoot 'shell-boot-request.json')
# A zero timeout without /f does not request forcibly closing applications.
& shutdown.exe /r /t 0
if($LASTEXITCODE -ne 0){throw 'Windows rejected the restart request; one-time boot remains selected.'}
