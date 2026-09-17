# Remove only the recorded q1n1 entry and files, retaining Windows and partitions.
$ErrorActionPreference='Stop'
$record=Get-Content (Join-Path $PSScriptRoot 'esp-install.json') -Raw | ConvertFrom-Json
if($record.State -ne 'installed' -or $record.BootEntry -notmatch '^\{[0-9a-fA-F-]{36}\}$'){
    throw 'Expected a completed q1n1 installation record.'
}
if(Test-Path 'Q:\'){throw 'Q: is already in use.'}
$mounted=$false
try {
    & mountvol.exe Q: /S
    if($LASTEXITCODE -ne 0){throw 'Could not mount ESP.'}
    $mounted=$true
    $part=Get-Partition -DiskNumber 0 -PartitionNumber 12
    $mountedVolume=((& mountvol.exe Q: /L | Out-String).Trim())
    if($LASTEXITCODE -ne 0 -or $part.AccessPaths -notcontains $mountedVolume -or
       $part.GptType -ne '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}') {throw 'ESP identity mismatch.'}
    foreach($file in $record.Files){
        if($file.Path -notmatch '^\\EFI\\q1n1\\[^\\]+\.efi$' -and $file.Path -ne '\tcblaunch.exe'){
            throw 'Unexpected path in install record.'
        }
        if((Get-FileHash -LiteralPath "Q:$($file.Path)" -Algorithm SHA256).Hash.ToLower() -ne $file.SHA256){
            throw "File changed; inspect before removing: $($file.Path)"
        }
    }
    & bcdedit.exe /delete $record.BootEntry
    if($LASTEXITCODE -ne 0){throw 'Boot entry removal failed.'}
    foreach($file in $record.Files){Remove-Item -LiteralPath "Q:$($file.Path)"}
    if(!(Get-ChildItem 'Q:\EFI\q1n1')){Remove-Item -LiteralPath 'Q:\EFI\q1n1'}
    $record.State='removed'
    $record | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $PSScriptRoot 'esp-install.json')
} finally {
    if($mounted){& mountvol.exe Q: /D | Out-Null}
}
