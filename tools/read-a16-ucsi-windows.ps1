# Read VERSION/CCI and optionally the DSDT's two control-register values.
# No UCSI commands, role requests, resets, or registry writes.
param([string]$OutputPath=(Join-Path $PSScriptRoot 'ucsi-register-read.json'),[switch]$IncludeControlState)
$ErrorActionPreference='Stop'
$driver='C:\Windows\system32\DriverStore\FileRepository\qcpmicglink8480.inf_arm64_699465a2a07dd7a4\qcpmicglink8480.sys'
$expected='2c7161093c74a830569cf6897a141cb5a0dffa2e4a2f155d2b123413e4f9a95e'
if((Get-FileHash -LiteralPath $driver -Algorithm SHA256).Hash.ToLower() -ne $expected){throw 'PMIC driver fingerprint differs from inspected build.'}
if((Get-CimInstance Win32_ComputerSystem).Model -notlike '*UX3607OA*'){throw 'Unexpected machine.'}
$device='\\?\ACPI#QCOM0F8E#2&daba3ff&0#{bc8ef524-7dd5-4b44-8846-078f3f43febc}'
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class A16UcsiRead {
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 static extern SafeFileHandle CreateFile(string name,uint access,uint share,IntPtr security,uint creation,uint flags,IntPtr template);
 [DllImport("kernel32.dll",SetLastError=true)]
 static extern bool DeviceIoControl(SafeFileHandle handle,uint code,byte[] input,uint inputSize,byte[] output,uint outputSize,out uint returned,IntPtr overlapped);
 public static string Read(string device,uint register) {
  if(register!=0x20100 && register!=0x20104 && register!=0x100 && register!=0x180)
   throw new ArgumentException("Register is not allowlisted");
  using(var h=CreateFile(device,0x80000000,3,IntPtr.Zero,3,0,IntPtr.Zero)) {
   if(h.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error(),"Open inspected PMIC interface");
   byte[] input=new byte[8],output=new byte[1024];
   Array.Copy(BitConverter.GetBytes(register),0,input,0,4);
   Array.Copy(BitConverter.GetBytes((uint)4),0,input,4,4);
   uint n;
   bool ok=DeviceIoControl(h,0x80332050,input,8,output,1024,out n,IntPtr.Zero);
   int error=Marshal.GetLastWin32Error();
   if(!ok) throw new Win32Exception(error,"Inspected GIO read IOCTL");
   if(n!=4) throw new InvalidOperationException("Unexpected returned byte count: "+n);
   return "0x"+BitConverter.ToUInt32(output,0).ToString("x8");
  }
 }
}
'@
$version=[A16UcsiRead]::Read($device,0x20100)
$cci=[A16UcsiRead]::Read($device,0x20104)
$report=[ordered]@{CapturedUtc=[DateTime]::UtcNow.ToString('o');DriverSHA256=$expected;
 Version=$version;CCI=$cci;NoRoleOrUcsiCommandSent=$true}
if($IncludeControlState) {
 $controls=@()
 foreach($register in @(0x100,0x180)) {
  try {
   $value=[A16UcsiRead]::Read($device,$register)
   $controls += [ordered]@{Register=('0x{0:x}' -f $register);ReadSucceeded=$true;RawValue=$value}
  } catch {
   $controls += [ordered]@{Register=('0x{0:x}' -f $register);ReadSucceeded=$false;Error=$_.Exception.Message}
  }
 }
 $report.ControlRegisters=$controls
}
$report | ConvertTo-Json | Set-Content -LiteralPath $OutputPath
$report | ConvertTo-Json -Compress
