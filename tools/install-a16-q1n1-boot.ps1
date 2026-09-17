# Install the q1n1 USB/proxy payload and the persistent boot-loop script on the
# existing ESP. Windows stays first in the firmware boot order; the q1n1 shell
# entry is only reached when something arms BootNext (boot-a16-q1n1.ps1, or the
# q1n1 boot window itself). The script always falls back to Windows.
# Run elevated on the A16 with the payload staged next to this file.
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PayloadSHA256,
    [string]$PayloadFile='q1n1-usb.efi',
    [switch]$Update
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$root=$PSScriptRoot
$source=Join-Path $root $PayloadFile
$relative='\EFI\q1n1\q1n1-usb.efi'
$knownGood='379558299150a121220ee40b7e8095238aad3474c48a695e0b121bf93c41dd32'
$entry='{cef50169-b1ff-11f1-94fe-c41375be4bea}'
# fs2: is the confirmed shell mapping for the NVMe ESP on this machine.
$script=@'
@echo -off
if not exist fs2:\EFI\q1n1\q1n1-usb.efi then
  fs2:\EFI\Microsoft\Boot\bootmgfw.efi
endif
fs2:\EFI\q1n1\q1n1-usb.efi --boot --auto
if %lasterror% == 0 then
  fs2:\EFI\Microsoft\Boot\bootmgfw.efi
endif
'@
$scriptText=($script -replace "`r?`n","`r`n")+"`r`n"

function Hash([string]$Path){(Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()}
function Bcd([string[]]$Arguments){
    $text=(& bcdedit.exe @Arguments 2>&1 | Out-String)
    if($LASTEXITCODE -ne 0){throw "bcdedit failed: $text"}
    $text
}
function TextHash([string]$Text){
    $stream=[IO.MemoryStream]::new([Text.Encoding]::ASCII.GetBytes($Text))
    (Get-FileHash -InputStream $stream -Algorithm SHA256).Hash.ToLower()
}

if((Hash $source) -ne $PayloadSHA256){throw 'Staged payload hash mismatch.'}
$system=Get-CimInstance Win32_ComputerSystem
$part=Get-Partition -DiskNumber 0 -PartitionNumber 12
$disk=Get-Disk -Number 0
if($system.Model -notlike '*UX3607OA*' -or
   $part.GptType -ne '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' -or $part.Size -ne 471859200 -or
   $disk.FriendlyName -ne 'SAMSUNG MZVL81T0HFLB-00BTW' -or !$disk.IsBoot){throw 'Target identity changed.'}
$recordPath=Join-Path $root 'esp-install.json'
$record=Get-Content $recordPath -Raw | ConvertFrom-Json
if($record.State -ne 'installed'){throw 'Expected a completed q1n1 installation.'}
$entryText=Bcd @('/enum',$entry)
if($entryText -notmatch '\\EFI\\q1n1\\shellaa64\.efi'){throw 'The q1n1 firmware entry no longer points at the verified shell.'}
if(Test-Path 'Q:\'){throw 'Q: is already in use.'}
$mounted=$false
try {
    & mountvol.exe Q: /S
    if($LASTEXITCODE -ne 0){throw 'Could not mount the existing ESP.'}
    $mounted=$true
    $volume=((& mountvol.exe Q: /L | Out-String).Trim())
    if($LASTEXITCODE -ne 0 -or $part.AccessPaths -notcontains $volume){throw 'Mounted ESP identity mismatch.'}
    $before=Bcd @('/enum','firmware','/v')
    if($before -notmatch 'displayorder\s+\{9dea862c-5cdd-4e70-acc1-f32b344d4795\}'){
        throw 'Windows is not first in firmware boot order.'
    }
    foreach($file in $record.Files){
        if($file.Path -eq $relative){continue}
        if((Hash "Q:$($file.Path)") -ne $file.SHA256){throw "Existing file changed: $($file.Path)"}
    }
    if((Hash 'Q:\EFI\q1n1\q1n1.efi') -ne $knownGood){throw 'Known-good EL2 payload changed.'}
    $windowsHash=Hash 'Q:\EFI\Microsoft\Boot\bootmgfw.efi'

    $target="Q:$relative"
    $replaced=$null
    if(Test-Path -LiteralPath $target){
        $existing=Hash $target
        if($existing -ne $PayloadSHA256){
            if(!$Update){throw 'A different q1n1-usb.efi is installed; rerun with -Update to replace it.'}
            $backup=Join-Path $root ('q1n1-usb-'+$existing.Substring(0,12)+'.efi')
            if(!(Test-Path $backup)){Copy-Item -LiteralPath $target -Destination $backup}
            Copy-Item -LiteralPath $source -Destination $target -Force
            $replaced=$existing
        }
    } else {
        Copy-Item -LiteralPath $source -Destination $target
    }
    if((Hash $target) -ne $PayloadSHA256){throw 'ESP payload verification failed.'}

    $startup='Q:\startup.nsh'
    $wantHash=TextHash $scriptText
    if(Test-Path $startup){
        $haveHash=Hash $startup
        if($haveHash -ne $wantHash){
            if(!$Update){throw 'A different \startup.nsh exists (a one-time probe run?); collect it first or rerun with -Update.'}
            Copy-Item -LiteralPath $startup -Destination (Join-Path $root ('startup-'+$haveHash.Substring(0,12)+'.nsh'))
            [IO.File]::WriteAllText($startup,$scriptText,[Text.Encoding]::ASCII)
        }
    } else {
        [IO.File]::WriteAllText($startup,$scriptText,[Text.Encoding]::ASCII)
    }
    if((Hash $startup) -ne $wantHash){throw 'Boot script verification failed.'}

    if(!(@($record.Files | Where-Object Path -eq $relative).Count)){
        $backup=Join-Path $root 'esp-install-before-q1n1-boot.json'
        if(!(Test-Path $backup)){Copy-Item -LiteralPath $recordPath -Destination $backup}
        $record.Files=@($record.Files)+[pscustomobject]@{Path=$relative;SHA256=$PayloadSHA256}
    } else {
        foreach($file in $record.Files){if($file.Path -eq $relative){$file.SHA256=$PayloadSHA256}}
    }
    $temporary=Join-Path $root 'esp-install-q1n1-boot.tmp'
    $record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporary
    Move-Item -LiteralPath $temporary -Destination $recordPath -Force

    if((Hash 'Q:\EFI\q1n1\q1n1.efi') -ne $knownGood -or
       (Hash 'Q:\EFI\Microsoft\Boot\bootmgfw.efi') -ne $windowsHash -or
       (Bcd @('/enum','firmware','/v')) -ne $before){throw 'Unexpected change to existing boot state.'}

    $report=[ordered]@{State='installed';Payload=$relative;PayloadSHA256=$PayloadSHA256;
        ReplacedPayloadSHA256=$replaced;StartupPath='\startup.nsh';StartupSHA256=$wantHash;
        BootEntry=$entry;KnownGoodPayloadSHA256=$knownGood;WindowsBootManagerSHA256=$windowsHash;
        WindowsDefaultPreserved=$true;CompletedUtc=[DateTime]::UtcNow.ToString('o')}
    $report | ConvertTo-Json | Set-Content (Join-Path $root 'q1n1-boot.json')
    $report | ConvertTo-Json -Compress
} finally {
    if($mounted){& mountvol.exe Q: /D | Out-Null}
}
