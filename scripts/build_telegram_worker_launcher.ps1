param(
    [string]$OutputPath = "$(Split-Path -Parent $PSScriptRoot)\Start Telegram Worker.exe"
)

$repository = Split-Path -Parent $PSScriptRoot
$sourcePath = Join-Path $repository 'tools\TelegramWorkerLauncher.cs'

if (-not (Test-Path -LiteralPath $sourcePath)) {
    throw "Launcher source was not found: $sourcePath"
}

if (Test-Path -LiteralPath $OutputPath) {
    Remove-Item -LiteralPath $OutputPath -Force
}

Add-Type `
    -Path $sourcePath `
    -ReferencedAssemblies @('System.dll', 'System.Core.dll', 'System.Management.dll', 'System.Windows.Forms.dll') `
    -OutputAssembly $OutputPath `
    -OutputType WindowsApplication

Write-Output "Built: $OutputPath"
