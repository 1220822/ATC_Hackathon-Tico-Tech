[CmdletBinding()]
param(
    [switch]$Reset,       # Faz reset aos sensores e alarmes do simulador antes de arrancar
    [switch]$Open,        # Abre o browser automaticamente na UI 2D e 3D
    [switch]$DryRun,      # Força DRY_RUN=true se pretendido (por omissão é false em dev)
    [switch]$NoAgents     # Arranca apenas a API sem os agentes A2A
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  ATC Hackathon - Tico Tech Dev Launcher " -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# 1. Libertar portas 8000 e 9100..9103 se estiverem presas
$ports = @(8000, 9100, 9101, 9102, 9103)
foreach ($port in $ports) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
        if ($conns) {
            $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
            foreach ($procId in $pids) {
                if ($procId -gt 0) {
                    Write-Host "A terminar processo $procId que ocupava a porta $port..." -ForegroundColor Yellow
                    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                }
            }
        }
    } catch {}
}

# 2. Localizar Python no .venv
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Erro: Ambiente virtual não encontrado em .venv\Scripts\python.exe!" -ForegroundColor Red
    exit 1
}

# 3. Se pedido -Reset, repõe o estado do simulador
if ($Reset) {
    Write-Host "A repor o estado inicial do simulador (reset)..." -ForegroundColor Magenta
    & $python (Join-Path $root "scripts\simulate.py") reset
}

# 4. Ajustar variáveis de ambiente específicas
if ($DryRun) {
    $env:DRY_RUN = "true"
    Write-Host "Modo DRY_RUN ativado." -ForegroundColor Yellow
} else {
    $env:DRY_RUN = "false"
}

# 5. Abrir browser se solicitado (-Open)
if ($Open) {
    Start-Job -ScriptBlock {
        Start-Sleep -Seconds 3
        Start-Process "http://127.0.0.1:8000/"
    } | Out-Null
}

# 6. Arrancar o servidor
$apiArgs = @("-m", "src.api", "--sim")
if (-not $NoAgents) {
    $apiArgs += "--agents"
}

Write-Host "A arrancar servidor: python $($apiArgs -join ' ')" -ForegroundColor Green
Write-Host "UI: http://127.0.0.1:8000/  |  3D: http://127.0.0.1:8000/twin" -ForegroundColor Cyan
Write-Host "Pressiona Ctrl+C para parar." -ForegroundColor Gray
Write-Host "-----------------------------------------"

Set-Location $root
& $python $apiArgs

