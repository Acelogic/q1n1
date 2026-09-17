# Install a temporary shell script for the selected probe, then use the existing one-time entry.
# The script returns to Windows; normal firmware boot order remains Windows-first.
param([ValidateSet('register','status','status-v2','device0','device0-v2','device0-v3','device0-v4','serial-dock','dock-probe','controller-dock','serial-dock-v3','serial-dock-v3-r2','ebs-takeover')][string]$Probe='register')
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$hashes=@{register='1b555e790e75164a35304a393aa0ad6cab64a57aa79ef20b633978ec2dfd65aa';status='561a0fa7a6ce2e8b93f6d3293aff5a7b4a70c3dec22a615806feab43a2acb89b';'status-v2'='80bbcf862f3c0d197a0276c69fbac7f83cc6054a085c9d0811c8b63fd0908d71'}
$probeHash=$hashes[$Probe]
if($Probe -eq 'device0'){$probeHash='73741c63f408bc2ed775bc3f1c75c2f6f01a141ebefa1a664fc23aa553b30b2d'}
if($Probe -eq 'device0-v2'){$probeHash='ecbfa59aeaf7b9b89258055154ac46498deaa83025b0fbb76827fc03ef93ecf1'}
if($Probe -eq 'device0-v3'){$probeHash='33e2de941dacdc7db6e8365d8566f59335950e72e95f4a70c4733341f96cc55c'}
if($Probe -eq 'device0-v4'){$probeHash='875c5007dd7eff0c77f11bb81ef7ab45763ef4d9f65709d44885d999af7f2198'}
$tag=if($Probe -eq 'register'){'usb-ucsi-reg-probe'}else{'usb-ucsi-'+$Probe}
$stem='ucsi-'+$Probe+'-once'
if($Probe -eq 'serial-dock') {
    # The dock supplies the USB host connection. Do not run a UCSI role-swap app.
    $tag='usb-serial-v2'
    $stem='usb-serial-dock-once'
    $probeHash='b2c3a2082fb68d8ddaa2d871ae538cf9dbbc69aadc969e48d01d751165bb913b'
}
if($Probe -eq 'dock-probe') {
    $tag='usb-ucsi-status-v2'
    $stem='usb-dock-probe-once'
    $probeHash='80bbcf862f3c0d197a0276c69fbac7f83cc6054a085c9d0811c8b63fd0908d71'
}
if($Probe -eq 'controller-dock') {
    $tag='usb-controller-probe'
    $stem='usb-controller-dock-once'
    $probeHash='f7a59a10567ffc634d3ea8c9a6879b58072b7c658998f4b4d9baaaf086f29af3'
}
if($Probe -eq 'serial-dock-v3') {
    $tag='usb-serial-v3'
    $stem='usb-serial-dock-v3-once'
    $probeHash='19c5b2414e9736e635d92899ea070c13eef144d69a98854ddb70815e7a115b95'
}
if($Probe -eq 'serial-dock-v3-r2') {
    # Same installed v3 binary; separate result so the first v3 log stays preserved.
    # This rerun exists because the first Mac watcher omitted ioreg -l and could not see ports.
    $tag='usb-serial-v3'
    $stem='usb-serial-dock-v3-r2-once'
    $probeHash='19c5b2414e9736e635d92899ea070c13eef144d69a98854ddb70815e7a115b95'
}
if($Probe -eq 'ebs-takeover') {
    # Firmware prep (returns to shell, logged) then q1n1 DWC3 takeover after
    # ExitBootServices (never returns; resets to the Windows-first boot order).
    $tag='usb-ebs-prep'
    $stem='usb-ebs-once'
    $probeHash='5ad50c56b467d70c038680d4737149aef60fa85163cd8bebfbf1a78d37c701f2'
    $takeoverHash='1953a53daed43b16ed7120e98f34a5a2bead166e592ce3c2c41d61b184930bf6'
    $takeover=Get-Content (Join-Path $root 'usb-ebs-install.json') -Raw | ConvertFrom-Json
    if($takeover.State -ne 'installed' -or $takeover.SHA256 -ne $takeoverHash){throw 'Expected verified takeover installation.'}
}
$report=Get-Content (Join-Path $root ($tag+'-install.json')) -Raw | ConvertFrom-Json
if($report.State -ne 'installed' -or $report.SHA256 -ne $probeHash){throw 'Expected verified probe installation.'}
$windowsHash=$report.WindowsBootManagerSHA256
if($Probe -in @('serial-dock','dock-probe')) {
    # Windows was updated after serial v2 was installed; this loader was verified
    # during the later UCSI v4 installation and physical test.
    $windowsHash='04281e60cf80df2192de3e4c31851b62e5ae9de7c2d1fdc58ce6f62252d868a5'
}
if((Get-CimInstance Win32_ComputerSystem).Model -notlike '*UX3607OA*'){throw 'Unexpected machine.'}
$part=Get-Partition -DiskNumber 0 -PartitionNumber 12
if($part.GptType -ne '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' -or $part.Size -ne 471859200){throw 'ESP identity changed.'}
if(Test-Path 'Q:\'){throw 'Q drive busy.'}
mountvol.exe Q: /S
if($LASTEXITCODE -ne 0){throw 'Could not mount ESP.'}
try {
    $volume=((& mountvol.exe Q: /L | Out-String).Trim())
    if($LASTEXITCODE -ne 0 -or $part.AccessPaths -notcontains $volume){throw 'Mounted ESP mismatch.'}
    if(Test-Path 'Q:\startup.nsh'){throw 'Existing root startup script; refusing replacement.'}
    if((Get-FileHash ('Q:\EFI\q1n1\q1n1-'+$tag+'.efi')).Hash.ToLower() -ne $probeHash){throw 'Probe changed.'}
    if((Get-FileHash 'Q:\EFI\Microsoft\Boot\bootmgfw.efi').Hash.ToLower() -ne $windowsHash){throw 'Windows loader changed.'}
    if($Probe -eq 'ebs-takeover' -and (Get-FileHash 'Q:\EFI\q1n1\q1n1-usb-ebs.efi').Hash.ToLower() -ne $takeoverHash){throw 'Takeover changed.'}
    if(Test-Path ('Q:\EFI\q1n1\'+$stem+'.log')){throw 'Prior result exists; preserve it first.'}
    $script=@'
@echo -off
fs2:\EFI\q1n1\q1n1-{0}.efi > fs2:\EFI\q1n1\{1}.log
type fs2:\EFI\q1n1\{1}.log
fs2:\EFI\Microsoft\Boot\bootmgfw.efi
'@
    $script=$script -f $tag,$stem
    if($Probe -eq 'dock-probe') {
        $manifest=Get-Content (Join-Path $root 'esp-install.json') -Raw | ConvertFrom-Json
        $role=@($manifest.Files | Where-Object Path -eq '\EFI\q1n1\q1n1-usb-role-probe.efi')
        if($role.Count -ne 1 -or (Get-FileHash 'Q:\EFI\q1n1\q1n1-usb-role-probe.efi').Hash.ToLower() -ne $role[0].SHA256){throw 'Role inventory probe identity changed.'}
        $script=@'
@echo -off
fs2:\EFI\q1n1\q1n1-usb-role-probe.efi > fs2:\EFI\q1n1\usb-dock-probe-once.log
fs2:\EFI\q1n1\q1n1-usb-ucsi-status-v2.efi >> fs2:\EFI\q1n1\usb-dock-probe-once.log
type fs2:\EFI\q1n1\usb-dock-probe-once.log
fs2:\EFI\Microsoft\Boot\bootmgfw.efi
'@
    }
    if($Probe -in @('serial-dock','serial-dock-v3','serial-dock-v3-r2')) {
        $script=@'
@echo -off
fs2:\EFI\q1n1\q1n1-{0}.efi --device0 > fs2:\EFI\q1n1\{1}.log
type fs2:\EFI\q1n1\{1}.log
fs2:\EFI\Microsoft\Boot\bootmgfw.efi
'@
        $script=$script -f $tag,$stem
    }
    if($Probe -eq 'ebs-takeover') {
        $script=@'
@echo -off
fs2:\EFI\q1n1\q1n1-{0}.efi --device0 > fs2:\EFI\q1n1\{1}.log
if %lasterror% == 0 then
  fs2:\EFI\q1n1\q1n1-usb-ebs.efi --takeover
endif
type fs2:\EFI\q1n1\{1}.log
fs2:\EFI\Microsoft\Boot\bootmgfw.efi
'@
        $script=$script -f $tag,$stem
    }
    if($Probe -in @('device0','device0-v2','device0-v3','device0-v4')) {
        $manifest=Get-Content (Join-Path $root 'esp-install.json') -Raw | ConvertFrom-Json
        $serial=@($manifest.Files | Where-Object Path -eq '\EFI\q1n1\q1n1-usb-serial-v2.efi')
        if($serial.Count -ne 1 -or (Get-FileHash 'Q:\EFI\q1n1\q1n1-usb-serial-v2.efi').Hash.ToLower() -ne $serial[0].SHA256){throw 'Serial v2 identity changed.'}
        $serialStem='ucsi-'+$Probe+'-serial'
        if(Test-Path ('Q:\EFI\q1n1\'+$serialStem+'.log')){throw 'Prior serial log exists; preserve it first.'}
        $script=@'
@echo -off
fs2:\EFI\q1n1\q1n1-{0}.efi --device0 > fs2:\EFI\q1n1\{1}.log
if %lasterror% == 0 then
  type fs2:\EFI\q1n1\{1}.log
  fs2:\EFI\q1n1\q1n1-usb-serial-v2.efi --device0 > fs2:\EFI\q1n1\{2}.log
endif
type fs2:\EFI\q1n1\{1}.log
fs2:\EFI\Microsoft\Boot\bootmgfw.efi
'@
        $script=$script -f $tag,$stem,$serialStem
    }
    [IO.File]::WriteAllText('Q:\startup.nsh',($script -replace "`r?`n","`r`n")+"`r`n",[Text.Encoding]::ASCII)
    [ordered]@{StartupPath='\startup.nsh';StartupSHA256=(Get-FileHash 'Q:\startup.nsh').Hash.ToLower();
        ProbeSHA256=$probeHash;LogPath=('\EFI\q1n1\'+$stem+'.log');
        FilesystemMapping='fs2: previously confirmed on A16 NVMe';CreatedUtc=[DateTime]::UtcNow.ToString('o')} |
        ConvertTo-Json | Set-Content (Join-Path $root ($stem+'.json'))
} finally {mountvol.exe Q: /D}
& (Join-Path $root 'boot-a16-shell-once.ps1')
