#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  Dashboard Econômico Brasileiro — Start Script (Linux / macOS)
#  Usage:  ./start.sh
# ════════════════════════════════════════════════════════════════
set -e

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Dashboard Econômico Brasileiro         ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""

# ── Check Python ─────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "  [ERRO] Python3 não encontrado."
    echo "  Ubuntu/Debian: sudo apt install python3 python3-pip"
    echo "  macOS:         brew install python"
    exit 1
fi

# ── Install dependencies ──────────────────────────────────────────
echo "  Verificando dependências..."
pip3 install -r requirements.txt -q

# ── Create .env if it doesn't exist ──────────────────────────────
if [ ! -f .env ]; then
    cat > .env <<'EOF'
# Dashboard Econômico — Configuração

# Chave da API Anthropic (obtenha em console.anthropic.com)
# ANTHROPIC_API_KEY=sk-ant-...

# Banco de dados (padrão: SQLite — não precisa configurar)
# DATABASE_URL=postgresql://postgres:senha@localhost:5432/econ_dashboard
EOF
    echo "  [INFO] Arquivo .env criado. Edite-o para adicionar sua chave Anthropic."
    echo "         As projeções IA ficam desativadas sem a chave."
    echo ""
fi

# ── Start server ──────────────────────────────────────────────────
echo "  Iniciando servidor..."
echo "  Acesse: http://localhost:8000"
echo "  Pressione Ctrl+C para parar."
echo ""
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
