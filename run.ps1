param([Parameter(ValueFromRemainingArguments = $true)][string[]]$BotArgs)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$runtimePython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$venvPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (Test-Path -LiteralPath $venvPython) { $taskPython = $venvPython }
elseif (Test-Path -LiteralPath $runtimePython) { $taskPython = $runtimePython }
else { $taskPython = (Get-Command python -ErrorAction Stop).Source }
if ($BotArgs.Count -eq 0) { $BotArgs = @('demo') }
if ($BotArgs[0] -eq 'test') {
    & $taskPython -m unittest discover -s tests -v
} elseif ($BotArgs[0] -eq 'evaluate') {
    $evaluationArgs = @($BotArgs | Select-Object -Skip 1)
    & $taskPython -m hotel_bot.evaluate @evaluationArgs
} else {
    & $taskPython -m hotel_bot @BotArgs
}
exit $LASTEXITCODE
