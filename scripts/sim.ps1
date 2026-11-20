[CmdletBinding()]
param(
    [Parameter(Position=0, Mandatory=$false)]
    [string]$Action = "intrusao",
    
    [Parameter(Position=1, ValueFromRemainingArguments=$true)]
    [string[]]$ExtraArgs
)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$simScript = Join-Path $root "scripts\simulate.py"

if ($Action -in @("intrusao", "janela", "fumo", "objetivo")) {
    & $python $simScript cenario $Action @ExtraArgs
} else {
    & $python $simScript $Action @ExtraArgs
}

