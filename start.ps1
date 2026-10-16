param(
    [switch]$Setup,
    [switch]$EvidenceOnly,
    [switch]$PrepareSynthetic,
    [ValidateRange(1, 65535)][int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:UV_CACHE_DIR = Join-Path $PSScriptRoot '.uv-cache'
$runtimePath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$savedFlags = @{}
foreach ($flag in @('LLM_PROVIDER','EMBEDDER','WARMUP')) { $savedFlags[$flag] = [Environment]::GetEnvironmentVariable($flag, 'Process') }
try {
if ($Setup) {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv, then run .\start.ps1 -Setup again.' }
    if (-not (Test-Path -LiteralPath $runtimePath)) {
        uv venv --python 3.12 .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    uv pip install --python $runtimePath -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
if (-not (Test-Path -LiteralPath $runtimePath)) { throw 'Run .\start.ps1 -Setup first.' }
if ($EvidenceOnly) {
    $env:LLM_PROVIDER = 'none'
    $env:EMBEDDER = 'hash'
    $env:WARMUP = '0'
    Write-Host 'Evidence-only mode: generated answers are unavailable; inventory and review work.'
}
if (-not $EvidenceOnly) {
    & $runtimePath -m scripts.start_ollama
    if ($LASTEXITCODE -ne 0) { throw 'Local model runtime is unavailable.' }
}
& $runtimePath -m scripts.preflight
if ($LASTEXITCODE -ne 0) { throw 'Resolve the readiness checks above, or use -EvidenceOnly for structural features.' }
& $runtimePath -m scripts.seed
if ($LASTEXITCODE -ne 0) { throw 'Could not initialise the project database.' }
if ($PrepareSynthetic) {
    & $runtimePath -m scripts.ingest_synthetic
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic preparation failed.' }
    & $runtimePath -m scripts.run_checks_all
    if ($LASTEXITCODE -ne 0) { throw 'Consistency checks failed.' }
}
Write-Host "Open http://127.0.0.1:$Port  (Ctrl+C to stop)"
& $runtimePath -m uvicorn app.api:app --host 127.0.0.1 --port $Port
$runExitCode = $LASTEXITCODE
} finally {
    foreach ($flag in $savedFlags.Keys) { [Environment]::SetEnvironmentVariable($flag, $savedFlags[$flag], 'Process') }
}
exit $runExitCode
