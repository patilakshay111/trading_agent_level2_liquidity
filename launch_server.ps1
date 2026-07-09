$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "C:\Users\akpatil\AppData\Local\Programs\Python\Python313\python.exe"

if (-not (Test-Path $python)) {
    $python = "python"
}

Set-Location $projectRoot
& $python "$projectRoot\run_with_log.py"
