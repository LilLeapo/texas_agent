<#
Run in an elevated Windows PowerShell window on the robot-control computer.
It forwards Windows-LAN TCP port 5100 to the WSL Ubuntu coordinator.
Run it again after `wsl --shutdown` or a reboot because a NAT-mode WSL IP may change.
#>
param(
    [string]$Distro = "Ubuntu-22.04",
    [int]$Port = 5100
)

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "请用管理员身份打开 PowerShell 后再运行此脚本。"
}

$wslAddresses = (wsl.exe -d $Distro -- hostname -I).Trim().Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)
if ($wslAddresses.Count -eq 0) {
    throw "无法获得 $Distro 的 WSL IP。请先启动 Ubuntu。"
}
$wslIp = $wslAddresses[0]

netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=$Port | Out-Null
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$Port connectaddress=$wslIp connectport=$Port

$ruleName = "Panthera LAN Orchestrator TCP $Port"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port | Out-Null
}

$lanAddresses = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
    Select-Object -ExpandProperty IPAddress -Unique

Write-Host "WSL target: $wslIp`:$Port"
Write-Host "Windows LAN addresses: $($lanAddresses -join ', ')"
Write-Host "Use http://<one Windows LAN address>:$Port on the Agent and hand computers."
