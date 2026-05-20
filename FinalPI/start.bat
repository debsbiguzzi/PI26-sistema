@echo off
title Dashboard Economico
echo.
echo  Dashboard Economico Brasileiro
echo  ================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERRO] Python nao encontrado. Instale em: https://python.org/downloads
    pause & exit /b 1
)

echo  Verificando dependencias...
pip install -r requirements.txt -q
if errorlevel 1 ( echo  [ERRO] pip falhou. & pause & exit /b 1 )

if not exist .env (
    python -c "open('.env','w',encoding='utf-8').write('# Dashboard Economico\n# ANTHROPIC_API_KEY=sk-ant-...\n# DATABASE_URL=postgresql://postgres:senha@localhost:5432/econ\n')"
    echo  [INFO] Arquivo .env criado.
)

echo  Abrindo http://localhost:8000 ...
echo  Pressione Ctrl+C para parar.
echo.
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
pause
