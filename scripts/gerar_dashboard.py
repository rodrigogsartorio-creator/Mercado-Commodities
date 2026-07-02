#!/usr/bin/env python3
"""
gerar_dashboard.py — Mercado Commodities
Busca cotações CEPEA/ESALQ via agrobr e gera o dashboard HTML diário.

Projetado para rodar no GitHub Actions (internet irrestrita).
Uso local: python3 scripts/gerar_dashboard.py

Sobreposição manual: crie cotacoes_manual.json na raiz do projeto para
fornecer valores que o agrobr não conseguiu buscar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
HISTORICO_PATH = BASE_DIR / "historico.json"
MANUAL_PATH = BASE_DIR / "cotacoes_manual.json"
CONTEXTO_PATH = BASE_DIR / "contexto_manual.json"

# Mapeamento produto → (agrobr_produto, agrobr_praca, unidade_display)
PRODUTOS = {
    "soja":           ("soja",         "paranagua", "R$/sc 60kg — CIF Paranaguá"),
    "trigo":          ("trigo",         None,        "R$/sc 60kg — Paraná (CEPEA)"),
    "cafe_arabica":   ("cafe_arabica",  None,        "R$/sc 60kg — Arábica Tipo 6"),
    "acucar_cristal": ("acucar",        None,        "R$/sc 50kg — ICUMSA 130–180 SP"),
    "arroz":          ("arroz",         None,        "R$/sc 50kg — RS (CEPEA/IRGA-RS)"),
    # feijão não está disponível no agrobr — sempre N/D
}

DIAS_HISTORICO = 30   # quantos dias exibir no gráfico
DIAS_TABELA = 5       # últimos dias úteis na tabela


# ──────────────────────────────────────────────────────────────────────────────
# 1. COLETA DE DADOS
# ──────────────────────────────────────────────────────────────────────────────

async def _fetch_cepea(produto: str, praca: str | None) -> tuple[float | None, float | None, str, str]:
    """
    Retorna (valor_hoje, variacao_pct, status, fonte).
    valor_hoje = None se não disponível.
    variacao_pct = variação percentual em relação ao dia anterior (None se não calculável).
    """
    try:
        import agrobr.cepea as cepea
        from datetime import date as dt_date
        inicio = str(dt_date.today() - timedelta(days=10))
        fim    = str(dt_date.today())
        df = await cepea.indicador(produto, praca=praca, inicio=inicio, fim=fim)
        if df.empty:
            return None, None, "nao_disponivel", "agrobr/cepea"
        # Ordenar por data
        df = df.sort_values("data")
        # Último valor
        ultimo = df.iloc[-1]
        valor = float(ultimo["valor"])
        fonte_raw = str(ultimo.get("fonte", "cepea"))
        status = "confirmado_cepea" if "cepea" in fonte_raw else "referenciado_noticias_agricolas"
        fonte = "CEPEA/ESALQ (agrobr)" if "cepea" in fonte_raw else "Notícias Agrícolas (agrobr/fallback)"
        # Variação percentual vs. dia anterior
        var_pct = None
        if len(df) >= 2:
            anterior = float(df.iloc[-2]["valor"])
            if anterior > 0:
                var_pct = round((valor - anterior) / anterior * 100, 2)
        return valor, var_pct, status, fonte
    except Exception as exc:
        log.warning("agrobr falhou para %s: %s", produto, exc)
        return None, None, "nao_disponivel", "agrobr/cepea"


async def _fetch_dolar() -> tuple[float | None, str]:
    """Retorna (taxa_dolar, fonte)."""
    try:
        import agrobr.bcb as bcb
        hoje_br = date.today().strftime("%d/%m/%Y")
        df = await bcb.ptax(data=hoje_br)
        if df.empty:
            # Tenta os últimos 5 dias caso mercado ainda não tenha publicado hoje
            fim = date.today()
            ini = fim - timedelta(days=5)
            df = await bcb.ptax(
                data_inicial=ini.strftime("%d/%m/%Y"),
                data_final=fim.strftime("%d/%m/%Y"),
            )
        if not df.empty:
            df = df.sort_values("data")
            taxa = round(float(df.iloc[-1]["cotacao_venda"]), 4)
            data_taxa = str(df.iloc[-1]["data"])
            return taxa, f"BCB PTAX venda ({data_taxa})"
    except Exception as exc:
        log.warning("BCB PTAX falhou: %s", exc)
    return None, "indisponível"


async def fetch_cotacoes() -> dict:
    """Busca todas as cotações de forma concorrente."""
    tasks = {
        nome: asyncio.create_task(
            _fetch_cepea(cfg[0], cfg[1])
        )
        for nome, cfg in PRODUTOS.items()
    }
    dolar_task = asyncio.create_task(_fetch_dolar())

    resultados: dict = {}
    for nome, task in tasks.items():
        valor, var_pct, status, fonte = await task
        resultados[nome] = {
            "valor":   valor,
            "var_pct": var_pct,
            "status":  status,
            "fonte":   fonte,
            "unidade": PRODUTOS[nome][2],
        }

    # Feijão: sempre N/D (não suportado pelo agrobr)
    resultados["feijao_carioca"] = {
        "valor":   None,
        "var_pct": None,
        "status":  "nao_disponivel",
        "fonte":   "agrobr não suporta feijão carioca",
        "unidade": "R$/sc 60kg — Nota 8,5 SP",
    }

    dolar_val, dolar_fonte = await dolar_task
    resultados["dolar"] = {"valor": dolar_val, "fonte": dolar_fonte}

    return resultados


def apply_manual_overrides(cotacoes: dict) -> dict:
    """Aplica valores de cotacoes_manual.json (se existir) sobre os dados buscados."""
    if not MANUAL_PATH.exists():
        return cotacoes
    try:
        with MANUAL_PATH.open(encoding="utf-8") as f:
            manual = json.load(f)
        for key, val in manual.items():
            if key in cotacoes and isinstance(val, dict):
                cotacoes[key].update(val)
                if cotacoes[key].get("valor") is not None:
                    cotacoes[key]["status"] = val.get("status", "confirmado_manual")
                log.info("Override manual aplicado: %s", key)
            elif key == "dolar" and isinstance(val, dict):
                cotacoes["dolar"].update(val)
    except Exception as exc:
        log.warning("Erro ao ler cotacoes_manual.json: %s", exc)
    return cotacoes


def load_contexto() -> dict:
    """Lê contexto_manual.json (fatores externos + notícias), se existir."""
    if not CONTEXTO_PATH.exists():
        return {"fatores_externos": [], "noticias": []}
    try:
        with CONTEXTO_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Erro ao ler contexto_manual.json: %s", exc)
        return {"fatores_externos": [], "noticias": []}


# ──────────────────────────────────────────────────────────────────────────────
# 2. HISTÓRICO JSON
# ──────────────────────────────────────────────────────────────────────────────

def load_historico() -> dict:
    if HISTORICO_PATH.exists():
        with HISTORICO_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    return {
        "descricao": "Histórico de cotações de commodities agrícolas — Dashboard Mercado Commodities",
        "atualizacao": str(date.today()),
        "unidades": {
            "soja":           "R$/saca 60kg (CIF Paranaguá — CEPEA/ESALQ)",
            "trigo":          "R$/saca 60kg (Região Sul — CEPEA/ESALQ)",
            "cafe_arabica":   "R$/saca 60kg (Arábica Tipo 6 — CEPEA/ESALQ)",
            "acucar_cristal": "R$/saca 50kg (ICUMSA 130–180 — CEPEA/ESALQ SP)",
            "feijao_carioca": "R$/saca 60kg (Nota 8,5 SP — CEPEA)",
            "arroz":          "R$/saca 50kg (em casca RS — CEPEA/IRGA-RS)",
            "dolar":          "R$/USD (comercial — BCB PTAX)",
        },
        "registros": [],
    }


def build_entry(data_hoje: str, cotacoes: dict) -> dict:
    entry: dict = {"data": data_hoje}
    for nome in ["soja", "trigo", "cafe_arabica", "acucar_cristal", "feijao_carioca", "arroz"]:
        c = cotacoes.get(nome, {})
        entry[nome] = c.get("valor")
        if c.get("var_pct") is not None:
            entry[f"{nome}_var_dia"] = c["var_pct"]
    dolar = cotacoes.get("dolar", {})
    entry["dolar"] = dolar.get("valor")
    entry["dolar_fonte"] = dolar.get("fonte", "")
    # Status por commodity
    entry["status"] = {
        nome: cotacoes.get(nome, {}).get("status", "nao_disponivel")
        for nome in ["soja", "trigo", "cafe_arabica", "acucar_cristal", "feijao_carioca", "arroz", "dolar"]
    }
    if "dolar" in entry["status"]:
        dolar_val = cotacoes.get("dolar", {}).get("valor")
        dolar_fonte_raw = (cotacoes.get("dolar", {}).get("fonte") or "").lower()
        if not dolar_val:
            entry["status"]["dolar"] = "nao_disponivel"
        elif "bcb" in dolar_fonte_raw or "ptax" in dolar_fonte_raw:
            entry["status"]["dolar"] = "bcb_ptax"
        else:
            entry["status"]["dolar"] = "fonte_alternativa"
    return entry


def update_historico(historico: dict, data_hoje: str, entry: dict) -> dict:
    registros = historico.get("registros", [])
    registros = [r for r in registros if r.get("data") != data_hoje]
    registros.append(entry)
    registros = sorted(registros, key=lambda r: r["data"])[-DIAS_HISTORICO:]
    historico["registros"] = registros
    historico["atualizacao"] = data_hoje
    return historico


def save_historico(historico: dict) -> None:
    with HISTORICO_PATH.open("w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────────────────────────────────────
# 3. GERAÇÃO DO HTML
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_brl(val: float | None, decimais: int = 2) -> str:
    if val is None:
        return "—"
    return f"{val:,.{decimais}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_var(var: float | None) -> tuple[str, str]:
    """Retorna (classe_css, texto_variação)."""
    if var is None:
        return "estavel", "— —"
    if var > 0.05:
        return "alta", f"▲ +{_fmt_brl(var, 2)}%"
    if var < -0.05:
        return "baixa", f"▼ {_fmt_brl(var, 2)}%"
    return "estavel", f"≈ {_fmt_brl(var, 2)}%"


def _status_badge(status: str | None) -> str:
    if not status or "nao_disponivel" in status:
        return '<span class="badge-confirmado b-nd">N/D — CEPEA INACESSÍVEL</span>'
    if "confirmado_cepea" in status:
        return '<span class="badge-confirmado b-confirmado">✓ CONFIRMADO CEPEA</span>'
    if "noticias_agricolas" in status:
        return '<span class="badge-confirmado b-referenciado">† REFERENCIADO (Notícias Agrícolas)</span>'
    if "manual" in status:
        return '<span class="badge-confirmado b-referenciado">† FORNECIDO MANUALMENTE</span>'
    return '<span class="badge-confirmado b-referenciado">† REFERENCIADO</span>'


def _card(
    nome: str,
    emoji: str,
    preco_fmt: str,
    unidade: str,
    classe_card: str,
    classe_var: str,
    texto_var: str,
    badge_html: str,
    info: str,
    ref: str,
    nd: bool = False,
) -> str:
    if nd:
        return f"""
    <div class="card nd">
      {badge_html}
      <div class="card-nome">{emoji} {nome}</div>
      <div class="card-preco">Dado não disponível</div>
      <div class="card-unidade">{unidade}</div>
      <div class="card-variacao">— —</div>
      <div class="card-info">{info}</div>
      <div class="card-ref">{ref}</div>
    </div>"""
    return f"""
    <div class="card {classe_card}">
      {badge_html}
      <div class="card-nome">{emoji} {nome}</div>
      <div class="card-preco">{preco_fmt}</div>
      <div class="card-unidade">{unidade}</div>
      <div class="card-variacao {classe_var}">{texto_var}</div>
      <div class="card-info">{info}</div>
      <div class="card-ref">{ref}</div>
    </div>"""


def _tabela_row(reg: dict) -> str:
    data_str = reg.get("data", "")
    try:
        dt = datetime.strptime(data_str, "%Y-%m-%d")
        dias_pt = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
        label = f"{dias_pt[dt.weekday()]} {dt.day:02d}/{dt.month:02d}"
    except Exception:
        label = data_str

    def td(val, var=None, nd=False, ref=False):
        if nd or val is None:
            return '<td class="td-nd">N/D</td>'
        fmt = _fmt_brl(val)
        if ref:
            return f'<td class="td-ref">{fmt} †</td>'
        if var is not None and var > 0.05:
            return f'<td class="td-alta">{fmt} ▲</td>'
        if var is not None and var < -0.05:
            return f'<td class="td-baixa">{fmt} ▼</td>'
        return f"<td>{fmt}</td>"

    status = reg.get("status", {})
    soja_ref = "noticias_agricolas" in status.get("soja", "") or "ref" in status.get("soja", "")
    cafe_ref = "noticias" in status.get("cafe_arabica", "") or "ref" in status.get("cafe_arabica", "")

    return f"""        <tr>
          <td><strong>{label}</strong></td>
          {td(reg.get("soja"), reg.get("soja_var_dia"), ref=soja_ref)}
          {td(reg.get("trigo"), nd=reg.get("trigo") is None)}
          {td(reg.get("cafe_arabica"), ref=cafe_ref, nd=reg.get("cafe_arabica") is None)}
          {td(reg.get("acucar_cristal"), nd=reg.get("acucar_cristal") is None)}
          {td(reg.get("feijao_carioca"), nd=True)}
          {td(reg.get("arroz"), nd=reg.get("arroz") is None)}
          <td>{_fmt_brl(reg.get("dolar"), 2) if reg.get("dolar") else "N/D"} {"*" if reg.get("dolar") else ""}</td>
        </tr>"""


def _chart_data(historico: dict, campo: str) -> str:
    """Retorna lista JS de valores para o gráfico de 30 dias."""
    regs = historico.get("registros", [])[-DIAS_HISTORICO:]
    vals = [str(r.get(campo)) if r.get(campo) is not None else "null" for r in regs]
    return f"[{', '.join(vals)}]"


def _chart_labels(historico: dict) -> str:
    regs = historico.get("registros", [])[-DIAS_HISTORICO:]
    labels = []
    for r in regs:
        try:
            dt = datetime.strptime(r["data"], "%Y-%m-%d")
            labels.append(f'"{dt.day:02d}/{dt.month:02d}"')
        except Exception:
            labels.append(f'"{r.get("data","")}"')
    return f"[{', '.join(labels)}]"


def _has_data(historico: dict, campo: str) -> bool:
    return any(r.get(campo) is not None for r in historico.get("registros", []))


def _dia_semana(dt: date) -> str:
    dias = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira",
            "Sexta-feira", "Sábado", "Domingo"]
    return dias[dt.weekday()]


def _mercado_status(dt: date) -> str:
    if dt.weekday() >= 5:
        return "🔒 Fim de Semana — Mercado Fechado"
    return "📅 Mercado Aberto"


def _fatores_externos_html(fatores: list) -> str:
    if not fatores:
        return """
    <div class="fator-card nd">
      <div class="fator-titulo">— Sem dados —</div>
      <div class="fator-texto">Nenhum fator externo fornecido nesta geração (contexto_manual.json ausente ou vazio).</div>
    </div>"""
    blocos = []
    for f in fatores:
        blocos.append(f"""
    <div class="fator-card">
      <div class="fator-titulo">{f.get('emoji','')} {f.get('titulo','')}</div>
      <div class="fator-texto">{f.get('texto','')}</div>
    </div>""")
    return "".join(blocos)


def _noticias_html(noticias: list) -> str:
    if not noticias:
        return """
    <div class="noticia-card nd">
      <div class="noticia-titulo">— Sem notícias —</div>
      <div class="noticia-texto">Nenhuma notícia fornecida nesta geração (contexto_manual.json ausente ou vazio).</div>
    </div>"""
    blocos = []
    for n in noticias:
        url = n.get("url", "")
        fonte = n.get("fonte", "")
        data_n = n.get("data", "")
        link = f'<a href="{url}" target="_blank" rel="noopener">{fonte}</a>' if url else fonte
        blocos.append(f"""
    <div class="noticia-card">
      <div class="noticia-titulo">{n.get('titulo','')}</div>
      <div class="noticia-texto">{n.get('resumo','')}</div>
      <div class="noticia-fonte">Fonte: {link} · {data_n}</div>
    </div>""")
    return "".join(blocos)


def generate_html(cotacoes: dict, historico: dict, data_hoje_str: str, contexto: dict | None = None) -> str:
    contexto = contexto or {"fatores_externos": [], "noticias": []}
    dt = datetime.strptime(data_hoje_str, "%Y-%m-%d")
    data_fmt = f"{dt.day:02d} de {['janeiro','fevereiro','março','abril','maio','junho','julho','agosto','setembro','outubro','novembro','dezembro'][dt.month-1]} de {dt.year}"
    hora_geracao = datetime.now().strftime("%H:%M")
    dia_semana = _dia_semana(dt.date())
    mercado_status = _mercado_status(dt.date())

    # Dólar
    dolar_val = cotacoes.get("dolar", {}).get("valor")
    dolar_fmt = f"R$ {_fmt_brl(dolar_val)}" if dolar_val else "N/D"
    dolar_fonte_display = cotacoes.get("dolar", {}).get("fonte", "")

    # Última referência disponível
    regs = historico.get("registros", [])
    ultima_ref = ""
    if regs:
        try:
            ul = datetime.strptime(regs[-1]["data"], "%Y-%m-%d")
            dias_pt = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
            ultima_ref = f"ref. {dias_pt[ul.weekday()]}. {ul.day:02d}/{ul.month:02d}/{ul.year}"
        except Exception:
            ultima_ref = regs[-1].get("data", "")

    # ── CARDS ──
    def make_card(chave, emoji, nome, unidade_short):
        c = cotacoes.get(chave, {})
        val = c.get("valor")
        var = c.get("var_pct")
        status = c.get("status", "nao_disponivel")
        fonte = c.get("fonte", "")
        badge = _status_badge(status)
        if val is None:
            return _card(nome, emoji, "—", unidade_short, "nd", "nd", "— —", badge,
                         f"Indicador CEPEA inacessível nesta geração. Acesse: cepea.org.br",
                         f"Fonte primária indisponível · {dt.strftime('%d/%m/%Y')}", nd=True)
        cls, txt_var = _fmt_var(var)
        return _card(nome, emoji, f"R$ {_fmt_brl(val)}", unidade_short,
                     cls, cls, txt_var, badge,
                     f"Fonte: {fonte}",
                     f"CEPEA/ESALQ · {data_hoje_str}")

    card_soja   = make_card("soja",           "🌱", "Soja",           "saca 60 kg | CIF Paranaguá")
    card_trigo  = make_card("trigo",          "🌾", "Trigo",          "saca 60 kg | Paraná")
    card_cafe   = make_card("cafe_arabica",   "☕", "Café Arábica",   "saca 60 kg | Arábica Tipo 6")
    card_acucar = make_card("acucar_cristal", "🍬", "Açúcar Cristal", "saca 50 kg | ICUMSA 130–180 SP")
    # Feijão sempre N/D
    card_feijao = _card("Feijão Carioca", "🫘", "—", "saca 60 kg | São Paulo",
                        "nd", "nd", "— —",
                        '<span class="badge-confirmado b-nd">N/D — não suportado</span>',
                        "Feijão carioca não está disponível no agrobr. Acesse: cepea.org.br/br/indicador/feijao.aspx",
                        f"Fonte primária indisponível · {dt.strftime('%d/%m/%Y')}", nd=True)
    card_arroz  = make_card("arroz", "🍚", "Arroz", "saca 50 kg | RS (CEPEA/IRGA-RS)")

    # ── TABELA ──
    ultimos_regs = regs[-DIAS_TABELA:]
    rows_tabela = "\n".join(_tabela_row(r) for r in reversed(ultimos_regs))

    # ── GRÁFICOS ──
    labels_js    = _chart_labels(historico)
    soja_data    = _chart_data(historico, "soja")
    cafe_data    = _chart_data(historico, "cafe_arabica")
    acucar_data  = _chart_data(historico, "acucar_cristal")
    trigo_data   = _chart_data(historico, "trigo")
    arroz_data   = _chart_data(historico, "arroz")

    def chart_block(chart_id, titulo, color, data_js, nota):
        has = any(v != "null" for v in data_js.strip("[]").split(","))
        if not has:
            return f"""
    <div class="chart-card">
      <div class="chart-title">{titulo}</div>
      <div class="chart-nd">
        <span>Dado não disponível — CEPEA inacessível</span>
      </div>
    </div>"""
        return f"""
    <div class="chart-card">
      <div class="chart-title">{titulo}</div>
      <div class="chart-container"><canvas id="{chart_id}"></canvas></div>
      <div class="chart-nota">{nota}</div>
    </div>"""

    chart_soja   = chart_block("chartSoja",   "🌱 Soja — R$/sc 60kg · CIF Paranaguá (CEPEA ✓)", "#27AE60", soja_data,   "Dados CEPEA/ESALQ confirmados via agrobr.")
    chart_cafe   = chart_block("chartCafe",   "☕ Café Arábica — R$/sc 60kg (CEPEA)",           "#795548", cafe_data,   "Dados CEPEA/ESALQ via agrobr.")
    chart_acucar = chart_block("chartAcucar", "🍬 Açúcar Cristal — R$/sc 50kg · CEPEA SP",      "#F39C12", acucar_data, "Dados CEPEA/ESALQ via agrobr.")
    chart_trigo  = chart_block("chartTrigo",  "🌾 Trigo — R$/sc 60kg · Paraná (CEPEA)",          "#8D6E63", trigo_data,  "Dados CEPEA/ESALQ via agrobr.")
    chart_arroz  = chart_block("chartArroz",  "🍚 Arroz — R$/sc 50kg · RS (CEPEA/IRGA-RS)",     "#3498DB", arroz_data,  "Dados CEPEA/IRGA-RS via agrobr.")

    # ── INSIGHTS AUTOMÁTICOS ──
    def insight(chave, nome, emoji):
        c = cotacoes.get(chave, {})
        val = c.get("valor")
        var = c.get("var_pct")
        if val is None:
            return f"""
    <div class="insight-card nd">
      <div class="insight-tipo">— Dado Indisponível — {emoji} {nome}</div>
      <div class="insight-texto">Não foi possível obter cotação do {nome} nesta geração.
        Acesse cepea.org.br para dados atualizados.</div>
    </div>"""
        cls_map = {"alta": "insight-card", "baixa": "insight-card alerta", "estavel": "insight-card atencao"}
        cls_card, txt_var = _fmt_var(var)
        card_cls = cls_map.get(cls_card, "insight-card atencao")
        tipo_map = {"alta": f"▲ Alta — {emoji} {nome}", "baixa": f"▼ Baixa — {emoji} {nome}", "estavel": f"≈ Estável — {emoji} {nome}"}
        tipo = tipo_map.get(cls_card, nome)
        return f"""
    <div class="{card_cls}">
      <div class="insight-tipo">{tipo}</div>
      <div class="insight-texto">Último indicador CEPEA/ESALQ: <strong>R$ {_fmt_brl(val)}</strong>
        {f"({txt_var} vs. dia anterior)" if var is not None else "(variação não disponível)"}.
        Fonte: {c.get("fonte", "CEPEA/agrobr")}.</div>
    </div>"""

    insights = (
        insight("soja",           "Soja",           "🌱") +
        insight("cafe_arabica",   "Café Arábica",   "☕") +
        insight("trigo",          "Trigo",          "🌾") +
        insight("acucar_cristal", "Açúcar Cristal", "🍬") +
        insight("arroz",          "Arroz",          "🍚")
    )

    # Commodities confirmadas
    confirmadas = [
        nome for nome, chave in [
            ("Soja", "soja"), ("Trigo", "trigo"), ("Café", "cafe_arabica"),
            ("Açúcar", "acucar_cristal"), ("Arroz", "arroz")
        ]
        if cotacoes.get(chave, {}).get("valor") is not None
    ]
    nd_lista = [
        nome for nome, chave in [
            ("Soja", "soja"), ("Trigo", "trigo"), ("Café", "cafe_arabica"),
            ("Açúcar", "acucar_cristal"), ("Feijão", "feijao_carioca"), ("Arroz", "arroz")
        ]
        if cotacoes.get(chave, {}).get("valor") is None
    ]
    confirmadas_str = ", ".join(confirmadas) if confirmadas else "nenhuma"
    nd_str = ", ".join(nd_lista) if nd_lista else "nenhuma"

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard de Commodities Agrícolas — {dt.strftime('%d/%m/%Y')}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {{
    --verde: #27AE60; --vermelho: #E74C3C; --amarelo: #F39C12;
    --cinza-escuro: #2C3E50; --cinza-medio: #7F8C8D; --cinza-claro: #ECF0F1;
    --branco: #FFFFFF; --fundo: #F4F6F7;
    --card-shadow: 0 2px 12px rgba(0,0,0,0.09);
    --nd-bg: #F8F9FA; --nd-border: #BDC3C7;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: var(--fundo); color: var(--cinza-escuro); }}
  header {{ background: linear-gradient(135deg, #1a5c35 0%, #27AE60 100%); color: white; padding: 24px 32px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }}
  header h1 {{ font-size: 1.6rem; font-weight: 700; letter-spacing: -0.5px; }}
  header h1 span {{ opacity: 0.75; font-size: 1rem; font-weight: 400; display: block; }}
  .header-meta {{ text-align: right; }}
  .header-meta .data {{ font-size: 1.1rem; font-weight: 600; }}
  .header-meta .hora {{ font-size: 0.85rem; opacity: 0.8; }}
  .dolar-badge {{ background: rgba(255,255,255,0.2); border-radius: 8px; padding: 8px 16px; font-size: 0.9rem; }}
  .dolar-badge strong {{ font-size: 1.1rem; }}
  .mercado-badge {{ background: rgba(255,255,255,0.15); border-radius: 8px; padding: 6px 14px; font-size: 0.8rem; border: 1px solid rgba(255,255,255,0.3); }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px 16px; }}
  .aviso-auditoria {{ background: #fef9e7; border-left: 5px solid var(--amarelo); border-radius: 8px; padding: 14px 18px; margin-bottom: 24px; font-size: 0.83rem; color: #6e4c00; line-height: 1.6; }}
  .aviso-auditoria strong {{ color: #5d3a00; }}
  .section-title {{ font-size: 1.05rem; font-weight: 700; color: var(--cinza-escuro); margin-bottom: 16px; padding-bottom: 8px; border-bottom: 2px solid var(--verde); display: flex; align-items: center; gap: 8px; }}
  .section-title .icon {{ font-size: 1.2rem; }}
  .cards-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 16px; margin-bottom: 32px; }}
  .card {{ background: var(--branco); border-radius: 12px; padding: 20px 18px; box-shadow: var(--card-shadow); border-top: 4px solid var(--cinza-claro); transition: transform 0.15s; }}
  .card:hover {{ transform: translateY(-2px); }}
  .card.alta {{ border-top-color: var(--verde); }}
  .card.baixa {{ border-top-color: var(--vermelho); }}
  .card.estavel {{ border-top-color: var(--amarelo); }}
  .card.nd {{ border-top-color: var(--nd-border); background: var(--nd-bg); }}
  .card-nome {{ font-size: 0.75rem; font-weight: 600; text-transform: uppercase; color: var(--cinza-medio); letter-spacing: 0.5px; margin-bottom: 6px; }}
  .card-preco {{ font-size: 1.45rem; font-weight: 700; color: var(--cinza-escuro); margin-bottom: 4px; }}
  .card.nd .card-preco {{ font-size: 1.1rem; color: var(--nd-border); }}
  .card-unidade {{ font-size: 0.7rem; color: var(--cinza-medio); margin-bottom: 10px; }}
  .card-variacao {{ display: inline-flex; align-items: center; gap: 4px; font-size: 0.85rem; font-weight: 700; padding: 3px 10px; border-radius: 20px; }}
  .alta .card-variacao {{ background: #d4efdf; color: var(--verde); }}
  .baixa .card-variacao {{ background: #fadbd8; color: var(--vermelho); }}
  .estavel .card-variacao {{ background: #fdebd0; color: var(--amarelo); }}
  .nd .card-variacao {{ background: #eaecee; color: var(--cinza-medio); }}
  .card-info {{ font-size: 0.72rem; color: var(--cinza-medio); margin-top: 10px; line-height: 1.4; }}
  .card-ref {{ font-size: 0.68rem; color: #b0b8c1; margin-top: 6px; }}
  .badge-confirmado {{ display: inline-block; font-size: 0.65rem; font-weight: 700; padding: 2px 7px; border-radius: 8px; margin-bottom: 4px; }}
  .b-confirmado {{ background: #d4efdf; color: var(--verde); }}
  .b-referenciado {{ background: #fdebd0; color: var(--amarelo); }}
  .b-nd {{ background: #eaecee; color: var(--cinza-medio); }}
  .charts-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 20px; margin-bottom: 32px; }}
  .chart-card {{ background: var(--branco); border-radius: 12px; padding: 20px; box-shadow: var(--card-shadow); }}
  .chart-title {{ font-size: 0.85rem; font-weight: 700; margin-bottom: 14px; color: var(--cinza-escuro); }}
  .chart-container {{ position: relative; height: 160px; }}
  .chart-nd {{ height: 160px; display: flex; align-items: center; justify-content: center; background: var(--nd-bg); border-radius: 8px; border: 1px dashed var(--nd-border); color: var(--cinza-medio); font-size: 0.8rem; text-align: center; }}
  .chart-nota {{ font-size: 0.67rem; color: #c0c8d0; margin-top: 8px; }}
  .table-wrap {{ overflow-x: auto; margin-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--branco); border-radius: 12px; overflow: hidden; box-shadow: var(--card-shadow); font-size: 0.85rem; }}
  th {{ background: var(--cinza-escuro); color: white; padding: 12px 14px; text-align: center; font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.4px; }}
  td {{ padding: 10px 12px; text-align: center; border-bottom: 1px solid var(--cinza-claro); }}
  tr:last-child td {{ border-bottom: none; }}
  tr:nth-child(even) td {{ background: #fafbfc; }}
  .td-alta {{ color: var(--verde); font-weight: 700; }}
  .td-baixa {{ color: var(--vermelho); font-weight: 700; }}
  .td-nd {{ color: #c0c8d0; font-size: 0.8rem; }}
  .td-ref {{ color: var(--amarelo); font-size: 0.78rem; }}
  .insights-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; margin-bottom: 32px; }}
  .insight-card {{ background: var(--branco); border-radius: 12px; padding: 18px; box-shadow: var(--card-shadow); border-left: 5px solid var(--verde); }}
  .insight-card.alerta {{ border-left-color: var(--vermelho); }}
  .insight-card.atencao {{ border-left-color: var(--amarelo); }}
  .insight-card.nd {{ border-left-color: var(--nd-border); background: var(--nd-bg); }}
  .insight-tipo {{ font-size: 0.72rem; font-weight: 700; text-transform: uppercase; margin-bottom: 6px; color: var(--verde); }}
  .insight-card.alerta .insight-tipo {{ color: var(--vermelho); }}
  .insight-card.atencao .insight-tipo {{ color: var(--amarelo); }}
  .insight-card.nd .insight-tipo {{ color: var(--cinza-medio); }}
  .insight-texto {{ font-size: 0.85rem; line-height: 1.55; }}
  .metodologia {{ background: var(--branco); border-radius: 12px; padding: 20px; box-shadow: var(--card-shadow); margin-bottom: 24px; font-size: 0.8rem; line-height: 1.7; color: var(--cinza-medio); }}
  .metodologia strong {{ color: var(--cinza-escuro); }}
  .fatores-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; margin-bottom: 32px; }}
  .fator-card {{ background: var(--branco); border-radius: 12px; padding: 18px; box-shadow: var(--card-shadow); border-left: 5px solid #3498DB; }}
  .fator-card.nd {{ border-left-color: var(--nd-border); background: var(--nd-bg); }}
  .fator-titulo {{ font-size: 0.85rem; font-weight: 700; margin-bottom: 8px; color: var(--cinza-escuro); }}
  .fator-texto {{ font-size: 0.82rem; line-height: 1.6; color: var(--cinza-medio); }}
  .noticias-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; margin-bottom: 32px; }}
  .noticia-card {{ background: var(--branco); border-radius: 12px; padding: 18px; box-shadow: var(--card-shadow); border-left: 5px solid var(--cinza-medio); }}
  .noticia-card.nd {{ border-left-color: var(--nd-border); background: var(--nd-bg); }}
  .noticia-titulo {{ font-size: 0.85rem; font-weight: 700; margin-bottom: 8px; color: var(--cinza-escuro); }}
  .noticia-texto {{ font-size: 0.82rem; line-height: 1.6; margin-bottom: 8px; }}
  .noticia-fonte {{ font-size: 0.7rem; color: #b0b8c1; }}
  .noticia-fonte a {{ color: #3498DB; }}
  footer {{ background: var(--cinza-escuro); color: rgba(255,255,255,0.6); text-align: center; padding: 16px; font-size: 0.78rem; }}
  @media (max-width: 600px) {{ header {{ flex-direction: column; }} .header-meta {{ text-align: left; }} .cards-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
</style>
</head>
<body>

<header>
  <div>
    <h1>🌾 Dashboard de Commodities Agrícolas<span>Análise de Mercado — CEPEA / Agrolink / Notícias Agrícolas</span></h1>
  </div>
  <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap;">
    <div class="mercado-badge">{mercado_status}</div>
    <div class="dolar-badge">💵 Dólar: <strong>{dolar_fmt}</strong><span style="font-size:0.75rem;opacity:0.8;"> {dolar_fonte_display[:30]}</span></div>
    <div class="header-meta">
      <div class="data">{data_fmt}</div>
      <div class="hora">Gerado às {hora_geracao} (Horário de Brasília)</div>
    </div>
  </div>
</header>

<div class="container">

  <div class="aviso-auditoria">
    <strong>📊 Dashboard gerado automaticamente em {dt.strftime('%d/%m/%Y')} às {hora_geracao} BRT.</strong>
    Cotações buscadas via <strong>agrobr</strong> (CEPEA/ESALQ). {dia_semana}.
    Confirmados: <strong>{confirmadas_str}</strong>.
    Indisponível (N/D): <strong>{nd_str}</strong>.
    Para dados exatos e em tempo real: <a href="https://www.cepea.org.br" target="_blank" style="color:#b07a00">cepea.org.br</a>.
    Feijão carioca não é suportado pelo agrobr — acesse CEPEA diretamente.
  </div>

  <div class="section-title"><span class="icon">🍊</span> Cotações — Última Referência Disponível ({ultima_ref})</div>
  <div class="cards-grid">
    {card_soja}
    {card_trigo}
    {card_cafe}
    {card_acucar}
    {card_feijao}
    {card_arroz}
  </div>

  <div class="section-title"><span class="icon">📅</span> Histórico — Últimos {DIAS_TABELA} Dias Úteis</div>
  <div class="table-wrap" style="margin-bottom:8px;">
    <table>
      <thead>
        <tr>
          <th>Data</th>
          <th>Soja (R$/sc 60kg)</th>
          <th>Trigo (R$/sc)</th>
          <th>Café (R$/sc 60kg)</th>
          <th>Açúcar (R$/sc 50kg)</th>
          <th>Feijão (R$/sc)</th>
          <th>Arroz (R$/sc)</th>
          <th>Dólar (R$/USD)</th>
        </tr>
      </thead>
      <tbody>
{rows_tabela}
      </tbody>
    </table>
  </div>
  <p style="font-size:0.72rem;color:#aaa;margin-bottom:32px;">
    ✓ Confirmado CEPEA/ESALQ via agrobr · † Referenciado (Notícias Agrícolas/fallback) · * BCB PTAX · N/D = não disponível
  </p>

  <div class="section-title"><span class="icon">📈</span> Tendências — Série Histórica</div>
  <div class="charts-grid">
    {chart_soja}
    {chart_cafe}
    {chart_acucar}
    {chart_trigo}
    {chart_arroz}
    <div class="chart-card">
      <div class="chart-title">🫘 Feijão Carioca <span style="color:var(--nd-border)">[N/D]</span></div>
      <div class="chart-nd">
        <span>Não suportado pelo agrobr<br><small>Acesse: cepea.org.br/br/indicador/feijao.aspx</small></span>
      </div>
    </div>
  </div>

  <div class="section-title"><span class="icon">🌐</span> Fatores Externos</div>
  <div class="fatores-grid">
    {_fatores_externos_html(contexto.get("fatores_externos", []))}
  </div>

  <div class="section-title"><span class="icon">💡</span> Leitura das Tendências</div>
  <p style="font-size:0.75rem;color:var(--cinza-medio);margin-bottom:14px;">
    Esta seção descreve o que os dados indicam sobre o comportamento recente de cada mercado.
    Não constitui recomendação de compra, venda ou qualquer decisão de investimento.
  </p>
  <div class="insights-grid">
    {insights}
    <div class="insight-card nd">
      <div class="insight-tipo">— Dado Indisponível — 🫘 Feijão Carioca</div>
      <div class="insight-texto">Feijão carioca não está disponível no agrobr.
        Acesse <strong>cepea.org.br/br/indicador/feijao.aspx</strong> para dados atualizados.
        Contexto: mai/26 atingiu pico de R$ 415–491/sc antes de recuar.</div>
    </div>
    <div class="insight-card atencao">
      <div class="insight-tipo">📋 Nota Metodológica</div>
      <div class="insight-texto">Dados gerados automaticamente via <strong>agrobr</strong>
        que acessa CEPEA/ESALQ e Notícias Agrícolas (fallback).
        Valores com badge verde são confirmados direto da tabela CEPEA.
        Badge amarelo = fallback Notícias Agrícolas. N/D = fonte inacessível.
        Nunca invente números — marque N/D se não confirmado.</div>
    </div>
  </div>

  <div class="section-title"><span class="icon">📰</span> Notícias Relevantes (Últimos 2 Dias)</div>
  <div class="noticias-grid">
    {_noticias_html(contexto.get("noticias", []))}
  </div>

  <div class="section-title"><span class="icon">📋</span> Metodologia & Fontes</div>
  <div class="metodologia">
    <strong>Geração automática:</strong> Dashboard gerado via script Python (agrobr) em {dt.strftime('%d/%m/%Y')} às {hora_geracao} BRT.
    O agrobr busca dados em CEPEA/ESALQ (fonte primária) com fallback para Notícias Agrícolas quando CEPEA retorna erro.<br><br>

    <strong>Confirmados ({confirmadas_str}):</strong>
    Indicadores CEPEA/ESALQ obtidos via tabela oficial e parsing via agrobr.<br><br>

    <strong>Não disponíveis ({nd_str}):</strong>
    Feijão carioca não está na lista de produtos suportados pelo agrobr.
    Demais N/D: CEPEA retornou erro na coleta.<br><br>

    <strong>Câmbio:</strong> {dolar_fmt} — {dolar_fonte_display}.<br><br>

    <strong>Referências de metodologia dos indicadores:</strong>
    Soja = CIF Paranaguá (PR) · Café = Arábica Tipo 6 posto SP · Açúcar = Cristal ICUMSA 130–180 posto SP ·
    Trigo = Tipo 1 Pão Região Sul · Feijão = Carioca Nota 8,5 SP · Arroz = Em Casca RS sc/50kg — CEPEA/IRGA-RS<br><br>

    <strong>Fontes:</strong> CEPEA/Esalq–USP (cepea.org.br) · Notícias Agrícolas · BCB PTAX · agrobr (github.com/bruno-portfolio/agrobr)<br><br>

    <strong>Disclaimer:</strong> Relatório com finalidade exclusivamente informativa.
    Não constitui recomendação de compra ou venda.
    Consulte fontes primárias (cepea.org.br) e especialistas antes de tomar decisões comerciais.
  </div>

</div>

<footer>
  Dashboard de Commodities Agrícolas © {dt.year} — Gerado automaticamente às {hora_geracao} BRT · {dt.strftime('%d/%m/%Y')} ({dia_semana}) · Dados: CEPEA/ESALQ via agrobr
</footer>

<script>
const LABELS = {labels_js};
const SOJA   = {soja_data};
const CAFE   = {cafe_data};
const ACUCAR = {acucar_data};
const TRIGO  = {trigo_data};
const ARROZ  = {arroz_data};

function makeChart(id, label, color, data) {{
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const hasData = data.some(v => v !== null);
  if (!hasData) return;
  new Chart(canvas, {{
    type: 'line',
    data: {{
      labels: LABELS,
      datasets: [{{ label, data, borderColor: color, backgroundColor: color + '18',
        fill: true, tension: 0.35, pointRadius: 2, borderWidth: 2.5 }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 10 }}, maxTicksLimit: 6 }} }},
        y: {{ grid: {{ color: '#f0f0f0' }}, ticks: {{ font: {{ size: 10 }} }} }}
      }}
    }}
  }});
}}

makeChart('chartSoja',   'Soja',           '#27AE60', SOJA);
makeChart('chartCafe',   'Café',           '#795548', CAFE);
makeChart('chartAcucar', 'Açúcar',         '#F39C12', ACUCAR);
makeChart('chartTrigo',  'Trigo',          '#8D6E63', TRIGO);
makeChart('chartArroz',  'Arroz',          '#3498DB', ARROZ);
</script>
</body>
</html>"""
    return html


# ──────────────────────────────────────────────────────────────────────────────
# 4. MAIN
# ──────────────────────────────────────────────────────────────────────────────

async def _main_async():
    data_hoje = str(date.today())
    log.info("=== Mercado Commodities — %s ===", data_hoje)

    # 1. Fetch
    log.info("Buscando cotações via agrobr...")
    cotacoes = await fetch_cotacoes()

    # 2. Override manual
    cotacoes = apply_manual_overrides(cotacoes)

    # 3. Historico
    historico = load_historico()
    entry = build_entry(data_hoje, cotacoes)
    historico = update_historico(historico, data_hoje, entry)
    save_historico(historico)
    log.info("historico.json atualizado.")

    # 4. HTML
    contexto = load_contexto()
    html = generate_html(cotacoes, historico, data_hoje, contexto)
    html_path = BASE_DIR / f"commodities_{data_hoje}.html"
    index_path = BASE_DIR / "index.html"
    html_path.write_text(html, encoding="utf-8")
    index_path.write_text(html, encoding="utf-8")
    log.info("Dashboard salvo: %s", html_path.name)

    # 5. Resumo
    log.info("=== Resumo das Cotações ===")
    for nome, c in cotacoes.items():
        if nome == "dolar":
            log.info("  dolar: %s (%s)", c.get("valor"), c.get("fonte"))
        else:
            log.info("  %s: %s — %s", nome, c.get("valor"), c.get("status"))


def main():
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
