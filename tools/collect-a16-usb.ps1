# Read-only device/driver inventory for A16 USB console bring-up.
# Writes one JSON report; does not enable devices, change roles, or alter boot settings.
param([string]$OutputPath = (Join-Path $PSScriptRoot ('usb-inventory-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '.json')))
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$system = Get-CimInstance Win32_ComputerSystem
if ($system.Model -notlike '*UX3607OA*') { throw 'This collector is intended for the ASUS UX3607OA.' }
$properties = @(
    'DEVPKEY_Device_Parent', 'DEVPKEY_Device_Children',
    'DEVPKEY_Device_LocationPaths', 'DEVPKEY_Device_LocationInfo',
    'DEVPKEY_Device_Service', 'DEVPKEY_Device_DriverInfPath',
    'DEVPKEY_Device_DriverVersion', 'DEVPKEY_Device_MatchingDeviceId',
    'DEVPKEY_Device_BusReportedDeviceDesc', 'DEVPKEY_Device_CompatibleIds',
    'DEVPKEY_Device_ProblemCode'
)
$present = @(Get-PnpDevice -PresentOnly)
$selected = @($present | Where-Object {
    $_.InstanceId -match '^(ACPI\\QCOM0(F8B|F8C|FED|F9D|C6D|F16)|URS\\|USB4\\|USB\\VID_05AC&PID_1905)' -or
    $_.FriendlyName -match 'USB.*(Controller|Root Hub|Type-C|Host Router)|Synopsys|UCSI|USB Function' -or
    $_.Class -eq 'Ports'
})
$devices = foreach ($device in $selected) {
    $values = [ordered]@{}
    foreach ($key in $properties) {
        try {
            $property = Get-PnpDeviceProperty -InstanceId $device.InstanceId -KeyName $key -ErrorAction Stop
            $values[$key] = $property.Data
        } catch { $values[$key] = $null }
    }
    [ordered]@{
        InstanceId = $device.InstanceId; Name = $device.FriendlyName
        Class = $device.Class; Status = $device.Status; Properties = $values
    }
}
$ids = @($selected.InstanceId)
$drivers = @(Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceID -in $ids } |
    Select-Object DeviceName, DeviceID, DriverProviderName, DriverVersion, InfName, IsSigned)
$service = Get-CimInstance Win32_Service -Filter "Name='sshd'"
$firmware = (& bcdedit.exe /enum firmware /v 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) { $firmware = 'Unavailable: run elevated to read firmware boot entries.' }
$report = [ordered]@{
    SchemaVersion = 1; CapturedUtc = [DateTime]::UtcNow.ToString('o')
    Computer = $env:COMPUTERNAME; Model = $system.Model
    SshService = ($service | Select-Object Name, State, StartMode)
    Devices = @($devices); Drivers = $drivers; FirmwareBootEntries = $firmware
    Notes = @('Passive Windows inventory; no roles or hardware settings changed.',
        'Device-mode driver availability does not prove cable routing or post-EBS operation.')
}
$report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
[ordered]@{ Report = $OutputPath; DeviceCount = @($devices).Count; Computer = $env:COMPUTERNAME } | ConvertTo-Json -Compress
