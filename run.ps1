# Launch the full stack — engine, tunnel, Host app — each in its own window.
# See RUNBOOK.md for health checks and troubleshooting.

$root = $PSScriptRoot

# Tunnel domain comes from .env (NGROK_WEBHOOK_BASE_URL), so it lives in one place.
$baseUrl = ((Get-Content "$root\.env" | Where-Object { $_ -match '^NGROK_WEBHOOK_BASE_URL=' }) -split '=', 2)[1].Trim()
$domain = ([uri]$baseUrl).Host

Start-Process pwsh -ArgumentList '-NoExit', '-Command', "Set-Location '$root'; .venv\Scripts\activate; uvicorn app.main:create_app --factory --port 8000"
Start-Process pwsh -ArgumentList '-NoExit', '-Command', "Set-Location '$root'; ngrok http --domain=$domain 8000"
Start-Process pwsh -ArgumentList '-NoExit', '-Command', "Set-Location '$root'; .venv\Scripts\activate; streamlit run host/dashboard.py"

Write-Host "Started: engine (:8000), tunnel ($domain), Streamlit (:8501) - each in its own window."
