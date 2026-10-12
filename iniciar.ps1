param([switch]$Diagnostico, [switch]$SinInstalar, [switch]$SinNavegador, [int]$Puerto = 8000)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$selectedPython = $null
$pythonPrefix = @()

function Test-Python([string]$candidate, [string[]]$prefix = @()) {
    if (-not $candidate -or $candidate -like '*\Microsoft\WindowsApps\*') { return $false }
    try {
        & $candidate @prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

try {
    if ($Puerto -lt 1 -or $Puerto -gt 65535) { throw 'El puerto debe estar entre 1 y 65535.' }
    $venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if ((Test-Path -LiteralPath $venvPython) -and (Test-Python $venvPython)) {
        $selectedPython = $venvPython
    }
    if (-not $selectedPython) {
        foreach ($command in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
            if (Test-Python $command.Source) { $selectedPython = $command.Source; break }
        }
    }
    if (-not $selectedPython) {
        $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($pyLauncher -and (Test-Python $pyLauncher.Source @('-3'))) {
            $selectedPython = $pyLauncher.Source
            $pythonPrefix = @('-3')
        }
    }
    if (-not $selectedPython) {
        $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if ((Test-Path -LiteralPath $bundledPython) -and (Test-Python $bundledPython)) {
            $selectedPython = $bundledPython
        }
    }
    if (-not $selectedPython) {
        throw 'No se encontro Python 3.10+ real. El acceso de Microsoft Store no basta. Instala Python desde https://www.python.org/downloads/windows/ y marca Add Python to PATH.'
    }
    Write-Host "Python detectado: $selectedPython"
    if ($Diagnostico) {
        & $selectedPython @pythonPrefix 'arrancar.py' '--check' '--port' $Puerto
        exit $LASTEXITCODE
    }
    if (-not $SinInstalar) {
        if ($selectedPython -ne $venvPython) {
            Write-Host 'Creando el entorno del proyecto...'
            & $selectedPython @pythonPrefix -m venv '.venv'
            if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear .venv. Ejecuta diagnostico.bat y revisa permisos/Python.' }
            $selectedPython = $venvPython
            $pythonPrefix = @()
        }
        Write-Host 'Comprobando e instalando dependencias...'
        & $selectedPython @pythonPrefix -m pip install --disable-pip-version-check -r 'requirements.txt'
        if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar las dependencias. Revisa el error de pip y tu conexion.' }
    }
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot '.browsers'
    if (-not $SinInstalar) {
        Write-Host 'Comprobando Chromium para Google Maps sin API...'
        & $selectedPython @pythonPrefix -m playwright install chromium
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'Chromium no se pudo instalar. El panel se abrira; Maps sin API requerira instalarlo despues.' -ForegroundColor Yellow
        }
    }
    $startArgs = @('arrancar.py', '--port', "$Puerto")
    if ($SinNavegador) { $startArgs += '--no-browser' }
    & $selectedPython @pythonPrefix @startArgs
    exit $LASTEXITCODE
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
