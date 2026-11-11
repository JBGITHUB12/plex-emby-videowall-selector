<#
.SYNOPSIS
  Launches Google Chrome or Microsoft Edge in borderless fullscreen --kiosk mode for the
  Local Media Plex, Emby & Jellyfin VideoWall Selector (Portrait or Landscape).

.PARAMETER Url
  URL of the running VideoWall server (default: http://127.0.0.1:18500).

.PARAMETER Orientation
  Display orientation to enforce before launching: "Keep" (default), "Portrait", or "Landscape".

.PARAMETER AudioDeviceMatch
  Optional substring (e.g. "HDMI" or "LG" or "SAMSUNG") to set as the default Windows audio output and unmute.

.PARAMETER TouchMonitorMatch
  Optional PnP Monitor InstanceId substring if binding a USB touch overlay digitizer to a specific display.
#>
param(
    [string]$Url = "http://127.0.0.1:18500",
    [ValidateSet("Keep", "Portrait", "Landscape")]
    [string]$Orientation = "Keep",
    [string]$AudioDeviceMatch = "",
    [string]$TouchMonitorMatch = "",
    [string]$ProfileDir = "$PSScriptRoot\..\wall_data\KioskBrowserProfile"
)

$ErrorActionPreference = "Continue"

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public class KioskDisplayAndAudio {
    [DllImport("user32.dll", CharSet=CharSet.Ansi)]
    public static extern int EnumDisplaySettings(string lpszDeviceName, int iModeNum, ref DEVMODE lpDevMode);

    [DllImport("user32.dll", CharSet=CharSet.Ansi)]
    public static extern int ChangeDisplaySettingsEx(string lpszDeviceName, ref DEVMODE lpDevMode, IntPtr hwnd, uint dwflags, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Ansi)]
    public struct DEVMODE {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmDeviceName;
        public short dmSpecVersion; public short dmDriverVersion; public short dmSize; public short dmDriverExtra;
        public int dmFields;
        public int dmPositionX; public int dmPositionY;
        public int dmDisplayOrientation; public int dmDisplayFixedOutput;
        public short dmColor; public short dmDuplex; public short dmYResolution; public short dmTTOption;
        public short dmCollate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmFormName;
        public short dmLogPixels; public short dmBitsPerPel; public int dmPelsWidth; public int dmPelsHeight;
        public int dmDisplayFlags; public int dmDisplayFrequency;
        public int dmICMMethod; public int dmICMIntent; public int dmMediaType; public int dmDitherType;
        public int dmReserved1; public int dmReserved2; public int dmPanningWidth; public int dmPanningHeight;
    }

    public static string SetOrientation(bool wantPortrait) {
        DEVMODE dm = new DEVMODE();
        dm.dmSize = (short)Marshal.SizeOf(dm);
        if (EnumDisplaySettings(null, -1, ref dm) == 0) return "EnumDisplaySettings failed";
        bool isPortrait = dm.dmPelsHeight > dm.dmPelsWidth;
        if (wantPortrait && !isPortrait) {
            int tmp = dm.dmPelsWidth; dm.dmPelsWidth = dm.dmPelsHeight; dm.dmPelsHeight = tmp;
            dm.dmDisplayOrientation = 1; // DMDO_90
            dm.dmFields |= (0x00800000 | 0x00080000 | 0x00100000);
            ChangeDisplaySettingsEx(null, ref dm, IntPtr.Zero, 0x01, IntPtr.Zero);
            return string.Format("Rotated to Portrait {0}x{1}", dm.dmPelsWidth, dm.dmPelsHeight);
        }
        if (!wantPortrait && isPortrait) {
            int tmp = dm.dmPelsWidth; dm.dmPelsWidth = dm.dmPelsHeight; dm.dmPelsHeight = tmp;
            dm.dmDisplayOrientation = 0; // DMDO_DEFAULT
            dm.dmFields |= (0x00800000 | 0x00080000 | 0x00100000);
            ChangeDisplaySettingsEx(null, ref dm, IntPtr.Zero, 0x01, IntPtr.Zero);
            return string.Format("Rotated to Landscape {0}x{1}", dm.dmPelsWidth, dm.dmPelsHeight);
        }
        return string.Format("Display ready ({0}x{1})", dm.dmPelsWidth, dm.dmPelsHeight);
    }

    [ComImport, Guid("568b9108-44bf-40b4-9006-86afe5b5a620"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPolicyConfigVista {
        int GetMixFormat(string a, IntPtr b);
        int GetDeviceFormat(string a, int b, IntPtr c);
        int SetDeviceFormat(string a, IntPtr b, IntPtr c);
        int GetProcessingPeriod(string a, int b, IntPtr c, IntPtr d);
        int SetProcessingPeriod(string a, IntPtr b);
        int GetShareMode(string a, IntPtr b);
        int SetShareMode(string a, IntPtr b);
        int GetPropertyValue(string a, IntPtr b, IntPtr c);
        int SetPropertyValue(string a, IntPtr b, IntPtr c);
        int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string wszDeviceId, int eRole);
        int SetEndpointVisibility(string a, int b);
    }

    [ComImport, Guid("294935CE-F637-4E7C-A41B-AB255460B862")]
    class PolicyConfigVistaClient { }

    public static void SetDefaultAudio(string deviceId) {
        var client = (IPolicyConfigVista)(new PolicyConfigVistaClient());
        client.SetDefaultEndpoint(deviceId, 0);
        client.SetDefaultEndpoint(deviceId, 1);
        client.SetDefaultEndpoint(deviceId, 2);
    }
}
'@

if ($Orientation -eq "Portrait") {
    Write-Host ([KioskDisplayAndAudio]::SetOrientation($true))
} elseif ($Orientation -eq "Landscape") {
    Write-Host ([KioskDisplayAndAudio]::SetOrientation($false))
}

if ($TouchMonitorMatch) {
    $mon = Get-PnpDevice -Class Monitor -Status OK -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -match $TouchMonitorMatch } | Select-Object -First 1
    if ($mon) {
        $monInterface = "\\?\" + ($mon.InstanceId.ToLower() -replace "\\", "#") + "#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
        $digimonKey = "HKLM:\SOFTWARE\Microsoft\Wisp\Pen\Digimon"
        if (Test-Path $digimonKey) {
            $props = Get-ItemProperty -Path $digimonKey
            foreach ($p in $props.PSObject.Properties) {
                if ($p.Name -match "^20-" -and $p.Value -ne $monInterface) {
                    Set-ItemProperty -Path $digimonKey -Name $p.Name -Value $monInterface -Type String
                }
            }
        }
    }
}

if ($AudioDeviceMatch) {
    Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\*\Properties" -ErrorAction SilentlyContinue | ForEach-Object {
        $guid = Split-Path (Split-Path $_.PSPath -Parent) -Leaf
        $state = (Get-ItemProperty (Split-Path $_.PSPath -Parent)).DeviceState
        $friendly = $_."{a45c254e-df1c-4efd-8020-67d146a850e0},2"
        if ($state -eq 1 -and $friendly -match $AudioDeviceMatch) {
            [KioskDisplayAndAudio]::SetDefaultAudio("{0.0.0.00000000}.$guid")
            Write-Host "Switched default audio output to: $friendly"
        }
    }
}

$browserCandidates = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
)
$browserExe = $browserCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browserExe) {
    throw "Could not find Google Chrome or Microsoft Edge."
}

New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null

$browserArgs = @(
    "--kiosk",
    $Url,
    "--user-data-dir=$ProfileDir",
    "--autoplay-policy=no-user-gesture-required",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--disable-features=TranslateUI,InfiniteSessionRestore",
    "--overscroll-history-navigation=0"
)

$proc = Start-Process -FilePath $browserExe -ArgumentList $browserArgs -PassThru
Write-Host "Launched VideoWall Kiosk ($browserExe, PID=$($proc.Id)) -> $Url"
