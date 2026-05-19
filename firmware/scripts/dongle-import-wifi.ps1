# dongle-import-wifi.ps1 — push the host's current Wi-Fi SSID + PSK to a
# plugged-in R00K dongle. Uses only stock PowerShell + netsh; no installs.
#
# Usage:
#   pwsh ./dongle-import-wifi.ps1
#   pwsh ./dongle-import-wifi.ps1 -Port COM4 -Priority 1
#
# Run from the user account that joined the SSID (so `netsh wlan ... key=clear`
# can read the password). No admin rights required.

[CmdletBinding()]
param(
    [string]$Port,
    [int]$Priority = 2,
    [string]$Ssid,
    [string]$Psk
)

function Find-DonglePort {
    # Prefer a COM port whose USB IDs match Rook (VID_1209 PID_0001).
    $pnp = Get-PnpDevice -Class Ports -ErrorAction SilentlyContinue
    foreach ($d in $pnp) {
        if ($d.InstanceId -match 'VID_1209&PID_0001') {
            $com = $null
            if ($d.FriendlyName -match '\((COM\d+)\)') { $com = $Matches[1] }
            if ($com) { return $com }
        }
    }
    # Fall back to the first available COM port.
    $ports = [System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object
    return $ports | Select-Object -First 1
}

function Read-HostWifi {
    $iface = & netsh wlan show interfaces
    $ssid  = ($iface | Select-String -Pattern '^\s*SSID\s*:\s+(.+)$' |
              ForEach-Object { $_.Matches.Groups[1].Value.Trim() } |
              Select-Object -First 1)
    if (-not $ssid) { return $null }
    $profile = & netsh wlan show profile name="$ssid" key=clear
    $psk = ($profile | Select-String -Pattern '^\s*Key Content\s*:\s+(.+)$' |
            ForEach-Object { $_.Matches.Groups[1].Value.Trim() } |
            Select-Object -First 1)
    if (-not $psk) { return $null }
    return @{ Ssid = $ssid; Psk = $psk }
}

if (-not $Port)  { $Port = Find-DonglePort }
if (-not $Port)  { throw "No COM port found — plug the dongle in or pass -Port." }

if (-not $Ssid -or -not $Psk) {
    $creds = Read-HostWifi
    if (-not $creds) { throw "Could not read host Wi-Fi credentials." }
    $Ssid = $creds.Ssid
    $Psk  = $creds.Psk
}

Write-Host ("host wifi : ssid='{0}'  psk=*** ({1} chars)" -f $Ssid, $Psk.Length)
Write-Host ("target    : {0}  (priority {1})" -f $Port, $Priority)

$sp = New-Object System.IO.Ports.SerialPort $Port,115200,'None',8,'One'
$sp.DtrEnable = $true
$sp.RtsEnable = $true
$sp.NewLine   = "`r`n"
$sp.ReadTimeout  = 500
$sp.WriteTimeout = 1000
$sp.Open()
try {
    Start-Sleep -Milliseconds 300
    $sp.WriteLine("")
    Start-Sleep -Milliseconds 300
    $sp.DiscardInBuffer()

    foreach ($c in @(
        "wifi rm $Ssid",
        "wifi add $Ssid $Psk $Priority",
        "wifi reconnect"
    )) {
        $sp.WriteLine($c)
        Start-Sleep -Milliseconds 400
    }

    # Drain replies for ~8s (covers scan + connect), then `status`.
    $end = (Get-Date).AddSeconds(8)
    $buf = ""
    while ((Get-Date) -lt $end) {
        try { $buf += $sp.ReadExisting() } catch {}
        Start-Sleep -Milliseconds 100
    }
    $sp.WriteLine("status")
    Start-Sleep -Milliseconds 1500
    try { $buf += $sp.ReadExisting() } catch {}

    Write-Host "--- dongle reply ---"
    Write-Host $buf.TrimEnd()
    Write-Host "--------------------"
    if ($buf -match ("connected ssid={0}" -f [Regex]::Escape($Ssid))) {
        Write-Host ("OK: dongle connected to '{0}'." -f $Ssid)
    } else {
        Write-Host "note: dongle did not immediately confirm. The save took;"
        Write-Host "the background monitor will retry within ~45 seconds."
    }
} finally {
    $sp.Close()
}
