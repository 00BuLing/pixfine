$ErrorActionPreference = "Stop"

$serviceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$apiKeyPath = Join-Path $serviceDir "api-key.txt"
if (-not (Test-Path $apiKeyPath)) {
    throw "Missing $apiKeyPath"
}

$env:IMAGE_OPTIMIZER_API_KEY = (Get-Content $apiKeyPath -Raw).Trim()
$env:IMAGE_OPTIMIZER_HOST = "0.0.0.0"
$env:IMAGE_OPTIMIZER_PORT = "8789"
$env:IMAGE_OPTIMIZER_GPU_CONCURRENCY = "2"
$env:IMAGE_OPTIMIZER_GPU_TIMEOUT_SECONDS = "150"
$env:IMAGE_OPTIMIZER_HTTP_IO_TIMEOUT_SECONDS = "60"
$env:REALESRGAN_EXE = "C:\Users\22621\image2-resize-test\realesrgan\realesrgan-ncnn-vulkan.exe"

Set-Location $serviceDir
$stdoutPath = Join-Path $serviceDir "worker.stdout.log"
$stderrPath = Join-Path $serviceDir "worker.stderr.log"
# Keep native stdout out of the long-running PowerShell 5.1 pipeline.
$process = Start-Process -FilePath "python.exe" -ArgumentList "worker.py" -WorkingDirectory $serviceDir -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
exit $process.ExitCode
