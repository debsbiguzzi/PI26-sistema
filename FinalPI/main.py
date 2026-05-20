"""
main.py — FastAPI Backend
=========================
Start the server with:
    python start.py          ← recommended (handles everything)
    uvicorn main:app --reload --port 8000  ← manual

Then open:
    http://localhost:8000            ← dashboard (no IP config needed)
    http://localhost:8000/docs       ← interactive API docs

Environment variables (optional — set in .env file):
    ANTHROPIC_API_KEY=sk-ant-...     ← required for AI projections
    DATABASE_URL=postgresql://...    ← optional, defaults to SQLite
    PORT=8000                        ← optional, defaults to 8000
"""

import os
import json
import time
import traceback
import requests
# Anthropic is optional — local forecaster is used by default
try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from forecaster import generate_projections as _local_forecast

from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, Dict

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ── Load .env file if it exists (before anything else) ───────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — use system env vars

from models import (
    get_db, init_db, engine,
    IndexHistory, Projection,
    TIMESCALE_SETUP_SQL, IS_SQLITE
)

# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═════════════════════════════════════════════════════════════════════════════

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BCB_BASE = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados/ultimos/{n}?formato=json"
TIMEOUT  = 15
ULTIMOS  = 12

SERIES = {
    "IPCA":  433,
    "INPC":  188,
    "IGP-M": 189,
    "PIB":   1207,
    "INCC":  192,
}

# Path to the dashboard HTML file (same folder as this script)
BASE_DIR   = Path(__file__).parent
DASH_FILE  = BASE_DIR / "dashboard.html"

# ═════════════════════════════════════════════════════════════════════════════
#  APP SETUP
# ═════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Dashboard Econômico Brasileiro",
    description="""
API que serve dados econômicos do BCB, projeções por IA (Claude) e
informações do Bolsa Família.

Abra o dashboard em: **http://localhost:8000**
    """,
    version="2.0.0",
)

# CORS — allows the bundled dashboard HTML to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═════════════════════════════════════════════════════════════════════════════
#  STARTUP / SHUTDOWN
# ═════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
def on_startup():
    # 1. Create tables
    init_db()

    # 2. Try TimescaleDB (PostgreSQL-only, silently skipped on SQLite)
    if not IS_SQLITE:
        try:
            with engine.connect() as conn:
                conn.execute(text(TIMESCALE_SETUP_SQL))
                conn.commit()
            print("✓ TimescaleDB hypertable active.")
        except Exception:
            pass

    # 3. Auto-sync BCB data if the database is empty
    _maybe_initial_sync()

    # 4. Start scheduler
    _start_scheduler()

    print("\n" + "═" * 55)
    print("  ✓ Dashboard ready →  http://localhost:8000")
    print("  ✓ API docs        →  http://localhost:8000/docs")
    print("═" * 55 + "\n")


@app.on_event("shutdown")
def on_shutdown():
    _scheduler.shutdown(wait=False)


def _maybe_initial_sync():
    """If the DB has no data at all, fetch from BCB automatically on first boot."""
    from models import SessionLocal
    db = SessionLocal()
    try:
        count = db.query(IndexHistory).count()
        if count == 0:
            print("  ℹ Database is empty — fetching initial data from BCB…")
            _fetch_and_save_all()
        else:
            print(f"  ✓ Database has {count} history rows.")
    finally:
        db.close()


# ═════════════════════════════════════════════════════════════════════════════
#  SERVE DASHBOARD HTML
#  This is the key change: FastAPI serves dashboard.html directly.
#  Browser calls  http://localhost:8000  → gets the page
#  Page calls  /api/indices  (relative) → same server, no IP needed
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/", include_in_schema=False)
def serve_dashboard():
    """Serves the dashboard HTML. Open http://localhost:8000 in your browser."""
    if DASH_FILE.exists():
        return FileResponse(str(DASH_FILE), media_type="text/html")
    return HTMLResponse(
        "<h2>dashboard.html not found</h2>"
        "<p>Make sure dashboard.html is in the same folder as main.py.</p>",
        status_code=404,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  HEALTH
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/health", tags=["status"])
def health_check(db: Session = Depends(get_db)):
    """Verifica se o servidor e banco estão no ar."""
    try:
        total_historico = db.query(IndexHistory).count()
        total_projecoes = db.query(Projection).count()
        return {
            "status": "online",
            "database": "sqlite" if IS_SQLITE else "postgresql",
            "indices_history_rows": total_historico,
            "projections_rows": total_projecoes,
            "ai_engine": "claude" if (ANTHROPIC_API_KEY and _ANTHROPIC_AVAILABLE) else "statistical",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  INDICES
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/indices", tags=["indices"])
def get_indices(
    months: int = Query(default=12, ge=1, le=60),
    index_name: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Histórico de índices. Ordenado por data crescente (formato Recharts)."""
    cutoff = date.today() - timedelta(days=months * 31)
    query  = db.query(IndexHistory).filter(IndexHistory.date >= cutoff)

    if index_name:
        if index_name.upper() not in SERIES:
            raise HTTPException(400, f"Índice inválido. Use: {list(SERIES.keys())}")
        query = query.filter(IndexHistory.index_name == index_name.upper())

    rows = query.order_by(IndexHistory.date.asc()).all()
    return [r.to_dict() for r in rows]


@app.get("/api/indices/latest", tags=["indices"])
def get_latest_indices(db: Session = Depends(get_db)):
    """Valor mais recente + delta de cada índice (para cards KPI)."""
    resultado = {}
    for nome in SERIES:
        atual = (
            db.query(IndexHistory)
            .filter(IndexHistory.index_name == nome)
            .order_by(IndexHistory.date.desc())
            .first()
        )
        if not atual:
            resultado[nome] = None
            continue

        anterior = (
            db.query(IndexHistory)
            .filter(IndexHistory.index_name == nome)
            .order_by(IndexHistory.date.desc())
            .offset(1)
            .first()
        )

        delta = None
        trend = "stable"
        if anterior:
            delta = round(atual.value - anterior.value, 4)
            trend = "up" if delta > 0 else ("down" if delta < 0 else "stable")

        resultado[nome] = {
            **atual.to_dict(),
            "previous_value": anterior.value if anterior else None,
            "delta": delta,
            "trend": trend,
        }
    return resultado


@app.post("/api/indices/sync", tags=["indices"])
def sync_indices(background_tasks: BackgroundTasks):
    """Busca dados atualizados do BCB e salva no banco (roda em background)."""
    background_tasks.add_task(_fetch_and_save_all)
    return {
        "message": "Sincronização iniciada",
        "note": "Aguarde ~10s e recarregue o dashboard",
        "timestamp": datetime.utcnow().isoformat(),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  PROJECTIONS
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/projections", tags=["projections"])
def get_projections(db: Session = Depends(get_db)):
    """Projeções mais recentes geradas pelo Claude."""
    ultimo = (
        db.query(Projection.created_at)
        .order_by(Projection.created_at.desc())
        .first()
    )
    if not ultimo:
        return {
            "projections": [], "analysis": None, "risks": [],
            "generated_at": None,
            "message": "Nenhuma projeção. Use POST /api/projections/generate.",
        }

    rows = (
        db.query(Projection)
        .filter(Projection.created_at == ultimo[0])
        .order_by(Projection.projection_month.asc(), Projection.index_name.asc())
        .all()
    )

    analysis   = rows[0].analysis if rows else None
    risks      = json.loads(rows[0].risks) if rows and rows[0].risks else []
    return {
        "projections":  [r.to_dict() for r in rows],
        "analysis":     analysis,
        "risks":        risks,
        "generated_at": ultimo[0].isoformat(),
    }


@app.post("/api/projections/generate", tags=["projections"])
def generate_projections(background_tasks: BackgroundTasks):
    """Pede ao Claude novas projeções para os próximos 6 meses."""
    background_tasks.add_task(_run_projection_job)
    return {
        "message": "Geração iniciada",
        "note": "Aguarde ~20s e recarregue o dashboard",
        "timestamp": datetime.utcnow().isoformat(),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  BOLSA FAMÍLIA
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/bolsa-familia", tags=["bolsa-familia"])
def get_bolsa_familia():
    """Dados do Bolsa Família em Campinas (fonte: Secom-SP, DGSUAS, CadUnico)."""
    return {
        "kpis": {
            "total_beneficiaries":      57757,
            "cadunico_families":       137198,
            "cadunico_people":         316580,
            "renda_campinas_families":  25000,
            "poverty_reduction_pct":       21,
            "reference_month": "Janeiro/2025",
        },
        "regions": [
            {"region": "Sul",      "beneficiaries": 17670, "avg_income": 285.0, "vulnerability": "Alta",       "pct_of_total": 31.0},
            {"region": "Noroeste", "beneficiaries": 13716, "avg_income": 310.0, "vulnerability": "Media-Alta", "pct_of_total": 24.0},
            {"region": "Sudoeste", "beneficiaries": 13146, "avg_income": 295.0, "vulnerability": "Media-Alta", "pct_of_total": 23.0},
            {"region": "Norte",    "beneficiaries":  7993, "avg_income": 340.0, "vulnerability": "Media",      "pct_of_total": 14.0},
            {"region": "Leste",    "beneficiaries":  4560, "avg_income": 390.0, "vulnerability": "Baixa",      "pct_of_total":  8.0},
        ],
        "history": [
            {"year": 2020, "families": 48200},
            {"year": 2021, "families": 61400},
            {"year": 2022, "families": 74863},
            {"year": 2023, "families": 68200},
            {"year": 2024, "families": 57757},
            {"year": 2025, "families": 55100, "is_projection": True},
        ],
        "profile": [
            {"label": "Responsáveis femininos",  "value": 83},
            {"label": "Ensino Médio completo",    "value": 54},
            {"label": "Autônomos (mercado inf.)", "value": 30},
            {"label": "Empregados formais",       "value": 15},
            {"label": "Jovens 'nem-nem'",         "value": 60},
        ],
    }


# ═════════════════════════════════════════════════════════════════════════════
#  BACKGROUND JOBS
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_and_save_all():
    """Busca todos os índices na BCB e persiste no banco."""
    from models import SessionLocal
    db = SessionLocal()
    salvos = atualizados = 0

    try:
        for nome, codigo in SERIES.items():
            url = BCB_BASE.format(codigo=codigo, n=ULTIMOS)
            try:
                resp = requests.get(url, timeout=TIMEOUT)
                resp.raise_for_status()
                raw = resp.json()
            except Exception as e:
                print(f"  ✗ BCB {nome}: {e}")
                continue

            for item in raw:
                data_str  = item.get("data", "")
                valor_str = str(item.get("valor", "0")).replace(",", ".")
                try:
                    dia, mes, ano = data_str.split("/")
                    data_obj = date(int(ano), int(mes), int(dia))
                    valor    = float(valor_str)
                except Exception:
                    continue

                existente = db.query(IndexHistory).filter(
                    IndexHistory.date == data_obj,
                    IndexHistory.index_name == nome,
                ).first()

                if existente:
                    existente.value      = valor
                    existente.fetched_at = datetime.utcnow()
                    atualizados += 1
                else:
                    db.add(IndexHistory(date=data_obj, index_name=nome, value=valor))
                    salvos += 1

        db.commit()
        print(f"  ✓ BCB sync: {salvos} novos, {atualizados} atualizados — {datetime.utcnow().strftime('%H:%M:%S')}")

    except Exception as e:
        db.rollback()
        print(f"  ✗ BCB sync falhou: {e}")
    finally:
        db.close()


def _run_projection_job():
    """
    Generates 6-month projections.
    - No API key set → uses local statistical model (free, always works)
    - ANTHROPIC_API_KEY set → uses Claude for richer narrative analysis
    """
    from models import SessionLocal
    db = SessionLocal()

    try:
        cutoff = date.today() - timedelta(days=400)
        rows   = (
            db.query(IndexHistory)
            .filter(IndexHistory.date >= cutoff)
            .order_by(IndexHistory.date.asc())
            .all()
        )
        if not rows:
            print("  \u2717 Sem dados para projec\u00e3o. Execute sync primeiro.")
            return

        # Build per-index value lists (chronological)
        history: Dict[str, list] = {}
        for row in rows:
            if row.index_name not in history:
                history[row.index_name] = []
            history[row.index_name].append(row.value)

        # Choose engine
        if ANTHROPIC_API_KEY and _ANTHROPIC_AVAILABLE:
            resultado   = _run_claude_projection(history)
            engine_used = "claude-sonnet-4-20250514"
        else:
            print("  \u2139 Using local statistical forecaster (no API key needed)")
            resultado   = _local_forecast(history, steps=6)
            engine_used = resultado.get("model", "statistical")

        if not resultado or not resultado.get("projecoes"):
            print("  \u2717 Projec\u00e3o retornou vazia.")
            return

        MES_MAP = {
            "Jan": 1,"Fev": 2,"Mar": 3,"Abr": 4,"Mai": 5,"Jun": 6,
            "Jul": 7,"Ago": 8,"Set": 9,"Out":10,"Nov":11,"Dez":12,
        }
        INDEX_MAP = {
            "IPCA":"IPCA","INPC":"INPC","IGPM":"IGP-M","PIB":"PIB","INCC":"INCC"
        }

        batch_time = datetime.utcnow()
        analysis   = resultado.get("analise", "")
        risks_json = json.dumps(resultado.get("riscos", []), ensure_ascii=False)
        salvos     = 0

        for proj in resultado["projecoes"]:
            mes_str = proj.get("mes", "")
            try:
                mes_nome, ano_2d = mes_str.split("/")
                proj_date = date(2000 + int(ano_2d), MES_MAP[mes_nome[:3]], 1)
            except Exception:
                continue
            for jk, dk in INDEX_MAP.items():
                val = proj.get(jk)
                if val is None:
                    continue
                db.add(Projection(
                    projection_month=proj_date,
                    index_name=dk,
                    projected_value=float(val),
                    created_at=batch_time,
                    model_used=engine_used,
                    analysis=analysis,
                    risks=risks_json,
                ))
                salvos += 1

        db.commit()
        print(f"  \u2713 Projec\u00e3o ({engine_used}): {salvos} registros \u2014 {batch_time.strftime('%H:%M:%S')}")

    except Exception as e:
        db.rollback()
        print(f"  \u2717 Projec\u00e3o falhou: {e}")
        import traceback; traceback.print_exc()
    finally:
        db.close()


def _run_claude_projection(history: dict) -> dict:
    """Calls Claude API. Only invoked when ANTHROPIC_API_KEY is configured."""
    mes_pt = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
    today  = date.today()
    labels = []
    for i in range(1, 7):
        m = (today.month - 1 + i) % 12
        y = today.year + (today.month - 1 + i) // 12
        labels.append(f"{mes_pt[m]}/{str(y)[2:]}")

    lines = [
        f"  {idx}: " + ", ".join(f"{v:.2f}%" for v in vals[-12:])
        for idx, vals in history.items()
    ]
    template = "\n".join(
        f'    {{"mes": "{lb}", "IPCA": 0.00, "INPC": 0.00, "IGPM": 0.00, "PIB": 0.00, "INCC": 0.00}}'
        for lb in labels
    )
    prompt = (
        "Voc\u00ea \u00e9 um economista s\u00eanior especialista em macroeconomia brasileira.\n"
        "Analise a s\u00e9rie hist\u00f3rica e projete os pr\u00f3ximos 6 meses.\n\n"
        "HIST\u00d3RICO:\n" + "\n".join(lines) + "\n\n"
        "Responda SOMENTE em JSON v\u00e1lido, sem markdown, ponto como decimal.\n"
        'FORMATO:\n{"projecoes": [\n' + template + '\n],\n"analise": "texto",\n"riscos": ["r1","r2","r3"]}'
    )
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg    = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    resposta = msg.content[0].text.strip()
    for tag in ["```json", "```JSON", "```"]:
        resposta = resposta.replace(tag, "")
    return json.loads(resposta.strip())


_scheduler = BackgroundScheduler()


def _start_scheduler():
    _scheduler.add_job(
        _fetch_and_save_all,
        trigger=CronTrigger(hour=6, minute=0),
        id="daily_bcb_sync",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        _run_projection_job,
        trigger=CronTrigger(day_of_week="mon", hour=7, minute=0),
        id="weekly_projection",
        replace_existing=True,
        misfire_grace_time=7200,
    )
    _scheduler.start()
