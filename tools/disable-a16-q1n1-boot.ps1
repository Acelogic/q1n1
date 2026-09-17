# Remove the persistent q1n1 boot-loop script, so the q1n1 shell entry returns
# to whatever the one-time probe scripts expect. The payload and the firmware
# entry are left in place; -RemovePayload also deletes \EFI\q1n1\q1n1-usb.efi.
param([switch]$RemovePayload)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$record=Get-Content (Join-Path $root 'q1n1-boot.json') -Raw | ConvertFrom-Json
if($env:COMPUTERNAME -ne 'MCRUZ-ASUS' -or $record.StartupPath -ne '\startup.nsh'){throw 'Unexpected installation record.'}
function Hash([string]$Path){(Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()}
if(Test-Path 'Q:\'){throw 'Q drive busy.'}
& mountvol.exe Q: /S
if($LASTEXITCODE -ne 0){throw 'Could not mount ESP.'}
$removedScript=$false
$removedPayload=$false
try {
    $startup='Q:\startup.nsh'
    if(Test-Path $startup){
        if((Hash $startup) -ne $record.StartupSHA256){throw 'Boot script changed; preserving it.'}
        Remove-Item -LiteralPath $startup
        $removedScript=$true
    }
    if($RemovePayload){
        $payload='Q:'+$record.Payload
        if(Test-Path $payload){
            if((Hash $payload) -ne $record.PayloadSHA256){throw 'Installed payload changed; preserving it.'}
            Remove-Item -LiteralPath $payload
            $removedPayload=$true
            $recordPath=Join-Path $root 'esp-install.json'
            $esp=Get-Content $recordPath -Raw | ConvertFrom-Json
            $esp.Files=@($esp.Files | Where-Object Path -ne $record.Payload)
            $esp | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $recordPath
        }
    }
} finally {& mountvol.exe Q: /D | Out-Null}
$record.State='disabled'
$record | Add-Member -NotePropertyName DisabledUtc -NotePropertyValue ([DateTime]::UtcNow.ToString('o')) -Force
$record | ConvertTo-Json | Set-Content (Join-Path $root 'q1n1-boot.json')
[ordered]@{ScriptRemoved=$removedScript;PayloadRemoved=$removedPayload} | ConvertTo-Json -Compress
