# Add only the selected discovery or role-test EFI app to the existing ESP directory.
# Retain the tested boot payload and append the new file to the removal manifest.
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ProbeSHA256,
    [ValidateSet('q1n1-usb-probe.efi','q1n1-usb-role-probe.efi','q1n1-usb-role-change.efi','q1n1-usb-serial.efi','q1n1-usb-serial-v2.efi','q1n1-usb-serial-v3.efi','q1n1-usb-typec-probe.efi','q1n1-usb-ucsi-probe.efi','q1n1-usb-usbc-probe.efi','q1n1-usb-ucsi-reg-probe.efi','q1n1-usb-ucsi-status.efi','q1n1-usb-ucsi-status-v2.efi','q1n1-usb-ucsi-device0.efi','q1n1-usb-ucsi-device0-v2.efi','q1n1-usb-ucsi-device0-v3.efi','q1n1-usb-ucsi-device0-v4.efi','q1n1-usb-controller-probe.efi','q1n1-usb-ebs-prep.efi','q1n1-usb-ebs.efi')][string]$ProbeFile='q1n1-usb-probe.efi'
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$source=Join-Path $root $ProbeFile
$tag=$ProbeFile.Replace('q1n1-','').Replace('.efi','')
$recordPath=Join-Path $root 'esp-install.json'
$relative='\EFI\q1n1\'+$ProbeFile
function Hash([string]$Path){(Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()}
function Bcd([string[]]$Arguments){
    $text=(& bcdedit.exe @Arguments 2>&1 | Out-String)
    if($LASTEXITCODE -ne 0){throw "bcdedit failed: $text"}
    $text
}
if((Hash $source) -ne $ProbeSHA256){throw 'Staged probe hash mismatch.'}
$record=Get-Content $recordPath -Raw | ConvertFrom-Json
if($record.State -ne 'installed'){throw 'Expected a completed q1n1 installation.'}
$system=Get-CimInstance Win32_ComputerSystem
$part=Get-Partition -DiskNumber 0 -PartitionNumber 12
$disk=Get-Disk -Number 0
if($system.Model -notlike '*UX3607OA*' -or
   $part.GptType -ne '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' -or $part.Size -ne 471859200 -or
   $disk.FriendlyName -ne 'SAMSUNG MZVL81T0HFLB-00BTW' -or !$disk.IsBoot){throw 'Target identity changed.'}
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
        if($file.Path -notmatch '^\\EFI\\q1n1\\[^\\]+\.efi$' -and $file.Path -ne '\tcblaunch.exe'){
            throw 'Unexpected path in existing manifest.'
        }
        if((Hash "Q:$($file.Path)") -ne $file.SHA256){throw "Existing file changed: $($file.Path)"}
    }
    $knownGood='379558299150a121220ee40b7e8095238aad3474c48a695e0b121bf93c41dd32'
    if((Hash 'Q:\EFI\q1n1\q1n1.efi') -ne $knownGood){throw 'Known-good EL2 payload changed.'}
    $windowsHash=Hash 'Q:\EFI\Microsoft\Boot\bootmgfw.efi'
    $target="Q:$relative"
    if(Test-Path -LiteralPath $target){
        if((Hash $target) -ne $ProbeSHA256){throw 'A different probe already exists; refusing overwrite.'}
    } else {Copy-Item -LiteralPath $source -Destination $target}
    if((Hash $target) -ne $ProbeSHA256){throw 'ESP probe verification failed.'}
    if((Hash 'Q:\EFI\q1n1\q1n1.efi') -ne $knownGood -or
       (Hash 'Q:\EFI\Microsoft\Boot\bootmgfw.efi') -ne $windowsHash -or
       (Bcd @('/enum','firmware','/v')) -ne $before){throw 'Unexpected change to existing boot state.'}
    if(!(@($record.Files | Where-Object Path -eq $relative).Count)){
        $backup=Join-Path $root ('esp-install-before-'+$tag+'.json')
        if(!(Test-Path $backup)){Copy-Item -LiteralPath $recordPath -Destination $backup}
        $record.Files=@($record.Files)+[pscustomobject]@{Path=$relative;SHA256=$ProbeSHA256}
        $temporary=Join-Path $root ('esp-install-'+$tag+'.tmp')
        $record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporary
        Move-Item -LiteralPath $temporary -Destination $recordPath -Force
    }
    $report=[ordered]@{State='installed';Path=$relative;SHA256=$ProbeSHA256;
        KnownGoodPayloadSHA256=$knownGood;WindowsBootManagerSHA256=$windowsHash;
        WindowsDefaultPreserved=$true;Rebooted=$false;HardwareProbeRun=$false;
        CompletedUtc=[DateTime]::UtcNow.ToString('o')}
    $report | ConvertTo-Json | Set-Content (Join-Path $root ($tag+'-install.json'))
    $report | ConvertTo-Json -Compress
} finally {
    if($mounted){& mountvol.exe Q: /D | Out-Null}
}
