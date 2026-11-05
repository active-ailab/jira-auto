# Run elevated PowerShell on Windows. Re-run after WSL IP changes (NAT mode).
[CmdletBinding(SupportsShouldProcess=$true)]
param(
  [string]$Distro = 'Ubuntu-24.04',
  [Parameter(Mandatory=$true)][string]$ListenAddress,
  [ValidateSet('Nat','Mirrored')][string]$Mode = 'Nat'
)
$ErrorActionPreference = 'Stop'
$ip = [System.Net.IPAddress]::Parse($ListenAddress)
if ($ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or $ListenAddress -eq '0.0.0.0') { throw 'Provide the Windows LAN IPv4 address' }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run elevated PowerShell' }
$lanInterface = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $ListenAddress -ErrorAction Stop | Select-Object -First 1
if (-not $lanInterface) { throw 'ListenAddress must belong to a Windows network interface' }
$addressBytes = $ip.GetAddressBytes()
$networkBytes = for ($index = 0; $index -lt 4; $index++) {
  $bits = [Math]::Max(0, [Math]::Min(8, $lanInterface.PrefixLength - $index * 8))
  $mask = [int](256 - [Math]::Pow(2, 8 - $bits))
  $addressBytes[$index] -band $mask
}
$lanCidr = ($networkBytes -join '.') + '/' + $lanInterface.PrefixLength
if ($Mode -eq 'Nat') {
  $route = (& wsl.exe -d $Distro -- ip -4 route get 1.1.1.1 | Out-String)
  if ($LASTEXITCODE -ne 0 -or $route -notmatch 'src\s+(\d+\.\d+\.\d+\.\d+)') { throw 'Cannot determine WSL IPv4 address' }
  $wslAddress = $Matches[1]
  if ($PSCmdlet.ShouldProcess("${ListenAddress}:8791", "Set NAT forwarding to ${wslAddress}:8791")) {
    $existingProxy = & netsh interface portproxy show v4tov4
    $pattern = '^\s*' + [regex]::Escape($ListenAddress) + '\s+8791\s+'
    $verb = if ($existingProxy | Select-String -Pattern $pattern) { 'set' } else { 'add' }
    & netsh interface portproxy $verb v4tov4 listenport=8791 listenaddress=$ListenAddress connectport=8791 connectaddress=$wslAddress
    if ($LASTEXITCODE -ne 0) { throw 'portproxy failed' }
  }
} else {
  if ($PSCmdlet.ShouldProcess('WSL Hyper-V firewall', 'Allow LAN TCP 8791')) {
    $name = 'JiraAuto8791-WSL'
    $existing = Get-NetFirewallHyperVRule -Name $name -ErrorAction SilentlyContinue
    if ($existing) {
      Set-NetFirewallHyperVRule -Name $name -RemoteAddresses $lanCidr -LocalPorts 8791 -Action Allow -Enabled True
    } else {
      New-NetFirewallHyperVRule -Name $name -DisplayName 'Jira Auto WSL TCP 8791' -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 8791 -RemoteAddresses $lanCidr -Action Allow
    }
  }
}
if ($PSCmdlet.ShouldProcess('Windows firewall', 'Allow Private/Domain LAN TCP 8791')) {
  if (Get-NetFirewallRule -Name 'JiraAuto8791' -ErrorAction SilentlyContinue) {
    Set-NetFirewallRule -Name 'JiraAuto8791' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8791 -LocalAddress $ListenAddress -RemoteAddress $lanCidr -Profile Private,Domain -Enabled True
  } else {
    New-NetFirewallRule -Name 'JiraAuto8791' -DisplayName 'Jira Auto TCP 8791' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8791 -LocalAddress $ListenAddress -RemoteAddress $lanCidr -Profile Private,Domain
  }
}
Write-Host "Dashboard: http://${ListenAddress}:8791 (also add this host in JA_ALLOWED_HOSTS)"
