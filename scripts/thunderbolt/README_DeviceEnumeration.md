# Device Enumeration Policy Scripts

These scripts control whether Windows blocks certain devices before user logon
(Code 55: "This device is blocked from starting while the user is not logged in").

## Scripts

- **Enable-DeviceEnumerationBeforeLogon.ps1**  
  Allows devices to start even if no one is physically logged in.  
  This sets the registry key:  
    HKLM\SOFTWARE\Policies\Microsoft\Windows\Kernel DMA Protection\DeviceEnumerationPolicy = 2


- **Undo-DeviceEnumerationPolicy.ps1**  
Removes the registry value, reverting to Windows’ default behavior.

## Usage

1. Open PowerShell **as Administrator**.
2. Navigate to the folder: 
    ```powershell:
    cd ~
    cd <project_directory>\pirt-camera-control\
    ```

3. Run the desired script:
    ```powershell
    powershell -ExecutionPolicy Bypass -File .\scripts\thunderbolt\Enable-DeviceEnumerationBeforeLogon.ps1

    or

    ```powershell:
    powershell -ExecutionPolicy Bypass -File .\scripts\thunderbolt\Undo-DeviceEnumerationPolicy.ps1
    ```
4. Reboot the machine for the changes to take effect.

⚠️ Note: This disables a DMA protection measure in Windows.
Only use if you understand the security implications.



