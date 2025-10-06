# Run in an elevated PowerShell (Run as Administrator)
$registryPath = "HKLM:\Software\Policies\Microsoft\Windows\Kernel DMA Protection"
$valueName = "DeviceEnumerationPolicy"

if (-not (Test-Path $registryPath)) {
    New-Item -Path $registryPath -Force | Out-Null
}
New-ItemProperty -Path $registryPath -Name $valueName -Value 2 -PropertyType DWord -Force | Out-Null
Write-Host "Set DeviceEnumerationPolicy=2. Reboot required."
