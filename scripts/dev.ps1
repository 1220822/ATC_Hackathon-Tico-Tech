# Carrega o .env para esta sessão e abre o VS Code com essas variáveis.
# Fecha TODAS as janelas do VS Code antes de correr, senão a instância já aberta não vê a chave.
# Uso: .\scripts\dev.ps1
$root = Split-Path -Parent $PSScriptRoot
Get-Content (Join-Path $root ".env") | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim('"'), 'Process')
    }
}
code $root
