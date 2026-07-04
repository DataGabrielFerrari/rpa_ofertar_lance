"""
Relatorio interno de execucao — enviado para rpa.ademicon@gmail.com.

NAO vai para o ADM/cliente. E um relatorio operacional completo para
acompanhamento interno do RPA, contendo:

  - Resumo geral (total, ofertadas, nao ofertadas, erros, taxa de sucesso)
  - Duracao do lote
  - Breakdown de erros por categoria com grafico matplotlib
  - Breakdown por consultor (ofertadas x nao ofertadas x erros)
  - Lista detalhada de cada categoria de erro
  - Lista de cotas para REEXECUTAR (safeguard — tentativas >= 3, FALHA)
"""

import base64
import io
import os
import re
import traceback
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from credenciais.google_auth import criar_servico_gmail
from db.db import fetchall, fetchone

EMAIL_RELATORIO_INTERNO = os.getenv(
    "EMAIL_RELATORIO_INTERNO",
    "rpa.ademicon@gmail.com",
)

CATEGORIAS_ORDEM = [
    "Erro de Login",
    "Cota Indisponivel",
    "TA Negativo",
    "Valor a Pagar",
    "3 Tentativas (Safeguard)",
    "Outros Erros",
    "Outros NAO_OFERTADO",
]

CORES_GRAF = {
    "Erro de Login":            "#C62828",
    "Cota Indisponivel":        "#EF6C00",
    "TA Negativo":              "#F9A825",
    "Valor a Pagar":            "#558B2F",
    "3 Tentativas (Safeguard)": "#6A1B9A",
    "Outros Erros":             "#37474F",
    "Outros NAO_OFERTADO":      "#78909C",
}

CORES_HTML = {
    "Erro de Login":            "#C62828",
    "Cota Indisponivel":        "#EF6C00",
    "TA Negativo":              "#F57F17",
    "Valor a Pagar":            "#2E7D32",
    "3 Tentativas (Safeguard)": "#6A1B9A",
    "Outros Erros":             "#37474F",
    "Outros NAO_OFERTADO":      "#546E7A",
}


# =========================================================
# QUERIES
# =========================================================

def _resumo_lote(id_fila_adm: int) -> dict:
    # mes_ref e derivado de hora_inicio (a coluna mes_ref nao existe em tbl_fila_adm)
    sql = """
        SELECT
            a.nome,
            fa.modalidade,
            TO_CHAR(COALESCE(fa.hora_inicio, fa.hora_criado), 'YYYYMM')::integer AS mes_ref,
            fa.hora_inicio,
            fa.hora_fim,
            COALESCE(fa.total_cotas, 0)                                                     AS total,
            COALESCE(SUM(CASE WHEN fc.status = 'OFERTADO'     THEN 1 ELSE 0 END), 0)       AS ofertadas,
            COALESCE(SUM(CASE WHEN fc.status = 'NAO_OFERTADO' THEN 1 ELSE 0 END), 0)       AS nao_ofertadas,
            COALESCE(SUM(CASE WHEN fc.status = 'FALHA'        THEN 1 ELSE 0 END), 0)       AS erros,
            COALESCE(SUM(CASE WHEN fc.status = 'PENDENTE'     THEN 1 ELSE 0 END), 0)       AS pendentes,
            fa.link_drive,
            fa.caminho_base
        FROM tbl_fila_adm fa
        INNER JOIN tbl_adm a ON a.id_adm = fa.id_adm
        LEFT JOIN tbl_fila_cotas fc ON fc.id_fila_adm = fa.id_fila_adm
        WHERE fa.id_fila_adm = %s
        GROUP BY a.nome, fa.modalidade, fa.hora_inicio, fa.hora_fim,
                 fa.hora_criado, fa.total_cotas, fa.link_drive, fa.caminho_base
    """
    row = fetchone(sql, (id_fila_adm,))
    if not row:
        return {}
    return {
        "nome_adm":      str(row[0] or ""),
        "modalidade":    str(row[1] or ""),
        "mes_ref":       row[2],
        "hora_inicio":   row[3],
        "hora_fim":      row[4],
        "total":         int(row[5]),
        "ofertadas":     int(row[6]),
        "nao_ofertadas": int(row[7]),
        "erros":         int(row[8]),
        "pendentes":     int(row[9]),
        "link_drive":    str(row[10] or ""),
        "caminho_base":  str(row[11] or ""),
    }


def _categorizar_erros(id_fila_adm: int) -> dict:
    sql = """
        SELECT
            CASE
                WHEN fc.observacao ILIKE '%%PAGINA_INESPERADA%%' AND fc.observacao ILIKE '%%login%%'
                    THEN 'Erro de Login'
                WHEN fc.observacao ILIKE '%%COTA INDISPONIVEL%%'
                     OR fc.observacao ILIKE '%%TIPO DE LANCE INDISPONIVEL%%'
                     OR fc.observacao ILIKE '%%CARD NAO APARECEU%%'
                    THEN 'Cota Indisponivel'
                WHEN fc.observacao ILIKE '%%TA DA PARCELA NEGATIVO%%'
                     OR fc.observacao ILIKE '%%TA NEGATIVO%%'
                     OR fc.observacao ILIKE '%%PERCENTUAL DE TA%%'
                    THEN 'TA Negativo'
                WHEN fc.observacao ILIKE '%%VALOR A PAGAR MAIOR%%'
                    THEN 'Valor a Pagar'
                WHEN COALESCE(fc.tentativas, 0) >= 3 AND fc.status = 'FALHA'
                    THEN '3 Tentativas (Safeguard)'
                WHEN fc.status = 'FALHA'
                    THEN 'Outros Erros'
                WHEN fc.status = 'NAO_OFERTADO'
                    THEN 'Outros NAO_OFERTADO'
                ELSE fc.status
            END AS categoria,
            COUNT(*) AS total
        FROM tbl_fila_cotas fc
        WHERE fc.id_fila_adm = %s
          AND fc.status IN ('FALHA', 'NAO_OFERTADO')
        GROUP BY categoria
        ORDER BY total DESC
    """
    rows = fetchall(sql, (id_fila_adm,))
    return {str(r[0]): int(r[1]) for r in rows} if rows else {}


def _cotas_por_consultor(id_fila_adm: int) -> list:
    sql = """
        SELECT
            COALESCE(NULLIF(TRIM(fc.nome_consultor), ''), 'SEM CONSULTOR') AS consultor,
            COUNT(*)                                                           AS total,
            SUM(CASE WHEN fc.status = 'OFERTADO'     THEN 1 ELSE 0 END)      AS ofertadas,
            SUM(CASE WHEN fc.status = 'NAO_OFERTADO' THEN 1 ELSE 0 END)      AS nao_ofertadas,
            SUM(CASE WHEN fc.status = 'FALHA'        THEN 1 ELSE 0 END)      AS erros
        FROM tbl_fila_cotas fc
        WHERE fc.id_fila_adm = %s
        GROUP BY consultor
        ORDER BY ofertadas DESC, total DESC
    """
    rows = fetchall(sql, (id_fila_adm,))
    return [
        {
            "consultor":     str(r[0]),
            "total":         int(r[1]),
            "ofertadas":     int(r[2]),
            "nao_ofertadas": int(r[3]),
            "erros":         int(r[4]),
        }
        for r in rows
    ] if rows else []


def _cotas_safeguard(id_fila_adm: int) -> list:
    """Cotas com tentativas >= 3 e status FALHA — precisam ser reexecutadas manualmente."""
    sql = """
        SELECT
            COALESCE(NULLIF(TRIM(fc.nome_cliente), ''),   '(sem nome)')        AS nome_cliente,
            COALESCE(NULLIF(TRIM(fc.nome_consultor), ''), 'SEM CONSULTOR')     AS consultor,
            fc.grupo,
            fc.cota,
            COALESCE(fc.tentativas, 0)                                          AS tentativas,
            COALESCE(NULLIF(TRIM(fc.observacao), ''),     '(sem observacao)')  AS observacao
        FROM tbl_fila_cotas fc
        WHERE fc.id_fila_adm = %s
          AND fc.status = 'FALHA'
          AND COALESCE(fc.tentativas, 0) >= 3
        ORDER BY fc.nome_consultor, fc.grupo, fc.cota
    """
    rows = fetchall(sql, (id_fila_adm,))
    return [
        {
            "nome_cliente": str(r[0]),
            "consultor":    str(r[1]),
            "grupo":        str(r[2]),
            "cota":         str(r[3]),
            "tentativas":   int(r[4]),
            "observacao":   str(r[5]),
        }
        for r in rows
    ] if rows else []


def _cotas_por_categoria(id_fila_adm: int, categoria: str) -> list:
    """Lista detalhada das cotas de uma categoria especifica."""
    condicao = {
        "Erro de Login": (
            "(fc.observacao ILIKE '%%PAGINA_INESPERADA%%' AND fc.observacao ILIKE '%%login%%')"
        ),
        "Cota Indisponivel": (
            "(fc.observacao ILIKE '%%COTA INDISPONIVEL%%' "
            "OR fc.observacao ILIKE '%%TIPO DE LANCE INDISPONIVEL%%' "
            "OR fc.observacao ILIKE '%%CARD NAO APARECEU%%')"
        ),
        "TA Negativo": (
            "(fc.observacao ILIKE '%%TA DA PARCELA NEGATIVO%%' "
            "OR fc.observacao ILIKE '%%TA NEGATIVO%%' "
            "OR fc.observacao ILIKE '%%PERCENTUAL DE TA%%')"
        ),
        "Valor a Pagar": (
            "fc.observacao ILIKE '%%VALOR A PAGAR MAIOR%%'"
        ),
        "Outros NAO_OFERTADO": (
            "fc.status = 'NAO_OFERTADO' "
            "AND fc.observacao NOT ILIKE '%%COTA INDISPONIVEL%%' "
            "AND fc.observacao NOT ILIKE '%%TIPO DE LANCE INDISPONIVEL%%' "
            "AND fc.observacao NOT ILIKE '%%CARD NAO APARECEU%%' "
            "AND fc.observacao NOT ILIKE '%%TA DA PARCELA NEGATIVO%%' "
            "AND fc.observacao NOT ILIKE '%%TA NEGATIVO%%' "
            "AND fc.observacao NOT ILIKE '%%PERCENTUAL DE TA%%' "
            "AND fc.observacao NOT ILIKE '%%VALOR A PAGAR MAIOR%%'"
        ),
        "Outros Erros": (
            "fc.status = 'FALHA' "
            "AND COALESCE(fc.tentativas, 0) < 3 "
            "AND fc.observacao NOT ILIKE '%%PAGINA_INESPERADA%%'"
        ),
    }.get(categoria)

    if not condicao:
        return []

    sql = f"""
        SELECT
            COALESCE(NULLIF(TRIM(fc.nome_cliente), ''),   '(sem nome)')       AS nome_cliente,
            COALESCE(NULLIF(TRIM(fc.nome_consultor), ''), 'SEM CONSULTOR')    AS consultor,
            fc.grupo,
            fc.cota,
            COALESCE(NULLIF(TRIM(fc.observacao), ''),     '(sem observacao)') AS observacao
        FROM tbl_fila_cotas fc
        WHERE fc.id_fila_adm = %s AND {condicao}
        ORDER BY fc.nome_consultor, fc.grupo, fc.cota
        LIMIT 50
    """
    rows = fetchall(sql, (id_fila_adm,))
    return [
        {
            "nome_cliente": str(r[0]),
            "consultor":    str(r[1]),
            "grupo":        str(r[2]),
            "cota":         str(r[3]),
            "observacao":   str(r[4]),
        }
        for r in rows
    ] if rows else []


# =========================================================
# GRAFICO
# =========================================================

def _gerar_grafico_png(categorias: dict, nome_adm: str, modalidade: str) -> Optional[bytes]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        return None

    dados = {}
    for cat in CATEGORIAS_ORDEM:
        if categorias.get(cat, 0) > 0:
            dados[cat] = categorias[cat]
    for cat, total in categorias.items():
        if cat not in dados and total > 0:
            dados[cat] = total

    if not dados:
        return None

    labels = list(dados.keys())
    values = list(dados.values())
    cores  = [CORES_GRAF.get(lbl, "#90A4AE") for lbl in labels]

    fig, ax = plt.subplots(figsize=(11, max(3.5, len(labels) * 0.75 + 2)))
    fig.patch.set_facecolor("#FAFAFA")
    ax.set_facecolor("#FFFFFF")

    bars = ax.barh(labels, values, color=cores, edgecolor="white", height=0.55)

    for bar, val in zip(bars, values):
        ax.text(
            bar.get_width() + max(values) * 0.01,
            bar.get_y() + bar.get_height() / 2,
            str(val),
            va="center", ha="left",
            fontsize=10, fontweight="bold", color="#333333",
        )

    total_erros = sum(values)
    ax.set_xlabel("Quantidade de cotas", fontsize=10, color="#555555")
    ax.set_title(
        f"Erros por categoria  —  {nome_adm}  ({modalidade})\n"
        f"Total de problemas: {total_erros}  ·  {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        fontsize=12, fontweight="bold", pad=14, color="#212121",
    )
    ax.invert_yaxis()
    ax.set_xlim(0, max(values) * 1.22)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#EEEEEE")
    ax.spines["bottom"].set_color("#EEEEEE")
    ax.tick_params(axis="y", labelsize=9, colors="#333333")
    ax.tick_params(axis="x", labelsize=8, colors="#888888")
    ax.xaxis.grid(True, color="#F0F0F0", linewidth=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout(pad=1.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# =========================================================
# HTML
# =========================================================

def _badge(texto: str, cor: str) -> str:
    return (
        f'<span style="background-color:{cor}; color:#FFFFFF; font-size:10px; '
        f'font-weight:700; padding:2px 8px; border-radius:10px; white-space:nowrap;">'
        f'{texto}</span>'
    )


def _tabela_cotas(cotas: list, mostrar_consultor: bool = True) -> str:
    if not cotas:
        return '<p style="color:#9E9E9E; font-size:12px; margin:0;">(nenhuma)</p>'

    header_consultor = (
        '<th style="padding:8px 10px; text-align:left; font-size:10px; '
        'color:#888; font-weight:700; text-transform:uppercase; letter-spacing:1px; '
        'border-bottom:2px solid #EEEEEE;">Consultor</th>'
        if mostrar_consultor else ""
    )
    linhas = []
    for c in cotas:
        col_consultor = (
            f'<td style="padding:8px 10px; font-size:12px; color:#616161; '
            f'border-bottom:1px solid #F5F5F5;">{c.get("consultor","")}</td>'
            if mostrar_consultor else ""
        )
        obs = c.get("observacao", "")
        # trunca observacao longa
        obs_exib = (obs[:90] + "…") if len(obs) > 90 else obs
        linhas.append(
            f'<tr>'
            f'{col_consultor}'
            f'<td style="padding:8px 10px; font-size:12px; color:#212121; font-weight:600; '
            f'border-bottom:1px solid #F5F5F5;">{c["nome_cliente"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#555; white-space:nowrap; '
            f'border-bottom:1px solid #F5F5F5;">{c["grupo"]}&nbsp;/&nbsp;{c["cota"]}</td>'
            f'<td style="padding:8px 10px; font-size:11px; color:#888; '
            f'border-bottom:1px solid #F5F5F5;">{obs_exib}</td>'
            f'</tr>'
        )

    return (
        f'<table width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse; border:1px solid #EEEEEE; border-radius:6px; overflow:hidden;">'
        f'<thead><tr style="background:#FAFAFA;">'
        f'{header_consultor}'
        f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#888; '
        f'font-weight:700; text-transform:uppercase; letter-spacing:1px; '
        f'border-bottom:2px solid #EEEEEE;">Cliente</th>'
        f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#888; '
        f'font-weight:700; text-transform:uppercase; letter-spacing:1px; '
        f'border-bottom:2px solid #EEEEEE;">Grupo / Cota</th>'
        f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#888; '
        f'font-weight:700; text-transform:uppercase; letter-spacing:1px; '
        f'border-bottom:2px solid #EEEEEE;">Observação</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(linhas)}</tbody>'
        f'</table>'
    )


def _secao_html(titulo: str, cor: str, conteudo: str) -> str:
    return f"""
    <tr><td style="padding:20px 32px 0 32px;">
      <div style="border-left:4px solid {cor}; padding-left:14px; margin-bottom:10px;">
        <span style="font-family:'Segoe UI',Arial,sans-serif; font-size:13px; font-weight:700;
          color:{cor}; text-transform:uppercase; letter-spacing:1px;">{titulo}</span>
      </div>
      {conteudo}
    </td></tr>"""


def _montar_html(
    resumo: dict,
    categorias: dict,
    por_consultor: list,
    safeguard: list,
    detalhe_cats: dict,
    mes_formatado: str,
    duracao_str: str,
) -> str:
    nome_adm   = resumo.get("nome_adm", "")
    modalidade = resumo.get("modalidade", "")
    total      = resumo.get("total", 0)
    ofertadas  = resumo.get("ofertadas", 0)
    nao_of     = resumo.get("nao_ofertadas", 0)
    erros      = resumo.get("erros", 0)
    pendentes  = resumo.get("pendentes", 0)
    taxa       = f"{ofertadas/total*100:.1f}%" if total > 0 else "—"

    # ── Breakdown por consultor ──
    linhas_cons = []
    for c in por_consultor:
        taxa_c = f"{c['ofertadas']/c['total']*100:.0f}%" if c['total'] > 0 else "—"
        linhas_cons.append(
            f'<tr>'
            f'<td style="padding:8px 10px; font-size:12px; color:#212121; font-weight:600; '
            f'border-bottom:1px solid #F5F5F5;">{c["consultor"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#424242; text-align:center; '
            f'border-bottom:1px solid #F5F5F5;">{c["total"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#2E7D32; font-weight:700; '
            f'text-align:center; border-bottom:1px solid #F5F5F5;">{c["ofertadas"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#E65100; text-align:center; '
            f'border-bottom:1px solid #F5F5F5;">{c["nao_ofertadas"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#B71C1C; text-align:center; '
            f'border-bottom:1px solid #F5F5F5;">{c["erros"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#1565C0; font-weight:700; '
            f'text-align:center; border-bottom:1px solid #F5F5F5;">{taxa_c}</td>'
            f'</tr>'
        )
    tabela_consultor = (
        f'<table width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse; border:1px solid #EEEEEE; border-radius:6px;">'
        f'<thead><tr style="background:#FAFAFA;">'
        f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#888; font-weight:700; '
        f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #EEEEEE;">Consultor</th>'
        f'<th style="padding:8px 10px; font-size:10px; color:#888; font-weight:700; '
        f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #EEEEEE; text-align:center;">Total</th>'
        f'<th style="padding:8px 10px; font-size:10px; color:#2E7D32; font-weight:700; '
        f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #EEEEEE; text-align:center;">Ofertadas</th>'
        f'<th style="padding:8px 10px; font-size:10px; color:#E65100; font-weight:700; '
        f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #EEEEEE; text-align:center;">Não ofert.</th>'
        f'<th style="padding:8px 10px; font-size:10px; color:#B71C1C; font-weight:700; '
        f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #EEEEEE; text-align:center;">Erros</th>'
        f'<th style="padding:8px 10px; font-size:10px; color:#1565C0; font-weight:700; '
        f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #EEEEEE; text-align:center;">Taxa</th>'
        f'</tr></thead><tbody>{"".join(linhas_cons)}</tbody></table>'
        if linhas_cons else '<p style="color:#9E9E9E; font-size:12px; margin:0;">(nenhum dado)</p>'
    )

    # ── Categorias de erro ──
    linhas_cat = []
    total_problemas = sum(categorias.values())
    for cat in CATEGORIAS_ORDEM:
        n = categorias.get(cat, 0)
        if n == 0:
            continue
        cor = CORES_HTML.get(cat, "#546E7A")
        pct = f"{n/total_problemas*100:.0f}%" if total_problemas > 0 else ""
        bar_w = int(n / max(categorias.values()) * 120) if categorias else 0
        linhas_cat.append(
            f'<tr>'
            f'<td style="padding:8px 12px; font-size:12px; color:#212121; font-weight:600; '
            f'border-bottom:1px solid #F5F5F5; white-space:nowrap;">'
            f'{_badge(cat, cor)}</td>'
            f'<td style="padding:8px 12px; border-bottom:1px solid #F5F5F5;">'
            f'<div style="background:{cor}; height:8px; width:{bar_w}px; border-radius:4px; display:inline-block;"></div></td>'
            f'<td style="padding:8px 12px; font-size:13px; color:{cor}; font-weight:700; '
            f'border-bottom:1px solid #F5F5F5; text-align:right; white-space:nowrap;">{n} &nbsp;<span style="font-size:10px;color:#aaa;">{pct}</span></td>'
            f'</tr>'
        )
    for cat, n in categorias.items():
        if cat not in CATEGORIAS_ORDEM and n > 0:
            linhas_cat.append(
                f'<tr><td style="padding:8px 12px; font-size:12px; color:#212121; border-bottom:1px solid #F5F5F5;">'
                f'{_badge(cat, "#546E7A")}</td>'
                f'<td style="border-bottom:1px solid #F5F5F5;"></td>'
                f'<td style="padding:8px 12px; font-size:13px; font-weight:700; color:#546E7A; '
                f'border-bottom:1px solid #F5F5F5; text-align:right;">{n}</td></tr>'
            )
    tabela_cats = (
        f'<table width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse; border:1px solid #EEEEEE; border-radius:6px;">'
        f'<tbody>{"".join(linhas_cat)}</tbody></table>'
        if linhas_cat else
        '<p style="color:#2E7D32; font-size:13px; font-weight:600; margin:0;">✓ Nenhum erro registrado</p>'
    )

    # ── Safeguard: lista para reexecutar ──
    if safeguard:
        linhas_sg = []
        for c in safeguard:
            obs_exib = (c['observacao'][:80] + "…") if len(c['observacao']) > 80 else c['observacao']
            linhas_sg.append(
                f'<tr>'
                f'<td style="padding:8px 10px; font-size:12px; color:#6A1B9A; font-weight:700; '
                f'border-bottom:1px solid #F5F5F5; white-space:nowrap;">{c["grupo"]}&nbsp;/&nbsp;{c["cota"]}</td>'
                f'<td style="padding:8px 10px; font-size:12px; color:#212121; font-weight:600; '
                f'border-bottom:1px solid #F5F5F5;">{c["nome_cliente"]}</td>'
                f'<td style="padding:8px 10px; font-size:12px; color:#616161; '
                f'border-bottom:1px solid #F5F5F5;">{c["consultor"]}</td>'
                f'<td style="padding:8px 10px; font-size:11px; color:#B71C1C; font-weight:700; '
                f'text-align:center; border-bottom:1px solid #F5F5F5;">{c["tentativas"]}x</td>'
                f'<td style="padding:8px 10px; font-size:11px; color:#888; '
                f'border-bottom:1px solid #F5F5F5;">{obs_exib}</td>'
                f'</tr>'
            )
        tabela_sg = (
            f'<div style="background:#FFF3E0; border:1px solid #FFB74D; border-radius:6px; '
            f'padding:10px 14px; margin-bottom:12px; font-size:12px; color:#E65100; font-weight:700;">'
            f'⚠ {len(safeguard)} cota(s) esgotaram as tentativas — reexecucao manual necessaria.</div>'
            f'<table width="100%" cellspacing="0" cellpadding="0" '
            f'style="border-collapse:collapse; border:1px solid #EEEEEE; border-radius:6px;">'
            f'<thead><tr style="background:#F3E5F5;">'
            f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#6A1B9A; font-weight:700; '
            f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #CE93D8;">Grupo / Cota</th>'
            f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#6A1B9A; font-weight:700; '
            f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #CE93D8;">Cliente</th>'
            f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#6A1B9A; font-weight:700; '
            f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #CE93D8;">Consultor</th>'
            f'<th style="padding:8px 10px; text-align:center; font-size:10px; color:#6A1B9A; font-weight:700; '
            f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #CE93D8;">Tent.</th>'
            f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#6A1B9A; font-weight:700; '
            f'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #CE93D8;">Último erro</th>'
            f'</tr></thead><tbody>{"".join(linhas_sg)}</tbody></table>'
        )
    else:
        tabela_sg = '<p style="color:#2E7D32; font-size:13px; font-weight:600; margin:0;">✓ Nenhuma cota em safeguard</p>'

    # ── Detalhes por categoria ──
    secoes_detalhe = ""
    for cat in CATEGORIAS_ORDEM:
        cotas = detalhe_cats.get(cat, [])
        if not cotas:
            continue
        cor = CORES_HTML.get(cat, "#546E7A")
        secoes_detalhe += _secao_html(
            f"{cat} ({len(cotas)})",
            cor,
            _tabela_cotas(cotas),
        )

    link_drive = resumo.get("link_drive", "")
    link_html = (
        f'<a href="{link_drive}" style="color:#1565C0;">{link_drive}</a>'
        if link_drive else "(sem link)"
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Relatorio Interno — Ofertar Lance</title></head>
<body style="margin:0;padding:0;background:#EEEEEE;font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellspacing="0" cellpadding="0" bgcolor="#EEEEEE">
<tr><td align="center" style="padding:28px 12px;">

<table width="680" cellspacing="0" cellpadding="0" bgcolor="#FFFFFF"
  style="max-width:680px;border-radius:8px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">

  <!-- Header -->
  <tr><td bgcolor="#1A237E" style="padding:24px 32px; background:#1A237E;">
    <div style="color:#C5CAE9;font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;">
      INTERNO &middot; RPA ADEMICON &middot; USO OPERACIONAL
    </div>
    <div style="color:#FFFFFF;font-size:22px;font-weight:700;margin-top:8px;">
      Relatório de Execução — Ofertar Lance
    </div>
    <div style="color:#9FA8DA;font-size:13px;margin-top:4px;">
      {nome_adm} &nbsp;&middot;&nbsp; {modalidade} &nbsp;&middot;&nbsp; {mes_formatado}
      &nbsp;&middot;&nbsp; {datetime.now().strftime('%d/%m/%Y %H:%M')}
    </div>
  </td></tr>

  <!-- Stats principais -->
  <tr><td style="padding:20px 32px 0 32px;">
    <table width="100%" cellspacing="0" cellpadding="0"
      style="border:1px solid #EEEEEE;border-radius:8px;overflow:hidden;">
    <tr>
      <td width="20%" align="center" style="padding:16px 4px;border-right:1px solid #EEE;">
        <div style="font-size:26px;font-weight:700;color:#424242;">{total}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Total</div>
      </td>
      <td width="20%" align="center" style="padding:16px 4px;border-right:1px solid #EEE;">
        <div style="font-size:26px;font-weight:700;color:#2E7D32;">{ofertadas}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Ofertadas</div>
      </td>
      <td width="20%" align="center" style="padding:16px 4px;border-right:1px solid #EEE;">
        <div style="font-size:26px;font-weight:700;color:#E65100;">{nao_of}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Não ofert.</div>
      </td>
      <td width="20%" align="center" style="padding:16px 4px;border-right:1px solid #EEE;">
        <div style="font-size:26px;font-weight:700;color:#B71C1C;">{erros}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Erros</div>
      </td>
      <td width="20%" align="center" style="padding:16px 4px;">
        <div style="font-size:26px;font-weight:700;color:#1565C0;">{taxa}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Taxa sucesso</div>
      </td>
    </tr>
    </table>
    <div style="font-size:11px;color:#9E9E9E;margin-top:6px;text-align:right;">
      Duração: {duracao_str}
      {"&nbsp;|&nbsp;<span style='color:#E65100;'>"+str(pendentes)+" pendentes</span>" if pendentes > 0 else ""}
      {"&nbsp;|&nbsp;<span style='color:#6A1B9A;font-weight:700;'>"+str(len(safeguard))+" para reexecutar</span>" if safeguard else ""}
    </div>
  </td></tr>

  <!-- Grafico placeholder -->
  <tr><td style="padding:16px 32px 0 32px;">
    <div style="font-family:'Segoe UI',Arial,sans-serif;font-size:11px;font-weight:700;
      color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
      Erros por categoria (gráfico em anexo)
    </div>
    {tabela_cats}
  </td></tr>

  <!-- Por consultor -->
  {_secao_html("Por Consultor / Funcionário", "#1565C0", tabela_consultor)}

  <!-- Safeguard -->
  {_secao_html("⚠ Para Reexecutar (Safeguard)", "#6A1B9A", tabela_sg)}

  <!-- Detalhes por categoria -->
  {"".join([secoes_detalhe]) if secoes_detalhe else ""}

  <!-- Drive -->
  <tr><td style="padding:16px 32px 0 32px;">
    <div style="font-size:11px;color:#888;font-weight:700;text-transform:uppercase;
      letter-spacing:1px;margin-bottom:4px;">Pasta no Drive</div>
    <div style="font-size:12px;">{link_html}</div>
  </td></tr>

  <!-- Footer -->
  <tr><td bgcolor="#F5F5F5" style="padding:14px 32px;border-top:1px solid #EEE;
    font-size:10px;color:#9E9E9E;text-align:center;margin-top:20px;">
    Email automatico gerado pelo RPA &middot; USO INTERNO &middot; Nao responder
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def _montar_txt(resumo: dict, categorias: dict, por_consultor: list, safeguard: list, duracao_str: str, mes_formatado: str) -> str:
    nome_adm = resumo.get("nome_adm", "")
    total    = resumo.get("total", 0)
    taxa     = f"{resumo['ofertadas']/total*100:.1f}%" if total > 0 else "—"
    linhas   = [
        "=" * 60,
        "RELATORIO INTERNO — RPA OFERTAR LANCE (USO OPERACIONAL)",
        "=" * 60,
        f"ADM        : {nome_adm}",
        f"Modalidade : {resumo.get('modalidade','')}",
        f"Mes ref    : {mes_formatado}",
        f"Geracao    : {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        f"Duracao    : {duracao_str}",
        "",
        "RESUMO",
        f"  Total        : {total}",
        f"  Ofertadas    : {resumo.get('ofertadas',0)}",
        f"  Nao ofert.   : {resumo.get('nao_ofertadas',0)}",
        f"  Erros        : {resumo.get('erros',0)}",
        f"  Pendentes    : {resumo.get('pendentes',0)}",
        f"  Taxa sucesso : {taxa}",
        "",
        "ERROS POR CATEGORIA",
    ]
    for cat in CATEGORIAS_ORDEM:
        n = categorias.get(cat, 0)
        if n:
            linhas.append(f"  {cat:<30}: {n}")
    linhas += ["", "POR CONSULTOR"]
    for c in por_consultor:
        taxa_c = f"{c['ofertadas']/c['total']*100:.0f}%" if c['total'] > 0 else "—"
        linhas.append(
            f"  {c['consultor'][:28]:<28} total={c['total']} "
            f"ofert={c['ofertadas']} nao_of={c['nao_ofertadas']} "
            f"erros={c['erros']} taxa={taxa_c}"
        )
    if safeguard:
        linhas += ["", f"PARA REEXECUTAR ({len(safeguard)} cotas — safeguard)"]
        for c in safeguard:
            linhas.append(
                f"  {c['grupo']}/{c['cota']} | {c['nome_cliente'][:30]} "
                f"| {c['consultor'][:20]} | {c['tentativas']}x | {c['observacao'][:60]}"
            )
    linhas += ["", "---", "Email automatico — uso interno. Nao responder."]
    return "\n".join(linhas)


# =========================================================
# HELPERS
# =========================================================

def _duracao(resumo: dict) -> str:
    try:
        inicio = resumo.get("hora_inicio")
        fim    = resumo.get("hora_fim") or datetime.now()
        if inicio:
            delta = fim - inicio
            mins  = int(delta.total_seconds() // 60)
            segs  = int(delta.total_seconds() % 60)
            return f"{mins}m {segs}s"
    except Exception:
        pass
    return "—"


def _formatar_mes(mes_ref) -> str:
    MESES = {1:"Jan",2:"Fev",3:"Mar",4:"Abr",5:"Mai",6:"Jun",
              7:"Jul",8:"Ago",9:"Set",10:"Out",11:"Nov",12:"Dez"}
    try:
        s   = str(mes_ref)
        ano = s[:4]
        mes = int(s[4:6])
        return f"{MESES.get(mes,'?')}/{ano}"
    except Exception:
        return str(mes_ref or "")


# =========================================================
# PONTO DE ENTRADA
# =========================================================

def gerar_relatorio_interno(id_fila_adm: int, modalidade: str, logger) -> bool:
    """
    Gera e envia o relatorio interno completo para rpa.ademicon@gmail.com.
    Nunca levanta excecao — falha silenciosa.
    """
    try:
        logger.info(f"[RELATORIO] Coletando dados do lote | id_fila_adm={id_fila_adm}")
        resumo = _resumo_lote(id_fila_adm)
        if not resumo:
            logger.warn(f"[RELATORIO] Lote nao encontrado | id_fila_adm={id_fila_adm}")
            return False

        logger.info(
            f"[RELATORIO] Resumo obtido | "
            f"nome_adm={resumo.get('nome_adm','')} "
            f"modalidade={resumo.get('modalidade','')} "
            f"total={resumo.get('total',0)} "
            f"ofertadas={resumo.get('ofertadas',0)} "
            f"erros={resumo.get('erros',0)}"
        )

        categorias   = _categorizar_erros(id_fila_adm)
        por_consultor = _cotas_por_consultor(id_fila_adm)
        safeguard    = _cotas_safeguard(id_fila_adm)
        duracao_str  = _duracao(resumo)
        mes_formatado = _formatar_mes(resumo.get("mes_ref"))

        logger.info(
            f"[RELATORIO] Dados coletados | "
            f"ofertadas={resumo.get('ofertadas',0)} "
            f"erros={resumo.get('erros',0)} "
            f"safeguard={len(safeguard)} "
            f"categorias={list(categorias.keys())} "
            f"duracao={duracao_str}"
        )

        # Detalhes por categoria (exceto safeguard que ja tem tabela propria)
        detalhe_cats = {}
        for cat in CATEGORIAS_ORDEM:
            if cat == "3 Tentativas (Safeguard)":
                continue
            if categorias.get(cat, 0) > 0:
                detalhe_cats[cat] = _cotas_por_categoria(id_fila_adm, cat)

        logger.info("[RELATORIO] Gerando grafico matplotlib...")
        png_bytes = _gerar_grafico_png(categorias, resumo.get("nome_adm",""), modalidade)
        if not png_bytes:
            logger.warn("[RELATORIO] Grafico nao gerado (matplotlib indisponivel ou sem dados)")
        else:
            logger.info(f"[RELATORIO] Grafico gerado | tamanho={len(png_bytes)} bytes")

        logger.info("[RELATORIO] Montando corpo HTML e TXT...")
        corpo_html = _montar_html(
            resumo, categorias, por_consultor, safeguard, detalhe_cats, mes_formatado, duracao_str
        )
        corpo_txt = _montar_txt(
            resumo, categorias, por_consultor, safeguard, duracao_str, mes_formatado
        )
        logger.info(f"[RELATORIO] HTML montado | tamanho={len(corpo_html)} chars")

        nome_adm    = resumo.get("nome_adm", "")
        total_erros = sum(categorias.values())
        assunto = (
            f"[INTERNO] {nome_adm} | {modalidade} | {mes_formatado} | "
            f"{resumo.get('ofertadas',0)} ofertadas / "
            f"{total_erros} problemas"
            + (f" / {len(safeguard)} reexecutar" if safeguard else "")
        )
        logger.info(f"[RELATORIO] Assunto: {assunto!r}")

        msg = MIMEMultipart("mixed")
        msg["To"]      = EMAIL_RELATORIO_INTERNO
        msg["Subject"] = assunto

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(corpo_txt,  "plain", "utf-8"))
        alt.attach(MIMEText(corpo_html, "html",  "utf-8"))
        msg.attach(alt)

        if png_bytes:
            anexo = MIMEBase("image", "png")
            anexo.set_payload(png_bytes)
            encoders.encode_base64(anexo)
            ts         = datetime.now().strftime("%Y%m%d_%H%M")
            nome_arq   = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{nome_adm}_{modalidade}_{ts}") + ".png"
            anexo.add_header("Content-Disposition", f'attachment; filename="{nome_arq}"')
            msg.attach(anexo)

        logger.info("[RELATORIO] Autenticando Gmail API...")
        service = criar_servico_gmail()
        logger.info("[RELATORIO] Gmail service criado — enviando mensagem...")
        raw     = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result  = service.users().messages().send(userId="me", body={"raw": raw}).execute()

        logger.info(
            f"[RELATORIO] Enviado com sucesso | "
            f"para={EMAIL_RELATORIO_INTERNO} "
            f"message_id={result.get('id','?')} "
            f"safeguard={len(safeguard)}"
        )
        return True

    except Exception as e:
        try:
            tb = traceback.format_exc()
            logger.error(f"[RELATORIO] Falha ao gerar/enviar: {e}")
            logger.error(f"[RELATORIO] Traceback:\n{tb}")
        except Exception:
            pass
        return False
