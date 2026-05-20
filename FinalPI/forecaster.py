"""
forecaster.py — Local Statistical Projection Engine
=====================================================
Replaces the Claude API for generating economic projections.
Uses Double Exponential Smoothing (Holt's method) + Linear Regression.

Zero external dependencies beyond numpy (already needed by most setups).
Falls back to pure-Python math if numpy is unavailable.

Algorithm:
  1. Linear regression  → detects long-term trend
  2. Holt's smoothing   → projects next N values following trend + level
  3. Volatility index   → standard deviation of last 6 months
  4. Rule-based text    → generates analysis and risk bullets from the numbers
"""

import math
import statistics
from datetime import date, timedelta
from typing import Dict, List, Tuple

# ── Try numpy for better precision; fall back to pure Python ─────────────────
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


# ═════════════════════════════════════════════════════════════════════════════
#  MATH HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _linear_regression(values: List[float]) -> Tuple[float, float]:
    """
    Returns (slope, intercept) of the least-squares line through the values.
    slope > 0 means upward trend, slope < 0 means downward.
    """
    n = len(values)
    if n < 2:
        return 0.0, values[0] if values else 0.0

    if _HAS_NUMPY:
        x = np.arange(n, dtype=float)
        slope, intercept = np.polyfit(x, values, 1)
        return float(slope), float(intercept)
    else:
        # Pure Python implementation
        x_mean = (n - 1) / 2.0
        y_mean = statistics.mean(values)
        num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
        den = sum((i - x_mean) ** 2 for i in range(n))
        slope = num / den if den else 0.0
        intercept = y_mean - slope * x_mean
        return slope, intercept


def _holt_smoothing(
    values: List[float],
    alpha: float = 0.35,
    beta: float = 0.25,
    steps: int = 6,
) -> List[float]:
    """
    Holt's Double Exponential Smoothing.
    Projects `steps` values into the future following level + trend.

    alpha: smoothing factor for level  (0–1, higher = more weight to recent)
    beta:  smoothing factor for trend  (0–1)
    """
    if len(values) < 2:
        return [values[0]] * steps if values else [0.0] * steps

    # Initialization
    level = values[0]
    trend = values[1] - values[0]

    for v in values[1:]:
        prev_level = level
        level = alpha * v + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend

    # Forecast
    return [round(level + i * trend, 4) for i in range(1, steps + 1)]


def _volatility(values: List[float]) -> float:
    """Standard deviation of the last 6 values (or all if fewer)."""
    tail = values[-6:] if len(values) >= 6 else values
    if len(tail) < 2:
        return 0.0
    return statistics.stdev(tail)


def _moving_avg(values: List[float], window: int = 3) -> float:
    """Simple moving average of the last `window` values."""
    tail = values[-window:] if len(values) >= window else values
    return statistics.mean(tail) if tail else 0.0


# ═════════════════════════════════════════════════════════════════════════════
#  NEXT 6 MONTH LABELS
# ═════════════════════════════════════════════════════════════════════════════

_MES_PT = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]

def _next_months(n: int = 6) -> List[Tuple[str, date]]:
    """Returns [(label, date), ...] for the next n months."""
    today   = date.today()
    result  = []
    for i in range(1, n + 1):
        month = (today.month - 1 + i) % 12
        year  = today.year + (today.month - 1 + i) // 12
        label = f"{_MES_PT[month]}/{str(year)[2:]}"
        result.append((label, date(year, month + 1, 1)))
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  ANALYSIS TEXT GENERATOR
# ═════════════════════════════════════════════════════════════════════════════

def _describe_trend(slope: float, index: str) -> str:
    if abs(slope) < 0.01:
        return f"O {index} apresenta comportamento estável, sem tendência clara de alta ou baixa."
    direction = "alta" if slope > 0 else "queda"
    intensity = "moderada" if abs(slope) < 0.05 else "acentuada"
    return f"O {index} mostra tendência de {direction} {intensity} de {abs(slope):.3f} p.p./mês."


def _generate_analysis(
    data: Dict[str, List[float]],
    projections: Dict[str, List[float]],
) -> str:
    parts = []

    ipca_vals = data.get("IPCA", [])
    if ipca_vals:
        slope, _ = _linear_regression(ipca_vals)
        avg6     = _moving_avg(ipca_vals, 6)
        vol      = _volatility(ipca_vals)
        proj_avg = statistics.mean(projections.get("IPCA", [avg6]))

        direction = "pressão inflacionária" if slope > 0.02 else (
            "arrefecimento da inflação" if slope < -0.02 else "inflação estável"
        )
        parts.append(
            f"A análise estatística dos últimos {len(ipca_vals)} meses aponta {direction}. "
            f"A média recente do IPCA é de {avg6:.2f}% ao mês, com volatilidade de {vol:.3f} p.p. "
            f"As projeções indicam média de {proj_avg:.2f}% nos próximos 6 meses."
        )

    igpm_vals = data.get("IGP-M", [])
    if igpm_vals:
        slope_gm, _ = _linear_regression(igpm_vals)
        avg_gm      = _moving_avg(igpm_vals, 3)
        desc        = "acima" if avg_gm > (statistics.mean(ipca_vals[-3:]) if ipca_vals else 0) else "abaixo"
        parts.append(
            f"O IGP-M permanece {desc} do IPCA (média recente {avg_gm:.2f}%), "
            f"{'pressionado por custos de atacado e construção' if slope_gm > 0 else 'com tendência de convergência ao IPCA'}."
        )

    pib_vals = data.get("PIB", [])
    if pib_vals:
        avg_pib  = _moving_avg(pib_vals, 4)
        slope_p, _ = _linear_regression(pib_vals)
        crescimento = "crescimento moderado" if avg_pib > 0 else "contração"
        parts.append(
            f"O PIB apresenta {crescimento} (média {avg_pib:.2f}%), "
            f"com {'perspectiva de melhora' if slope_p > 0 else 'risco de desaceleração'} no horizonte projetado."
        )

    return " ".join(parts) or "Projeções geradas por modelo estatístico (Holt's Double Exponential Smoothing)."


def _generate_risks(
    data: Dict[str, List[float]],
    projections: Dict[str, List[float]],
) -> List[str]:
    risks = []

    ipca_vals = data.get("IPCA", [])
    if ipca_vals:
        vol = _volatility(ipca_vals)
        slope, _ = _linear_regression(ipca_vals)
        if vol > 0.15:
            risks.append(f"Alta volatilidade do IPCA ({vol:.3f} p.p. desvio padrão) aumenta incerteza nas projeções.")
        if slope > 0.03:
            risks.append("Tendência de alta do IPCA pode pressionar o Banco Central a manter a Selic elevada.")

    incc_vals = data.get("INCC", [])
    if incc_vals:
        avg_incc = _moving_avg(incc_vals, 3)
        if avg_incc > 0.5:
            risks.append(f"INCC elevado ({avg_incc:.2f}% médio) pressiona custos do setor de construção civil.")

    igpm_vals = data.get("IGP-M", [])
    if igpm_vals:
        slope_gm, _ = _linear_regression(igpm_vals)
        if slope_gm > 0.04:
            risks.append("IGP-M em alta pode antecipar repasse de custos de atacado ao consumidor final.")

    pib_vals = data.get("PIB", [])
    if pib_vals:
        slope_pib, _ = _linear_regression(pib_vals)
        if slope_pib < -0.02:
            risks.append("Desaceleração do PIB pode reduzir arrecadação e pressionar o déficit fiscal.")

    # Always add a macro risk
    risks.append(
        "Variações cambiais (USD/BRL) e choques externos podem divergir as projeções do cenário-base."
    )

    if len(risks) < 3:
        risks.append(
            "Incertezas políticas e fiscais representam risco adicional para o horizonte de 6 meses."
        )

    return risks[:5]  # cap at 5


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def generate_projections(
    history: Dict[str, List[float]],
    steps: int = 6,
) -> dict:
    """
    Main function. Call with a dict of {index_name: [values in chronological order]}.

    Returns the same structure the old Claude endpoint returned:
    {
        "projecoes": [{"mes": "Jun/25", "IPCA": 0.00, ...}, ...],
        "analise": "...",
        "riscos": ["...", ...],
        "model": "statistical"
    }
    """
    INDEX_KEYS = ["IPCA", "INPC", "IGP-M", "PIB", "INCC"]
    PROJ_KEY   = {"IGP-M": "IGPM"}  # key mapping for output JSON

    month_labels = _next_months(steps)

    # Compute projections per index
    proj_by_index: Dict[str, List[float]] = {}
    for idx in INDEX_KEYS:
        vals = history.get(idx, [])
        if len(vals) >= 3:
            proj_by_index[idx] = _holt_smoothing(vals, steps=steps)
        elif vals:
            # Not enough data — flat forecast at last value
            proj_by_index[idx] = [vals[-1]] * steps
        else:
            proj_by_index[idx] = [0.0] * steps

    # Build output rows
    projecoes = []
    for i, (label, _) in enumerate(month_labels):
        row = {"mes": label}
        for idx in INDEX_KEYS:
            out_key = PROJ_KEY.get(idx, idx)
            row[out_key] = round(proj_by_index[idx][i], 2)
        projecoes.append(row)

    analise = _generate_analysis(history, proj_by_index)
    riscos  = _generate_risks(history, proj_by_index)

    return {
        "projecoes": projecoes,
        "analise":   analise,
        "riscos":    riscos,
        "model":     "statistical (Holt's Double Exponential Smoothing)",
    }


# ── Quick self-test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    sample = {
        "IPCA":  [0.45, 0.52, 0.48, 0.61, 0.56, 0.44, 0.49, 0.53, 0.57, 0.60, 0.55, 0.58],
        "INPC":  [0.50, 0.55, 0.51, 0.63, 0.59, 0.46, 0.52, 0.56, 0.60, 0.64, 0.58, 0.61],
        "IGP-M": [0.30, 0.41, 0.35, 0.55, 0.62, 0.48, 0.52, 0.66, 0.71, 0.75, 0.68, 0.72],
        "PIB":   [0.40, 0.38, 0.42, 0.44, 0.41, 0.39, 0.43, 0.45, 0.42, 0.40, 0.44, 0.46],
        "INCC":  [0.55, 0.60, 0.58, 0.65, 0.70, 0.68, 0.72, 0.75, 0.71, 0.69, 0.73, 0.76],
    }
    result = generate_projections(sample)
    print(json.dumps(result, ensure_ascii=False, indent=2))
