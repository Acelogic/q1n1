# Collect the one-time EFI result and remove only our exact temporary startup script.
param([ValidateSet('register','status','status-v2','device0','device0-v2','device0-v3','device0-v4','serial-dock','dock-probe','controller-dock','serial-dock-v3','serial-dock-v3-r2','ebs-takeover')][string]$Probe='register')
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$stem='ucsi-'+$Probe+'-once'
if($Probe -eq 'serial-dock'){$stem='usb-serial-dock-once'}
if($Probe -eq 'dock-probe'){$stem='usb-dock-probe-once'}
if($Probe -eq 'controller-dock'){$stem='usb-controller-dock-once'}
if($Probe -eq 'serial-dock-v3'){$stem='usb-serial-dock-v3-once'}
if($Probe -eq 'serial-dock-v3-r2'){$stem='usb-serial-dock-v3-r2-once'}
if($Probe -eq 'ebs-takeover'){$stem='usb-ebs-once'}
$metadata=Get-Content (Join-Path $root ($stem+'.json')) -Raw | ConvertFrom-Json
if($metadata.StartupPath -ne '\startup.nsh' -or $metadata.LogPath -ne ('\EFI\q1n1\'+$stem+'.log')){throw 'Unexpected one-time paths.'}
if((Get-CimInstance Win32_ComputerSystem).Model -notlike '*UX3607OA*'){throw 'Unexpected machine.'}
$part=Get-Partition -DiskNumber 0 -PartitionNumber 12
if($part.GptType -ne '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' -or $part.Size -ne 471859200){throw 'ESP identity changed.'}
if(Test-Path 'Q:\'){throw 'Q drive busy.'}
mountvol.exe Q: /S
if($LASTEXITCODE -ne 0){throw 'Could not mount ESP.'}
try {
    $volume=((& mountvol.exe Q: /L | Out-String).Trim())
    if($LASTEXITCODE -ne 0 -or $part.AccessPaths -notcontains $volume){throw 'Mounted ESP mismatch.'}
    $startup='Q:\startup.nsh'
    if(!(Test-Path $startup)){throw 'Expected temporary startup script is missing.'}
    if((Get-FileHash $startup).Hash.ToLower() -ne $metadata.StartupSHA256){throw 'Startup script changed; preserving it.'}
    $log='Q:\EFI\q1n1\'+$stem+'.log'
    $found=Test-Path $log
    if($found){Copy-Item $log (Join-Path $root ($stem+'.log'))}
    $serialFound=$false
    if($Probe -in @('device0','device0-v2','device0-v3','device0-v4')) {
        $serialName='ucsi-'+$Probe+'-serial.log'
        $serialFound=Test-Path ('Q:\EFI\q1n1\'+$serialName)
        if($serialFound){Copy-Item ('Q:\EFI\q1n1\'+$serialName) (Join-Path $root $serialName)}
    }
    Copy-Item $startup (Join-Path $root ($stem+'.startup.nsh'))
    Remove-Item -LiteralPath $startup
    $os=Get-CimInstance Win32_OperatingSystem
    $result=[ordered]@{
        CollectedUtc=[DateTime]::UtcNow.ToString('o');ResultFound=$found
        SerialLogFound=$serialFound
        StartupRemoved=!(Test-Path $startup);StartupSHA256=$metadata.StartupSHA256
        WindowsVersion=$os.Version;LastBootUpTime=$os.LastBootUpTime.ToUniversalTime().ToString('o')
        LogSHA256=if($found){(Get-FileHash $log).Hash.ToLower()}else{$null}
    }
    $result | ConvertTo-Json | Set-Content (Join-Path $root ($stem+'-collected.json'))
    $result | ConvertTo-Json
    if($found){Get-Content $log}
    if($serialFound){Get-Content (Join-Path $root $serialName)}
} finally {mountvol.exe Q: /D}
