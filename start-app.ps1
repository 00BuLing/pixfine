$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONUNBUFFERED = "1"
$env:IMAGE_BILLING_SERVICE_API_KEY = (Get-Content "$PSScriptRoot\service-api-key.txt" -Raw).Trim()
$env:IMAGE_OPTIMIZER_API_KEY = (Get-Content "$PSScriptRoot\api-key.txt" -Raw).Trim()
$env:IMAGE_BILLING_PASSWORD_HASH = (Get-Content "$PSScriptRoot\password-hash.txt" -Raw).Trim()
$env:IMAGE_OPTIMIZER_WORKER_URL = "http://127.0.0.1:8789/v1/images/optimize"
$env:IMAGE_OPTIMIZER_MAX_CONCURRENCY = "16"
$env:IMAGE_OPTIMIZER_MAX_QUEUE = "48"
$env:IMAGE_OPTIMIZER_QUEUE_TIMEOUT_SECONDS = "180"
$env:IMAGE_OPTIMIZER_WORKER_TIMEOUT_SECONDS = "180"
$env:IMAGE_OPTIMIZER_HTTP_IO_TIMEOUT_SECONDS = "60"
$env:IMAGE_OPTIMIZER_RESPONSE_WRITE_TIMEOUT_SECONDS = "300"
$env:IMAGE_OPTIMIZER_INGRESS_TIMEOUT_SECONDS = "300"
$env:IMAGE_BILLING_PUBLIC_BASE_URL = "https://image-optimizer.tietiezhi.xyz"
$env:IMAGE_BILLING_PAYMENT_CONFIG = "$PSScriptRoot\payment.json"
$stdoutPath = Join-Path $PSScriptRoot "billing.stdout.log"
$stderrPath = Join-Path $PSScriptRoot "billing.stderr.log"
# Keep native stdout out of the long-running PowerShell 5.1 pipeline.
$process = Start-Process -FilePath "python.exe" -ArgumentList "app.py" -WorkingDirectory $PSScriptRoot -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
exit $process.ExitCode
