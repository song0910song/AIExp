$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot = Split-Path -Parent $scriptRoot
Set-Location -LiteralPath $repoRoot
$env:LIGHTING_RAG_BACKEND = "local"
$env:LIGHTING_EMBEDDING_LOCAL_FILES_ONLY = "true"
$env:UV_CACHE_DIR = Join-Path $repoRoot ".uv-cache"
uv run pytest (Join-Path $repoRoot "tests") -q --basetemp (Join-Path $repoRoot ".pytest-basetemp")
