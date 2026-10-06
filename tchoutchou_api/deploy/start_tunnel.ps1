# Starts a Cloudflare quick tunnel to the local API and records the public URL in
# deploy\tunnel_url.txt. Needs `cloudflared` on PATH (see README_TUNNEL.md).
#
# The URL changes every time the tunnel restarts -- re-enter it in the app's Settings.
param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
  Write-Error "cloudflared not found. Install it first:  winget install --id Cloudflare.cloudflared"
  exit 1
}

$log = Join-Path $PSScriptRoot 'tunnel.log'
$urlFile = Join-Path $PSScriptRoot 'tunnel_url.txt'
Remove-Item $log, $urlFile -ErrorAction SilentlyContinue

$proc = Start-Process cloudflared -ArgumentList @('tunnel', '--url', "http://localhost:$Port", '--logfile', $log, '--no-autoupdate') -PassThru -NoNewWindow

$url = $null
for ($i = 0; $i -lt 90 -and -not $url; $i++) {
  Start-Sleep -Seconds 1
  if ($proc.HasExited) { Write-Error "cloudflared exited early (code $($proc.ExitCode)). See $log"; exit 1 }
  if (Test-Path $log) {
    $m = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $url = $m.Matches[0].Value }
  }
}
if (-not $url) { Write-Error "No tunnel URL after 90 s. See $log"; exit 1 }

Set-Content -Path $urlFile -Value $url -Encoding ascii
Write-Host ""
Write-Host "Tunnel is up:  $url" -ForegroundColor Green
Write-Host "Saved to:      $urlFile"
Write-Host "Check it:      $url/api/health"
Write-Host "In the app:    Settings -> Server address -> $url"
Write-Host ""
$proc.WaitForExit()
