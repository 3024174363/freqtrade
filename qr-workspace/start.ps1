$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python scripts/initialize.py
    if ($LASTEXITCODE -ne 0) { throw 'Configuration initialization failed.' }
    docker compose build
    if ($LASTEXITCODE -ne 0) { throw 'Docker build failed.' }
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw 'Docker startup failed. Inspect docker compose logs.' }
    Write-Host 'QR frontend: http://127.0.0.1:5173'
    Write-Host 'QR API:      http://127.0.0.1:8090/api/v1'
    Write-Host 'QR API docs: http://127.0.0.1:8090/docs'
    Write-Host 'Local login: user_data/local-access.txt'
} finally { Pop-Location }
