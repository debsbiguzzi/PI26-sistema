import os
import sys
import json
import time
import traceback
from datetime import datetime, timedelta, date

# ── Verificar dependencias ──────────────────────────────────────────────────

def check_deps():
    missing = []
    try:
        import requests
    except ImportError:
        missing.append("requests")
    try:
        import anthropic
    except ImportError:
        missing.append("anthropic")
    try:
        import sqlalchemy
    except ImportError:
        missing.append("sqlalchemy")
    try:
        import psycopg2
    except ImportError:
        missing.append("psycopg2-binary")
    if missing:
        print("\n[ERRO] Dependencias faltando. Execute:")
        print(f"  pip install {' '.join(missing)}")
        sys.exit(1)

check_deps()

import requests
import anthropic
from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    Date, DateTime, Text, UniqueConstraint, text
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# ── Configuracao ────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BCB_BASE  = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados/ultimos/{n}?formato=json"
TIMEOUT   = 15   # segundos por requisicao
ULTIMOS   = 12   # meses historicos

# !! EDITE AQUI com sua senha do PostgreSQL !!
# Formato: postgresql://USUARIO:SENHA@HOST:PORTA/NOME_DO_BANCO
# Se quiser usar variavel de ambiente, rode antes:
#   set DATABASE_URL=postgresql://postgres:8433@localhost:5432/economic_data
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:8433@localhost:5432/economic_data"
)

# Codigos SGS do Banco Central

SERIES = {
    "IPCA":  433,
    "INPC":  188,
    "IGP-M": 189,
    "PIB":   1207,
    "INCC":  192,
}

# Cores para terminal (ANSI)

class Cor:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    CYAN   = "\033[96m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    MAGENTA= "\033[95m"
    BLUE   = "\033[94m"
    GRAY   = "\033[90m"
    WHITE  = "\033[97m"

COR_INDICE = {
    "IPCA":  Cor.CYAN,
    "INPC":  Cor.MAGENTA,
    "IGP-M": Cor.YELLOW,
    "PIB":   Cor.GREEN,
    "INCC":  Cor.BLUE,
}

# ── Banco de Dados — Setup ───────────────────────────────────────────────────
# O engine e a "conexao viva" com o PostgreSQL.
# SessionLocal e a fabrica de sessoes — cada operacao no banco usa uma sessao.

try:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    DB_DISPONIVEL = True
except Exception as e:
    DB_DISPONIVEL = False
    print(f"{Cor.YELLOW}  ⚠ Banco de dados nao configurado: {e}{Cor.RESET}")

class Base(DeclarativeBase):
    pass

# ── Modelo: indices_history ──────────────────────────────────────────────────
# Representa a tabela que guarda os valores mensais buscados da BCB.
# Cada linha = um indice em um mes especifico.

class IndexHistory(Base):
    __tablename__ = "indices_history"

    id         = Column(Integer, primary_key=True)
    date       = Column(Date, nullable=False)          # Primeiro dia do mes de referencia
    index_name = Column(String(10), nullable=False)    # "IPCA", "INPC", "IGP-M", "PIB", "INCC"
    value      = Column(Float, nullable=False)         # Valor percentual mensal, ex: 0.56
    fetched_at = Column(DateTime, default=datetime.utcnow)  # Quando foi buscado da BCB

    # Garante que nao existam duas linhas com o mesmo indice na mesma data.
    # Se o APScheduler buscar todo dia, nao cria duplicatas.
    __table_args__ = (
        UniqueConstraint("date", "index_name", name="uq_history_date_index"),
    )

# ── Modelo: projections ──────────────────────────────────────────────────────
# Guarda as projecoes geradas pelo Claude.
# Cada "rodada" de projecao gera 6 meses x 5 indices = 30 linhas novas,
# todas com o mesmo created_at para identificar o lote.

class Projection(Base):
    __tablename__ = "projections"

    id               = Column(Integer, primary_key=True)
    projection_month = Column(Date, nullable=False)      # Mes futuro projetado
    index_name       = Column(String(10), nullable=False)
    projected_value  = Column(Float, nullable=False)
    created_at       = Column(DateTime, default=datetime.utcnow)  # Timestamp do lote
    model_used       = Column(String(50), default="claude-opus-4-5")
    analysis         = Column(Text, nullable=True)        # Texto de analise do Claude
    risks            = Column(Text, nullable=True)        # Lista de riscos em JSON

# ── Inicializar banco ────────────────────────────────────────────────────────

def init_db():
    """Cria as tabelas no PostgreSQL se ainda nao existirem."""
    if not DB_DISPONIVEL:
        return
    try:
        Base.metadata.create_all(bind=engine)
        # Testa conexao real
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print(f"  {Cor.GREEN}✓ Banco de dados conectado e tabelas prontas.{Cor.RESET}")
    except Exception as e:
        print(f"  {Cor.RED}✗ Erro ao conectar no banco: {e}{Cor.RESET}")
        print(f"  {Cor.GRAY}  Verifique se o PostgreSQL esta rodando e se a senha em DATABASE_URL esta correta.{Cor.RESET}")

# ── Utilitarios ─────────────────────────────────────────────────────────────

def fmt(valor, casas=2, sufixo="%"):
    """Formata numero com virgula no estilo BR."""
    try:
        return f"{float(valor):.{casas}f}{sufixo}".replace(".", ",")
    except (TypeError, ValueError):
        return "N/D"

def linha(char="─", n=62):
    return char * n

def titulo(texto, cor=Cor.CYAN):
    print(f"\n{cor}{Cor.BOLD}{linha('═')}{Cor.RESET}")
    print(f"{cor}{Cor.BOLD}  {texto}{Cor.RESET}")
    print(f"{cor}{Cor.BOLD}{linha('═')}{Cor.RESET}")

def subtitulo(texto, cor=Cor.BLUE):
    print(f"\n{cor}{Cor.BOLD}  {texto}{Cor.RESET}")
    print(f"{Cor.GRAY}  {linha('-', 58)}{Cor.RESET}")

def spinner(msg):
    print(f"{Cor.GRAY}  ⏳ {msg}…{Cor.RESET}", end="\r")

# ── Busca BCB ───────────────────────────────────────────────────────────────

def buscar_serie_bcb(nome, codigo, n=ULTIMOS, tentativas=3):
    """
    Busca serie temporal no SGS do Banco Central.
    Retorna lista de dicts: [{"data": "dd/MM/yyyy", "valor": float}, …]
    Tenta ate 3 vezes antes de desistir.
    """
    url = BCB_BASE.format(codigo=codigo, n=n)
    for tentativa in range(1, tentativas + 1):
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()

            raw = resp.json()

            if not isinstance(raw, list) or len(raw) == 0:
                raise ValueError(f"Resposta vazia ou formato inesperado para {nome}")

            resultado = []
            for item in raw:
                data_str = item.get("data", "")
                valor_str = str(item.get("valor", "0")).replace(",", ".")

                try:
                    valor = float(valor_str)
                except ValueError:
                    valor = 0.0

                resultado.append({"data": data_str, "valor": valor})

            return resultado

        except requests.exceptions.ConnectionError:
            erro = "Sem conexao com a internet"
        except requests.exceptions.Timeout:
            erro = f"Timeout apos {TIMEOUT}s"
        except requests.exceptions.HTTPError as e:
            erro = f"HTTP {e.response.status_code}"
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            erro = f"Dados invalidos: {e}"
        except Exception as e:
            erro = str(e)

        if tentativa < tentativas:
            print(f"  {Cor.YELLOW}⚠ {nome} tentativa {tentativa}/{tentativas}: {erro}. Aguardando...{Cor.RESET}")
            time.sleep(2)
        else:
            print(f"  {Cor.RED}✗ {nome} falhou apos {tentativas} tentativas: {erro}{Cor.RESET}")
            return []

# ── Salvar indices no banco ──────────────────────────────────────────────────

def salvar_no_banco(dados: dict):
    """
    Recebe o dicionario retornado por buscar_todos_indices() e salva
    cada valor no PostgreSQL.

    Logica de upsert:
      - Se a linha (data + nome do indice) ja existe → atualiza o valor
        (a BCB as vezes revisa dados anteriores)
      - Se nao existe → insere nova linha

    A conversao de data acontece aqui:
      "01/03/2025" (string BCB) → date(2025, 3, 1) (objeto Python)
    O SQLAlchemy converte o objeto date para o formato do PostgreSQL automaticamente.
    """
    if not DB_DISPONIVEL:
        print(f"  {Cor.YELLOW}⚠ Banco nao disponivel, dados nao salvos.{Cor.RESET}")
        return

    db = SessionLocal()
    salvos = 0
    atualizados = 0

    try:
        for nome, serie in dados.items():
            for item in serie:
                # ── Conversao de data ──────────────────────────────────────
                # BCB retorna "01/03/2025" (dia/mes/ano)
                # Precisamos de um objeto date(2025, 3, 1) para o banco
                try:
                    dia, mes, ano = item["data"].split("/")
                    data_obj = date(int(ano), int(mes), int(dia))
                except Exception:
                    continue  # Pula itens com data invalida

                # ── Upsert ────────────────────────────────────────────────
                existente = db.query(IndexHistory).filter(
                    IndexHistory.date == data_obj,
                    IndexHistory.index_name == nome
                ).first()

                if existente:
                    # Linha ja existe: so atualiza o valor
                    existente.value = item["valor"]
                    existente.fetched_at = datetime.utcnow()
                    atualizados += 1
                else:
                    # Linha nova: insere
                    db.add(IndexHistory(
                        date=data_obj,
                        index_name=nome,
                        value=item["valor"],
                    ))
                    salvos += 1

        db.commit()
        print(
            f"  {Cor.GREEN}✓ Banco atualizado:{Cor.RESET} "
            f"{Cor.WHITE}{salvos} novos{Cor.RESET}, "
            f"{Cor.GRAY}{atualizados} atualizados{Cor.RESET}"
        )

    except Exception as e:
        db.rollback()
        print(f"  {Cor.RED}✗ Erro ao salvar no banco: {e}{Cor.RESET}")
        if os.environ.get("DEBUG"):
            traceback.print_exc()
    finally:
        db.close()  # Sempre fecha a sessao, mesmo se der erro

# ── Salvar projecoes no banco ────────────────────────────────────────────────

def salvar_projecao_no_banco(resultado: dict):
    """
    Salva o JSON de projecoes retornado pelo Claude no banco.
    Cada lote de projecao tem um created_at unico para poder comparar
    projecoes feitas em datas diferentes.

    Conversao do mes:
      "Abr/25" (string do Claude) → date(2025, 4, 1) (objeto Python)
    """
    if not DB_DISPONIVEL:
        return

    MES_MAP = {
        "Jan": 1, "Fev": 2, "Mar": 3, "Abr": 4, "Mai": 5, "Jun": 6,
        "Jul": 7, "Ago": 8, "Set": 9, "Out": 10, "Nov": 11, "Dez": 12
    }
    # Claude usa "IGPM" no JSON, mas o banco guarda "IGP-M"
    INDEX_MAP = {
        "IPCA": "IPCA", "INPC": "INPC", "IGPM": "IGP-M", "PIB": "PIB", "INCC": "INCC"
    }

    db = SessionLocal()
    salvos = 0
    batch_time = datetime.utcnow()  # Timestamp unico para este lote de projecoes
    analysis_text = resultado.get("analise", "")
    risks_json = json.dumps(resultado.get("riscos", []), ensure_ascii=False)

    try:
        for proj in resultado.get("projecoes", []):
            mes_str = proj.get("mes", "")  # Ex: "Abr/25"

            # ── Conversao do mes ───────────────────────────────────────────
            try:
                mes_nome, ano_2d = mes_str.split("/")
                mes_num = MES_MAP.get(mes_nome[:3], 1)
                ano = 2000 + int(ano_2d)
                proj_date = date(ano, mes_num, 1)
            except Exception:
                continue

            for json_key, db_key in INDEX_MAP.items():
                val = proj.get(json_key)
                if val is None:
                    continue
                db.add(Projection(
                    projection_month=proj_date,
                    index_name=db_key,
                    projected_value=float(val),
                    created_at=batch_time,
                    model_used="claude-opus-4-5",
                    analysis=analysis_text,
                    risks=risks_json,
                ))
                salvos += 1

        db.commit()
        print(
            f"  {Cor.GREEN}✓ {salvos} projecoes salvas no banco "
            f"{Cor.GRAY}(lote: {batch_time.strftime('%d/%m/%Y %H:%M')}){Cor.RESET}"
        )

    except Exception as e:
        db.rollback()
        print(f"  {Cor.RED}✗ Erro ao salvar projecoes: {e}{Cor.RESET}")
    finally:
        db.close()

# ── Exibir historico do banco ────────────────────────────────────────────────

def exibir_historico_banco():
    """
    Le os dados salvos no PostgreSQL e exibe no terminal.
    Util para confirmar que os dados estao sendo persistidos corretamente,
    e para ver o historico acumulado ao longo do tempo.
    """
    if not DB_DISPONIVEL:
        print(f"  {Cor.RED}✗ Banco nao disponivel.{Cor.RESET}")
        return

    subtitulo("Historico salvo no banco (ultimos 6 meses)", Cor.CYAN)
    db = SessionLocal()
    try:
        cutoff = date.today() - timedelta(days=190)
        rows = (
            db.query(IndexHistory)
            .filter(IndexHistory.date >= cutoff)
            .order_by(IndexHistory.date.desc(), IndexHistory.index_name.asc())
            .all()
        )

        if not rows:
            print(f"  {Cor.YELLOW}  Nenhum dado no banco ainda. Execute a opcao 1 primeiro.{Cor.RESET}")
            return

        # Agrupa por data para exibir em tabela
        por_data = {}
        for row in rows:
            chave = row.date.strftime("%d/%m/%Y")
            if chave not in por_data:
                por_data[chave] = {}
            por_data[chave][row.index_name] = row.value

        # Cabecalho
        header = f"  {'Data':<12}"
        for nome in SERIES:
            cor = COR_INDICE.get(nome, Cor.WHITE)
            header += f"  {cor}{Cor.BOLD}{nome:>7}{Cor.RESET}"
        print(header)
        print(f"  {Cor.GRAY}{linha('-', 60)}{Cor.RESET}")

        for data_str, valores in sorted(por_data.items(), reverse=True):
            linha_txt = f"  {Cor.GRAY}{data_str:<12}{Cor.RESET}"
            for nome in SERIES:
                cor = COR_INDICE.get(nome, Cor.WHITE)
                val = valores.get(nome)
                if val is not None:
                    linha_txt += f"  {cor}{fmt(val):>8}{Cor.RESET}"
                else:
                    linha_txt += f"  {Cor.GRAY}{'N/D':>8}{Cor.RESET}"
            print(linha_txt)

        total = db.query(IndexHistory).count()
        print(f"\n  {Cor.GRAY}Total de registros no banco: {Cor.WHITE}{total}{Cor.RESET}")

    except Exception as e:
        print(f"  {Cor.RED}✗ Erro ao ler banco: {e}{Cor.RESET}")
    finally:
        db.close()

# ── Busca todos os indices ──────────────────────────────────────────────────

def buscar_todos_indices():
    """Busca todos os 5 indices e retorna dicionario organizado."""
    dados = {}
    print()
    for nome, codigo in SERIES.items():
        spinner(f"Buscando {nome} (BCB #{codigo})")
        serie = buscar_serie_bcb(nome, codigo)
        dados[nome] = serie
        if serie:
            ultimo = serie[-1]
            cor = COR_INDICE.get(nome, Cor.WHITE)
            print(
                f"  {cor}{Cor.BOLD}{nome:<6}{Cor.RESET}  "
                f"{Cor.GRAY}({len(serie)} meses){Cor.RESET}  "
                f"Ultimo: {Cor.WHITE}{fmt(ultimo['valor'])}{Cor.RESET} "
                f"{Cor.GRAY}em {ultimo['data']}{Cor.RESET}       "
            )
        else:
            print(f"  {Cor.RED}{nome:<6}  SEM DADOS{Cor.RESET}       ")

    # ── NOVO: salva automaticamente no banco apos buscar ──────────────────
    salvar_no_banco(dados)

    return dados

# ── Exibir tabela de indices ──────────────────────────────────────────────────

def exibir_tabela(dados):
    """Exibe tabela comparativa dos ultimos 12 meses."""
    subtitulo("Tabela — Ultimos 12 meses")

    meses = []
    for nome, serie in dados.items():
        for item in serie:
            if item["data"] not in meses:
                meses.append(item["data"])

    header = f"  {'Data':<12}"
    for nome in SERIES:
        cor = COR_INDICE.get(nome, Cor.WHITE)
        header += f"  {cor}{Cor.BOLD}{nome:>7}{Cor.RESET}"
    print(header)
    print(f"  {Cor.GRAY}{linha('-', 60)}{Cor.RESET}")

    referencia = dados.get("IPCA") or dados.get("INPC") or []
    for item in referencia:
        data = item["data"]
        linha_txt = f"  {Cor.GRAY}{data:<12}{Cor.RESET}"
        for nome in SERIES:
            serie = dados.get(nome, [])
            valor = None
            for d in serie:
                if d["data"] == data:
                    valor = d["valor"]
                    break
            cor = COR_INDICE.get(nome, Cor.WHITE)
            if valor is not None:
                linha_txt += f"  {cor}{fmt(valor):>8}{Cor.RESET}"
            else:
                linha_txt += f"  {Cor.GRAY}{'N/D':>8}{Cor.RESET}"
        print(linha_txt)

# ── Exibir resumo KPIs ──────────────────────────────────────────────────────

def exibir_kpis(dados):
    """Exibe os valores mais recentes de cada indice."""
    subtitulo("Valores Mais Recentes")

    for nome in SERIES:
        serie = dados.get(nome, [])
        cor = COR_INDICE.get(nome, Cor.WHITE)
        if serie:
            ultimo = serie[-1]
            anterior = serie[-2] if len(serie) >= 2 else None

            if anterior:
                delta = ultimo["valor"] - anterior["valor"]
                seta = "▲" if delta >= 0 else "▼"
                cor_delta = Cor.RED if delta > 0 and nome != "PIB" else Cor.GREEN
                delta_str = f" {cor_delta}{seta} {fmt(abs(delta))}{Cor.RESET}"
            else:
                delta_str = ""

            print(
                f"  {cor}{Cor.BOLD}{nome:<6}{Cor.RESET}  "
                f"{Cor.WHITE}{Cor.BOLD}{fmt(ultimo['valor']):>8}{Cor.RESET}"
                f"{delta_str}"
                f"  {Cor.GRAY}({ultimo['data']}){Cor.RESET}"
            )
        else:
            print(f"  {cor}{Cor.BOLD}{nome:<6}{Cor.RESET}  {Cor.RED}SEM DADOS{Cor.RESET}")

# ── Projecao IA ─────────────────────────────────────────────────────────────

def gerar_projecao(dados):
    """Usa Claude para projetar os proximos 6 meses."""
    subtitulo("Projecao IA — Proximos 6 Meses", Cor.MAGENTA)

    chave = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    if not chave:
        print(f"  {Cor.YELLOW}⚠ ANTHROPIC_API_KEY nao definida.{Cor.RESET}")
        print(f"  {Cor.GRAY}Configure com:{Cor.RESET}")
        print(f"  {Cor.WHITE}  set ANTHROPIC_API_KEY=sua-chave-aqui{Cor.RESET}")
        print(f"  {Cor.GRAY}Obtenha em: console.anthropic.com{Cor.RESET}")
        return

    indices_com_dados = {k: v for k, v in dados.items() if len(v) >= 3}
    if not indices_com_dados:
        print(f"  {Cor.RED}✗ Dados insuficientes para projecao. Verifique a conexao.{Cor.RESET}")
        return

    linhas_historico = []
    referencia = dados.get("IPCA") or list(dados.values())[0]
    for item in referencia:
        data = item["data"]
        valores = {}
        for nome, serie in dados.items():
            for d in serie:
                if d["data"] == data:
                    valores[nome] = d["valor"]
                    break
        partes = ", ".join(f"{k}={v:.2f}%" for k, v in valores.items())
        linhas_historico.append(f"  {data}: {partes}")

    historico_texto = "\n".join(linhas_historico)

    prompt = f"""Voce e um economista senior especialista em macroeconomia brasileira.

Analise a serie historica dos ultimos {len(referencia)} meses dos indicadores abaixo e realize:

1. Projecao dos proximos 6 meses para cada indice (IPCA, INPC, IGP-M, PIB, INCC)
2. Analise das tendencias e principais riscos

SERIE HISTORICA (valores mensais em %):
{historico_texto}

CONTEXTO ATUAL (Marco 2025):

- Taxa Selic: 14,75% ao ano (ciclo de alta)
- Dolar: aproximadamente R$ 5,70
- Inflacao pressionada em servicos e alimentos
- PIB brasileiro com crescimento moderado
- INCC acelerado por custos de materiais de construcao

INSTRUCOES:

- Responda SOMENTE em JSON valido
- Nao inclua texto antes ou depois do JSON
- Nao use markdown, backticks ou comentarios
- Use ponto como separador decimal nos numeros

FORMATO EXATO DA RESPOSTA:
{{
"projecoes": [
{{"mes": "Abr/25", "IPCA": 0.00, "INPC": 0.00, "IGPM": 0.00, "PIB": 0.00, "INCC": 0.00}},
{{"mes": "Mai/25", "IPCA": 0.00, "INPC": 0.00, "IGPM": 0.00, "PIB": 0.00, "INCC": 0.00}},
{{"mes": "Jun/25", "IPCA": 0.00, "INPC": 0.00, "IGPM": 0.00, "PIB": 0.00, "INCC": 0.00}},
{{"mes": "Jul/25", "IPCA": 0.00, "INPC": 0.00, "IGPM": 0.00, "PIB": 0.00, "INCC": 0.00}},
{{"mes": "Ago/25", "IPCA": 0.00, "INPC": 0.00, "IGPM": 0.00, "PIB": 0.00, "INCC": 0.00}},
{{"mes": "Set/25", "IPCA": 0.00, "INPC": 0.00, "IGPM": 0.00, "PIB": 0.00, "INCC": 0.00}}
],
"analise": "texto da analise aqui",
"riscos": ["risco 1", "risco 2", "risco 3"]
}}"""

    spinner("Consultando IA (Claude)...")
    try:
        client = anthropic.Anthropic(api_key=chave)
        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )

        resposta_bruta = msg.content[0].text.strip()

        resposta_limpa = resposta_bruta
        for tag in ["```json", "```JSON", "```"]:
            resposta_limpa = resposta_limpa.replace(tag, "")
        resposta_limpa = resposta_limpa.strip()

        try:
            resultado = json.loads(resposta_limpa)
        except json.JSONDecodeError as e:
            import re
            match = re.search(r'\{[\s\S]*\}', resposta_limpa)
            if match:
                resultado = json.loads(match.group())
            else:
                raise ValueError(f"JSON invalido: {e}\nResposta: {resposta_bruta[:200]}")

        projecoes = resultado.get("projecoes", [])
        if not projecoes:
            raise ValueError("Campo 'projecoes' ausente ou vazio no JSON")

        print(f"  {Cor.GREEN}✓ Projecao gerada com sucesso!{Cor.RESET}       \n")

        colunas = ["IPCA", "INPC", "IGPM", "PIB", "INCC"]
        header = f"  {'Mes':<10}"
        for col in colunas:
            cor = COR_INDICE.get(col.replace("GM", "-M"), Cor.WHITE)
            nome_exib = "IGP-M" if col == "IGPM" else col
            header += f"  {cor}{Cor.BOLD}{nome_exib:>7}{Cor.RESET}"
        print(header)
        print(f"  {Cor.GRAY}{linha('-', 58)}{Cor.RESET}")

        for proj in projecoes:
            mes = proj.get("mes", "?")
            linha_proj = f"  {Cor.YELLOW}{Cor.BOLD}{mes:<10}{Cor.RESET}"
            for col in colunas:
                chave_proj = col
                val = proj.get(chave_proj)
                if val is None:
                    for variante in [col.lower(), col.upper(), "IGP-M" if col == "IGPM" else col]:
                        val = proj.get(variante)
                        if val is not None:
                            break

                cor = COR_INDICE.get(col.replace("GM", "-M"), Cor.WHITE)
                if val is not None:
                    try:
                        linha_proj += f"  {cor}{fmt(float(val)):>8}{Cor.RESET}"
                    except (TypeError, ValueError):
                        linha_proj += f"  {Cor.GRAY}{'N/D':>8}{Cor.RESET}"
                else:
                    linha_proj += f"  {Cor.GRAY}{'N/D':>8}{Cor.RESET}"
            print(linha_proj)

        analise = resultado.get("analise", "")
        if analise:
            print(f"\n  {Cor.MAGENTA}{Cor.BOLD}ANALISE DA IA:{Cor.RESET}")
            palavras = analise.split()
            linha_atual = "  "
            for palavra in palavras:
                if len(linha_atual) + len(palavra) + 1 > 72:
                    print(f"{Cor.WHITE}{linha_atual}{Cor.RESET}")
                    linha_atual = "  " + palavra + " "
                else:
                    linha_atual += palavra + " "
            if linha_atual.strip():
                print(f"{Cor.WHITE}{linha_atual}{Cor.RESET}")

        riscos = resultado.get("riscos", [])
        if riscos:
            print(f"\n  {Cor.RED}{Cor.BOLD}PRINCIPAIS RISCOS:{Cor.RESET}")
            for r in riscos:
                print(f"  {Cor.RED}•{Cor.RESET} {Cor.WHITE}{r}{Cor.RESET}")

        print(f"\n  {Cor.GRAY}* Projecoes geradas por IA. Nao constituem recomendacao financeira.{Cor.RESET}")

        # ── NOVO: salva projecoes no banco automaticamente ─────────────────
        salvar_projecao_no_banco(resultado)

        return resultado

    except anthropic.AuthenticationError:
        print(f"  {Cor.RED}✗ Chave da API invalida.{Cor.RESET}")
        print(f"  {Cor.GRAY}Verifique em: console.anthropic.com{Cor.RESET}")
    except anthropic.RateLimitError:
        print(f"  {Cor.RED}✗ Limite de requisicoes atingido. Aguarde e tente novamente.{Cor.RESET}")
    except anthropic.APIConnectionError:
        print(f"  {Cor.RED}✗ Sem conexao com a API da Anthropic. Verifique a internet.{Cor.RESET}")
    except ValueError as e:
        print(f"  {Cor.RED}✗ Erro ao processar resposta da IA: {e}{Cor.RESET}")
    except Exception as e:
        print(f"  {Cor.RED}✗ Erro inesperado: {e}{Cor.RESET}")
        if os.environ.get("DEBUG"):
            traceback.print_exc()

# ── Bolsa Familia Campinas ───────────────────────────────────────────────────

def exibir_bolsa_familia():
    """Exibe dados do Bolsa Familia em Campinas com base em dados publicos 2024/25."""
    titulo("BOLSA FAMILIA — CAMPINAS / SP", Cor.GREEN)
    print(f"  {Cor.GRAY}Fontes: Secom-SP (Jan/2025), DGSUAS, PUC-Campinas (CadUnico 2024){Cor.RESET}")

    subtitulo("Numeros Gerais")
    kpis = [
        ("Beneficiarios BF", "57.757 familias", "Jan/2025 (3o maior no estado SP)", Cor.CYAN),
        ("CadUnico Total",   "137.198 familias", "Dez/2024 (+8% vs 2022)", Cor.GREEN),
        ("Pessoas CadUnico", "316.580 pessoas",  "27% da populacao de Campinas", Cor.YELLOW),
        ("Renda Campinas",   "25.000 familias",  "R$ 134 a R$ 201/mes (prog. municipal)", Cor.MAGENTA),
        ("Reducao Pobreza",  "-21%",             "2022: 74.863 fam. → 2024: 58.855 fam.", Cor.GREEN),
    ]
    for label, valor, obs, cor in kpis:
        print(f"  {Cor.GRAY}{label:<22}{Cor.RESET}  {cor}{Cor.BOLD}{valor:<22}{Cor.RESET}  {Cor.GRAY}{obs}{Cor.RESET}")

    subtitulo("Distribuicao por Regiao (Renda Campinas / BF 2024)")
    regioes = [
        ("Sul",      31, 17670, 285, "Alta"),
        ("Noroeste", 24, 13716, 310, "Media-Alta"),
        ("Sudoeste", 23, 13146, 295, "Media-Alta"),
        ("Norte",    14,  7993, 340, "Media"),
        ("Leste",     8,  4560, 390, "Baixa"),
    ]

    BARRA_MAX = 30
    print(f"  {Cor.GRAY}{'Regiao':<12} {'%':>4}  {'Grafico':<32} {'Benefic.':>10}  {'R.Media':>8}  Vulnerab.{Cor.RESET}")
    print(f"  {Cor.GRAY}{linha('-', 80)}{Cor.RESET}")
    cores_regiao = [Cor.RED, Cor.YELLOW, Cor.BLUE, Cor.MAGENTA, Cor.GREEN]
    for i, (reg, pct, benef, renda, vuln) in enumerate(regioes):
        cor = cores_regiao[i]
        barras = int(pct / 100 * BARRA_MAX)
        barra = "█" * barras + "░" * (BARRA_MAX - barras)
        cor_vuln = Cor.RED if "Alta" in vuln and "Media" not in vuln else (Cor.YELLOW if "Media" in vuln else Cor.GREEN)
        print(
            f"  {cor}{Cor.BOLD}{reg:<12}{Cor.RESET} {Cor.WHITE}{pct:>3}%{Cor.RESET}  "
            f"{cor}{barra}{Cor.RESET}  {Cor.WHITE}{benef:>10,}{Cor.RESET}  "
            f"{Cor.CYAN}R${renda:>6}{Cor.RESET}  {cor_vuln}{vuln}{Cor.RESET}"
        )

    subtitulo("Perfil dos Beneficiarios (2024)")
    perfil = [
        ("Responsaveis femininos",  "83%", Cor.MAGENTA),
        ("Escolaridade Ens. Medio", "54%", Cor.CYAN),
        ("Autonomos (mercado inf.)", "30%", Cor.YELLOW),
        ("Empregados formais",       "15%", Cor.GREEN),
        ("Jovens 'nem-nem'",         "60%", Cor.RED),
    ]
    for label, val, cor in perfil:
        print(f"  {Cor.GRAY}{label:<28}{Cor.RESET}  {cor}{Cor.BOLD}{val}{Cor.RESET}")

    subtitulo("Evolucao — Familias em Situacao de Pobreza Extrema")
    evolucao = [
        (2020, 48200), (2021, 61400), (2022, 74863),
        (2023, 68200), (2024, 57757), (2025, 55100),
    ]
    maximo = max(v for _, v in evolucao)
    for ano, val in evolucao:
        barras = int(val / maximo * 35)
        cor = Cor.RED if val == maximo else (Cor.GREEN if val < 60000 else Cor.YELLOW)
        estrela = " ← pico" if val == maximo else (" ← proj." if ano == 2025 else "")
        print(f"  {Cor.GRAY}{ano}{Cor.RESET}  {cor}{'█' * barras}{Cor.RESET}  "
              f"{Cor.WHITE}{val:>7,}{Cor.RESET}{Cor.GRAY}{estrela}{Cor.RESET}")

# ── Menu interativo ──────────────────────────────────────────────────────────

def menu():
    titulo("DASHBOARD ECONOMICO BRASILEIRO", Cor.CYAN)
    print(f"  {Cor.GRAY}APIs: Banco Central do Brasil (BCB/SGS) — sem chave necessaria{Cor.RESET}")
    print(f"  {Cor.GRAY}IA:   Claude (Anthropic) — requer ANTHROPIC_API_KEY{Cor.RESET}")
    print(f"  {Cor.GRAY}Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}{Cor.RESET}")

    # Inicializa e testa conexao com o banco na abertura
    print()
    init_db()

    while True:
        print(f"\n{Cor.BOLD}  Opcoes:{Cor.RESET}")
        opcoes = [
            ("1", "Buscar indices ao vivo (BCB) e salvar no banco",      Cor.CYAN),
            ("2", "Gerar projecao IA (Claude) e salvar no banco",        Cor.MAGENTA),
            ("3", "Exibir dados do Bolsa Familia em Campinas",           Cor.GREEN),
            ("4", "Executar tudo (indices + projecao + bolsa familia)",  Cor.YELLOW),
            ("5", "Ver historico salvo no banco de dados",               Cor.BLUE),
            ("0", "Sair",                                                 Cor.RED),
        ]
        for num, desc, cor in opcoes:
            print(f"  {cor}{Cor.BOLD}[{num}]{Cor.RESET} {Cor.WHITE}{desc}{Cor.RESET}")

        print(f"\n  {Cor.GRAY}> {Cor.RESET}", end="")
        try:
            escolha = input().strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{Cor.GRAY}  Encerrando...{Cor.RESET}")
            break

        dados_cache = {}

        if escolha == "1":
            titulo("INDICES ECONOMICOS — AO VIVO", Cor.CYAN)
            dados_cache = buscar_todos_indices()   # ja salva no banco internamente
            exibir_kpis(dados_cache)
            exibir_tabela(dados_cache)

        elif escolha == "2":
            titulo("PROJECAO INTELIGENTE", Cor.MAGENTA)
            if not dados_cache:
                print(f"  {Cor.GRAY}Buscando dados historicos primeiro...{Cor.RESET}")
                dados_cache = buscar_todos_indices()
            gerar_projecao(dados_cache)            # ja salva projecoes no banco internamente

        elif escolha == "3":
            exibir_bolsa_familia()

        elif escolha == "4":
            titulo("RELATORIO COMPLETO", Cor.YELLOW)
            dados_cache = buscar_todos_indices()
            exibir_kpis(dados_cache)
            exibir_tabela(dados_cache)
            exibir_bolsa_familia()
            titulo("PROJECAO INTELIGENTE", Cor.MAGENTA)
            gerar_projecao(dados_cache)

        elif escolha == "5":
            titulo("HISTORICO DO BANCO DE DADOS", Cor.BLUE)
            exibir_historico_banco()

        elif escolha == "0":
            print(f"\n{Cor.CYAN}  Ate logo!{Cor.RESET}\n")
            break
        else:
            print(f"  {Cor.RED}Opcao invalida. Escolha 0-5.{Cor.RESET}")

# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        print(f"\n{Cor.GRAY}  Interrompido pelo usuario.{Cor.RESET}\n")
    except Exception as e:
        print(f"\n{Cor.RED}[ERRO CRITICO] {e}{Cor.RESET}")
        traceback.print_exc()
        sys.exit(1)