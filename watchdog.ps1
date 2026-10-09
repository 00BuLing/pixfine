$ErrorActionPreference = "Stop"

$workerTask = "TerlnImageOptimizerWorker"
$billingTask = "TerlnImageOptimizerBilling"
$healthURL = "http://127.0.0.1:8791/health"
$workerHealthURL = "http://127.0.0.1:8789/health"

for ($attempt = 1; $attempt -le 2; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri $healthURL -TimeoutSec 5
        if ($health.worker -eq $true -and $health.billing -eq $true) {
            exit 0
        }
    } catch {}
    if ($attempt -eq 1) {
        Start-Sleep -Seconds 10
    }
}

$workerHealthy = $false
try {
    $workerHealth = Invoke-RestMethod -Uri $workerHealthURL -TimeoutSec 5
    $workerHealthy = ($workerHealth.status -eq "ok")
} catch {}

if (-not $workerHealthy) {
    $worker = Get-ScheduledTask -TaskName $workerTask -ErrorAction SilentlyContinue
    if ($null -ne $worker) {
        Stop-ScheduledTask -TaskName $workerTask -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $workerTask
    }
}

$billing = Get-ScheduledTask -TaskName $billingTask -ErrorAction SilentlyContinue
if ($null -ne $billing) {
    Stop-ScheduledTask -TaskName $billingTask -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $billingTask
}
