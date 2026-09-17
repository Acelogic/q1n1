# Install a separate q1n1 shell entry on the existing A16 ESP. Never repartition.
# Run elevated on the A16 with a sibling kit/ folder produced by make-a16-kit.sh.
param([switch]$Resume)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$root=$PSScriptRoot
$kit=Join-Path $root 'kit'
$resultFile=Join-Path $root 'esp-install.json'
if(Test-Path $resultFile){
    $prior=Get-Content $resultFile -Raw | ConvertFrom-Json
    if(!$Resume -or $prior.State -ne 'failed'){throw 'An installation record already exists; inspect it before rerunning.'}
    Move-Item -LiteralPath $resultFile -Destination (Join-Path $root ('esp-install-failed-'+[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')+'.json'))
}
$system=Get-CimInstance Win32_ComputerSystem
if($system.Model -notlike '*UX3607OA*'){throw 'This installer is restricted to the inspected UX3607OA.'}
$part=Get-Partition -DiskNumber 0 -PartitionNumber 12
$disk=Get-Disk -Number 0
if($part.GptType -ne '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' -or $part.Size -ne 471859200 -or
   $disk.FriendlyName -ne 'SAMSUNG MZVL81T0HFLB-00BTW' -or !$disk.IsBoot){throw 'ESP/disk identity changed.'}
if(Test-Path 'Q:\'){throw 'Q: is already in use; do not take over an existing mount.'}

function Bcd([string[]]$Arguments){
    $text=(& bcdedit.exe @Arguments 2>&1 | Out-String)
    if($LASTEXITCODE -ne 0){throw "bcdedit failed: $text"}
    return $text
}
function Hash([string]$Path){return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()}

# Verify every supplied executable against the locally generated manifest first.
foreach($line in Get-Content (Join-Path $kit 'SHA256SUMS')){
    if($line -notmatch '^([0-9a-f]{64})\s+(.+)$'){throw 'Invalid SHA256SUMS entry.'}
    $expected=$Matches[1]; $relative=$Matches[2].Replace('/','\')
    if((Hash (Join-Path $kit $relative)) -ne $expected){throw "Staged hash mismatch: $relative"}
}
$before=Bcd @('/enum','firmware','/v')
if($before -match 'q1n1 UEFI Shell'){throw 'A q1n1 firmware entry already exists; refusing a duplicate.'}
$before | Set-Content (Join-Path $root 'firmware-before.txt')
$bootmgrBefore=Bcd @('/enum','{bootmgr}','/v')
$bootmgrBefore | Set-Content (Join-Path $root 'windows-bootmgr-before.txt')
if(!(Test-Path (Join-Path $root 'bcd-before.bak'))){
    Bcd @('/export',(Join-Path $root 'bcd-before.bak')) | Out-Null
}
$id=$null
$installed=@()
$mounted=$false
try {
    & mountvol.exe Q: /S
    if($LASTEXITCODE -ne 0){throw 'Could not mount the existing ESP.'}
    $mounted=$true
    # mountvol /S creates a DOS-device mount that Storage CIM need not expose
    # as a DriveLetter. Resolve its volume GUID and match the known partition.
    $mountedVolume=((& mountvol.exe Q: /L | Out-String).Trim())
    if($LASTEXITCODE -ne 0 -or $part.AccessPaths -notcontains $mountedVolume){throw 'Mounted ESP volume identity failed.'}
    $vol=Get-Volume -UniqueId $mountedVolume
    if($vol.FileSystem -ne 'FAT32' -or $vol.SizeRemaining -lt 32MB){throw 'Mounted ESP free space failed.'}
    $dest='Q:\EFI\q1n1'
    if(!$Resume -and ((Test-Path $dest) -or (Test-Path 'Q:\tcblaunch.exe'))){throw 'Existing q1n1 or TCB file found; refusing overwrite.'}
    $windowsHash=Hash 'Q:\EFI\Microsoft\Boot\bootmgfw.efi'
    # bcdedit renders the same volume as partition=Q: while it is mounted.
    # Compare both snapshots under the same mount state.
    $bootmgrMountedBefore=Bcd @('/enum','{bootmgr}','/v')
    if(!(Test-Path $dest)){New-Item -ItemType Directory -Path $dest | Out-Null}
    $files=@(
        @{Source='q1n1.efi'; Target='Q:\EFI\q1n1\q1n1.efi'},
        @{Source='sltest.efi'; Target='Q:\EFI\q1n1\sltest.efi'},
        @{Source='slbounce-always.efi'; Target='Q:\EFI\q1n1\slbounce-always.efi'},
        @{Source='EFI\BOOT\BOOTAA64.EFI'; Target='Q:\EFI\q1n1\shellaa64.efi'},
        @{Source='tcblaunch.exe'; Target='Q:\tcblaunch.exe'}
    )
    foreach($f in $files){
        $sourceHash=Hash (Join-Path $kit $f.Source)
        if(!(Test-Path -LiteralPath $f.Target)){
            Copy-Item -LiteralPath (Join-Path $kit $f.Source) -Destination $f.Target
        }
        if((Hash $f.Target) -ne $sourceHash){throw "ESP copy verification failed: $($f.Target)"}
        $installed+=@{Path=$f.Target.Substring(2);SHA256=$sourceHash}
    }
    $created=Bcd @('/copy','{bootmgr}','/d','q1n1 UEFI Shell')
    if($created -notmatch '\{[0-9a-fA-F-]{36}\}'){throw "No new BCD identifier: $created"}
    $id=$Matches[0]
    # Record the identifier immediately for recovery from any later error.
    @{State='entry-created';BootEntry=$id;Files=$installed} | ConvertTo-Json -Depth 5 | Set-Content $resultFile
    Bcd @('/set',$id,'device','partition=Q:') | Out-Null
    Bcd @('/set',$id,'path','\EFI\q1n1\shellaa64.efi') | Out-Null
    Bcd @('/set','{fwbootmgr}','displayorder',$id,'/addlast') | Out-Null
    if((Bcd @('/enum','{bootmgr}','/v')) -ne $bootmgrMountedBefore){throw 'Windows boot-manager configuration changed unexpectedly.'}
    if((Hash 'Q:\EFI\Microsoft\Boot\bootmgfw.efi') -ne $windowsHash){throw 'Windows boot-manager hash changed unexpectedly.'}
    $order=Bcd @('/enum','{fwbootmgr}','/v')
    if($order -notmatch 'displayorder\s+\{9dea862c-5cdd-4e70-acc1-f32b344d4795\}'){
        throw 'Windows is not first in firmware boot order.'
    }
    $after=Bcd @('/enum','firmware','/v')
    $after | Set-Content (Join-Path $root 'firmware-after.txt')
    $report=[ordered]@{State='installed';BootEntry=$id;Description='q1n1 UEFI Shell';
        Disk=0;Partition=12;PartitionResized=$false;WindowsDefaultPreserved=$true;
        WindowsBootManagerSHA256=$windowsHash;Files=$installed;
        Rebooted=$false;SecureLaunchTested=$false;CompletedUtc=[DateTime]::UtcNow.ToString('o')}
    $report | ConvertTo-Json -Depth 6 | Set-Content $resultFile
    $report | ConvertTo-Json -Depth 6 -Compress
} catch {
    # If firmware registration fails, remove only the new entry, not the BCD store.
    if($id){& bcdedit.exe /delete $id 2>&1 | Out-Null}
    @{State='failed';BootEntry=$id;Files=$installed;Error=$_.Exception.Message} |
        ConvertTo-Json -Depth 5 | Set-Content $resultFile
    throw
} finally {
    if($mounted){& mountvol.exe Q: /D | Out-Null}
}
