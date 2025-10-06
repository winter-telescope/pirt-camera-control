# Run in an elevated PowerShell (Run as Administrator)
$registryPath = "HKLM:\Software\Policies\Microsoft\Windows\Kernel DMA Protection"
$valueName = "DeviceEnumerationPolicy"

if (Test-Path $registryPath) {
    if (Get-ItemProperty -Path $registryPath -Name $valueName -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $registryPath -Name $valueName -ErrorAction SilentlyContinue
        Write-Host "Removed DeviceEnumerationPolicy value."
    } else {
        Write-Host "DeviceEnumerationPolicy value not set; nothing to remove."
    }
    # Optional: clean up empty key
    $props = (Get-Item $registryPath).Property
    if ($props.Count -eq 0) {
        Remove-Item $registryPath -Force -ErrorAction SilentlyContinue
        Write-Host "Removed empty 'Kernel DMA Protection' key."
    }
} else {
    Write-Host "Registry path not found; nothing to undo."
}
Write-Host "Reboot required."
