[CmdletBinding()]
param()

Write-Host "A libertar portas da aplicacao (8000, 9100..9103)..." -ForegroundColor Yellow
$ports = @(8000, 9100, 9101, 9102, 9103)
foreach ($port in $ports) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
        if ($conns) {
            $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
            foreach ($procId in $pids) {
                if ($procId -gt 0) {
                    Write-Host "  -> Porta $port: a terminar processo $procId..." -ForegroundColor Red
                    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                }
            }
        }
    } catch {}
}
Write-Host "Portas libertadas com sucesso!" -ForegroundColor Green

