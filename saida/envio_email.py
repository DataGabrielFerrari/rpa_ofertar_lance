import base64
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from credenciais.google_auth import criar_servico_gmail
from db.db import fetchone, fetchall


# =========================================================
# HELPERS
# =========================================================

MESES_PT = {
    1: "Janeiro",
    2: "Fevereiro",
    3: "Março",
    4: "Abril",
    5: "Maio",
    6: "Junho",
    7: "Julho",
    8: "Agosto",
    9: "Setembro",
    10: "Outubro",
    11: "Novembro",
    12: "Dezembro",
}


def formatar_mes_extenso(mes_ref: int) -> str:
    mes_ref_str = str(mes_ref)
    ano = mes_ref_str[:4]
    try:
        mes = int(mes_ref_str[4:6])
    except Exception:
        mes = 0
    return f"{MESES_PT.get(mes, '?')}/{ano}"


def normalizar_modalidade_exibicao(modalidade: str) -> str:
    modalidade = (modalidade or "").strip().upper()

    if modalidade == "IMOVEL":
        return "Imóvel"
    if modalidade == "MOTORS":
        return "Motors"

    return modalidade.title() if modalidade else "Modalidade"


def _falhar_email(logger, id_fila_adm: int, acao: str, detalhe: str) -> None:
    logger.error(f"[EMAIL] {acao} | id_fila_adm={id_fila_adm} | {detalhe}")
    raise RuntimeError(detalhe)


# =========================================================
# QUERIES
# =========================================================

def _obter_resumo_lote_email(id_fila_adm: int):
    """
    Calcula as metricas AO VIVO direto de tbl_fila_cotas, em vez de
    depender dos contadores cacheados em tbl_fila_adm.

    Motivo: o email roda ANTES do fechar_lote_adm em saida/main.py,
    entao os contadores cacheados ainda estao em 0. Computar live
    garante que o resumo enviado ao ADM reflita o estado real do lote.
    """
    sql = """
        SELECT
            COALESCE(f.total_cotas, 0)                                          AS total_cotas,
            COALESCE(SUM(CASE WHEN fc.status = 'OFERTADO'     THEN 1 ELSE 0 END), 0) AS cotas_ofertadas,
            COALESCE(SUM(CASE WHEN fc.status = 'NAO_OFERTADO' THEN 1 ELSE 0 END), 0) AS cotas_nao_ofertadas,
            COALESCE(SUM(CASE WHEN fc.status = 'FALHA'        THEN 1 ELSE 0 END), 0) AS cotas_erro,
            f.link_drive,
            a.email,
            a.nome
        FROM tbl_fila_adm f
        INNER JOIN tbl_adm a ON a.id_adm = f.id_adm
        LEFT JOIN tbl_fila_cotas fc ON fc.id_fila_adm = f.id_fila_adm
        WHERE f.id_fila_adm = %s
        GROUP BY f.id_fila_adm, f.total_cotas, f.link_drive, a.email, a.nome
    """
    return fetchone(sql, (id_fila_adm,))


def _obter_mes_ref_banco(id_fila_adm: int, logger) -> int | None:
    """
    Deriva o mes_ref do lote a partir de hora_inicio (coluna mes_ref nao existe).
    Retorna inteiro no formato YYYYMM, ou None em caso de falha.
    """
    try:
        row = fetchone(
            """
            SELECT TO_CHAR(COALESCE(hora_inicio, hora_criado), 'YYYYMM')::integer
            FROM tbl_fila_adm
            WHERE id_fila_adm = %s
            """,
            (id_fila_adm,),
        )
        if row and row[0]:
            return int(row[0])
        logger.warn(
            f"[EMAIL] hora_inicio/hora_criado nulos em tbl_fila_adm | id_fila_adm={id_fila_adm}"
        )
    except Exception as e:
        logger.warn(
            f"[EMAIL] Falha ao obter mes_ref do banco — fallback para datetime.now() | "
            f"id_fila_adm={id_fila_adm} | erro={e}"
        )
    return None


def _listar_cotas_falha(id_fila_adm: int):
    """
    Retorna as cotas que terminaram em FALHA - para listar no email
    como secao de divergencias/atencao.
    """
    sql = """
        SELECT
            COALESCE(NULLIF(TRIM(fc.nome_cliente), ''), '(sem nome)') AS nome_cliente,
            fc.grupo,
            fc.cota,
            COALESCE(NULLIF(TRIM(fc.observacao), ''), '(sem observacao)') AS observacao
        FROM tbl_fila_cotas fc
        WHERE fc.id_fila_adm = %s
          AND fc.status = 'FALHA'
        ORDER BY fc.grupo, fc.cota
    """
    rows = fetchall(sql, (id_fila_adm,))
    return [
        {
            "nome_cliente": str(r[0]).strip(),
            "grupo": str(r[1]).strip(),
            "cota": str(r[2]).strip(),
            "observacao": str(r[3]).strip(),
        }
        for r in rows
    ]


# =========================================================
# RENDER
# =========================================================

def _montar_secao_falhas(cotas_falha):
    """
    Monta a secao "Cotas em FALHA" tanto em texto quanto em HTML.
    Quando nao ha cotas em FALHA, renderiza um bloco neutro de OK.
    """
    if not cotas_falha:
        secao_txt = "\nNenhuma cota apresentou falha tecnica.\n"
        secao_html = """
          <tr>
            <td style="padding:8px 32px 0 32px;">
              <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:13px; color:#616161; padding:14px 16px; background-color:#FAFAFA; border:1px solid #EEEEEE; border-radius:6px;">
                Nenhuma cota apresentou falha tecnica.
              </div>
            </td>
          </tr>
        """
        return secao_txt, secao_html

    linhas_txt = []
    linhas_tr = []

    for c in cotas_falha:
        linhas_txt.append(
            f"  - {c['nome_cliente']} | Grupo: {c['grupo']} | Cota: {c['cota']} | Obs: {c['observacao']}"
        )
        linhas_tr.append(
            f"""
            <tr>
              <td style="padding:10px 14px; border-bottom:1px solid #F0F0F0; font-family:'Segoe UI',Arial,sans-serif; font-size:13px; color:#212121;">
                <strong style="color:#000000;">{c['nome_cliente']}</strong>
                <div style="color:#9E9E9E; font-size:11px; margin-top:2px;">{c['observacao']}</div>
              </td>
              <td style="padding:10px 14px; border-bottom:1px solid #F0F0F0; font-family:'Segoe UI',Arial,sans-serif; font-size:13px; color:#616161; white-space:nowrap; text-align:right;">
                Grupo <strong style="color:#212121;">{c['grupo']}</strong> &middot; Cota <strong style="color:#212121;">{c['cota']}</strong>
              </td>
            </tr>"""
        )

    secao_txt = (
        "\nCotas que apresentaram FALHA tecnica e precisam de atencao:\n"
        + "\n".join(linhas_txt)
        + "\n"
    )
    secao_html = f"""
          <tr>
            <td style="padding:8px 32px 0 32px;">
              <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:11px; font-weight:700; color:#B71C1C; text-transform:uppercase; letter-spacing:1.2px; padding:6px 0 10px 0;">
                Cotas que apresentaram falha tecnica
              </div>
              <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color:#FAFAFA; border:1px solid #EEEEEE; border-radius:6px;">
                {''.join(linhas_tr)}
              </table>
            </td>
          </tr>
    """
    return secao_txt, secao_html


def _montar_corpo_txt(
    nome_adm: str,
    modalidade_exibicao: str,
    mes_formatado: str,
    total_cotas: int,
    cotas_ofertadas: int,
    cotas_nao_ofertadas: int,
    cotas_erro: int,
    link_drive: str,
    secao_txt: str,
) -> str:
    return f"""Resumo de Processamento - Ofertar Lance {modalidade_exibicao}
Mes de referencia: {mes_formatado}
Administrador: {nome_adm}

Resultado consolidado:

  Total de cotas ......... {total_cotas}
  Ofertadas .............. {cotas_ofertadas}
  Nao ofertadas .......... {cotas_nao_ofertadas}
  Falhas tecnicas ........ {cotas_erro}
{secao_txt}
Acesse a pasta no Google Drive:
{link_drive}

-
Este e-mail foi gerado automaticamente pelo sistema RPA Ademicon.
""".strip()


def _montar_corpo_html(
    nome_adm: str,
    modalidade_exibicao: str,
    mes_formatado: str,
    total_cotas: int,
    cotas_ofertadas: int,
    cotas_nao_ofertadas: int,
    cotas_erro: int,
    link_drive: str,
    secao_html: str,
) -> str:
    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Resumo de Processamento</title>
<!--[if mso]>
<style type="text/css">
table, td {{ border-collapse: collapse; mso-line-height-rule: exactly; }}
</style>
<![endif]-->
</head>
<body style="margin:0; padding:0; background-color:#EEEEEE; font-family:'Segoe UI', Arial, sans-serif;">
  <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" bgcolor="#EEEEEE">
    <tr>
      <td align="center" style="padding:32px 12px;">

        <table role="presentation" width="640" border="0" cellspacing="0" cellpadding="0" bgcolor="#FFFFFF" style="border-collapse:collapse; max-width:640px; border-radius:8px; overflow:hidden; box-shadow:0 2px 12px rgba(0,0,0,0.08);">

          <!-- Faixa vermelha superior fina -->
          <tr>
            <td bgcolor="#8B0000" style="background-color:#8B0000; height:4px; line-height:4px; font-size:0;">&nbsp;</td>
          </tr>

          <!-- Header com identidade Ademicon -->
          <tr>
            <td bgcolor="#B71C1C" style="padding:28px 32px; background-color:#B71C1C;">
              <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0">
                <tr>
                  <td align="left" valign="middle" style="color:#FFCDD2; font-family:'Segoe UI',Arial,sans-serif; font-size:11px; font-weight:700; letter-spacing:2px; text-transform:uppercase;">
                    ADEMICON &middot; RPA
                  </td>
                  <td align="right" valign="middle" style="color:#FFCDD2; font-family:'Segoe UI',Arial,sans-serif; font-size:11px; font-weight:600; letter-spacing:1px; text-transform:uppercase;">
                    {mes_formatado}
                  </td>
                </tr>
                <tr>
                  <td colspan="2" align="left" style="color:#FFFFFF; font-family:'Segoe UI',Arial,sans-serif; font-size:24px; font-weight:700; line-height:1.3; padding-top:10px;">
                    Resumo de Processamento
                  </td>
                </tr>
                <tr>
                  <td colspan="2" align="left" style="color:#FFCDD2; font-family:'Segoe UI',Arial,sans-serif; font-size:13px; padding-top:4px;">
                    Ofertar Lance {modalidade_exibicao} &middot; {nome_adm}
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Saudacao + intro -->
          <tr>
            <td style="padding:28px 32px 4px 32px; font-family:'Segoe UI',Arial,sans-serif; color:#212121; font-size:15px; line-height:1.65;">
              Olá <strong>{nome_adm}</strong>, segue o resultado consolidado do processamento das ofertas de lance referentes a <strong>{mes_formatado}</strong>.
            </td>
          </tr>

          <!-- Stats - linha unica horizontal limpa -->
          <tr>
            <td style="padding:24px 32px 8px 32px;">
              <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="border:1px solid #EEEEEE; border-radius:8px;">
                <tr>
                  <td width="25%" align="center" valign="middle" style="padding:18px 4px; border-right:1px solid #EEEEEE;">
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:28px; font-weight:700; color:#424242; line-height:1;">{total_cotas}</div>
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:10px; color:#616161; text-transform:uppercase; letter-spacing:1.2px; margin-top:6px; font-weight:700;">Total</div>
                  </td>
                  <td width="25%" align="center" valign="middle" style="padding:18px 4px; border-right:1px solid #EEEEEE;">
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:28px; font-weight:700; color:#2E7D32; line-height:1;">{cotas_ofertadas}</div>
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:10px; color:#616161; text-transform:uppercase; letter-spacing:1.2px; margin-top:6px; font-weight:700;">Ofertados</div>
                  </td>
                  <td width="25%" align="center" valign="middle" style="padding:18px 4px; border-right:1px solid #EEEEEE;">
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:28px; font-weight:700; color:#E65100; line-height:1;">{cotas_nao_ofertadas}</div>
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:10px; color:#616161; text-transform:uppercase; letter-spacing:1.2px; margin-top:6px; font-weight:700;">Não ofertados</div>
                  </td>
                  <td width="25%" align="center" valign="middle" style="padding:18px 4px;">
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:28px; font-weight:700; color:#B71C1C; line-height:1;">{cotas_erro}</div>
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:10px; color:#616161; text-transform:uppercase; letter-spacing:1.2px; margin-top:6px; font-weight:700;">Falhas</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          {secao_html}

          <!-- CTA -->
          <tr>
            <td align="center" style="padding:32px 32px 12px 32px;">
              <!--[if mso]>
              <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{link_drive}" style="height:46px; v-text-anchor:middle; width:280px;" arcsize="13%" stroke="f" fillcolor="#B71C1C">
                <w:anchorlock/>
                <center style="color:#ffffff; font-family:'Segoe UI',Arial,sans-serif; font-size:14px; font-weight:700; letter-spacing:0.5px;">ACESSAR PASTA NO DRIVE</center>
              </v:roundrect>
              <![endif]-->
              <!--[if !mso]><!-->
              <a href="{link_drive}" target="_blank" style="background-color:#B71C1C; border-radius:6px; color:#FFFFFF; display:inline-block; font-family:'Segoe UI',Arial,sans-serif; font-size:14px; font-weight:700; line-height:46px; text-align:center; text-decoration:none; padding:0 36px; -webkit-text-size-adjust:none; letter-spacing:0.5px;">ACESSAR PASTA NO DRIVE</a>
              <!--<![endif]-->
            </td>
          </tr>

          <!-- Link alternativo -->
          <tr>
            <td align="center" style="padding:0 32px 28px 32px; font-family:'Segoe UI',Arial,sans-serif; font-size:11px; color:#9E9E9E; line-height:1.6;">
              Link alternativo:
              <a href="{link_drive}" target="_blank" style="color:#B71C1C; text-decoration:underline; word-break:break-all;">{link_drive}</a>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td bgcolor="#FAFAFA" style="padding:18px 32px; border-top:1px solid #EEEEEE; font-family:'Segoe UI',Arial,sans-serif; font-size:11px; color:#9E9E9E; line-height:1.6; text-align:center;">
              Este e-mail foi gerado automaticamente pelo sistema RPA &middot; <strong style="color:#B71C1C;">Ademicon</strong>
            </td>
          </tr>

        </table>

        <!-- Marca discreta abaixo do card -->
        <table role="presentation" width="640" border="0" cellspacing="0" cellpadding="0" style="max-width:640px;">
          <tr>
            <td align="center" style="padding:14px 12px 4px 12px; font-family:'Segoe UI',Arial,sans-serif; font-size:10px; color:#9E9E9E; letter-spacing:1px;">
              ADEMICON ADMINISTRADORA DE CONSÓRCIOS S/A
            </td>
          </tr>
        </table>

      </td>
    </tr>
  </table>
</body>
</html>
"""


# =========================================================
# ENVIO
# =========================================================

def enviar_email_lote_lance(id_fila_adm: int, modalidade: str, logger) -> None:
    row = _obter_resumo_lote_email(id_fila_adm)

    if not row:
        _falhar_email(
            logger,
            id_fila_adm,
            "Buscar metricas",
            "Lote nao encontrado em tbl_fila_adm",
        )

    (
        total_cotas,
        cotas_ofertadas,
        cotas_nao_ofertadas,
        cotas_erro,
        link_drive,
        email_destino,
        nome_adm,
    ) = row

    if not link_drive:
        _falhar_email(logger, id_fila_adm, "Validar link", "link_drive vazio")

    if not email_destino:
        _falhar_email(
            logger,
            id_fila_adm,
            "Validar email",
            f"ADM sem email cadastrado: {nome_adm}",
        )

    cotas_falha = _listar_cotas_falha(id_fila_adm)

    # Roteamento de destino:
    # - 0 falhas → email(s) do ADM + rpa.ademicon@gmail.com (copia interna)
    # - qualquer falha → rpa.ademicon@gmail.com apenas (cliente nao recebe)
    EMAIL_INTERNO = "rpa.ademicon@gmail.com"
    if int(cotas_erro or 0) > 0:
        email_destino = EMAIL_INTERNO
        logger.info(
            f"[EMAIL] {cotas_erro} falha(s) detectada(s) — redirecionando para {EMAIL_INTERNO}"
        )
    else:
        # Adiciona rpa.ademicon como copia interna se ainda nao estiver na lista
        emails_adm = [e.strip() for e in email_destino.split(",") if e.strip()]
        if EMAIL_INTERNO.lower() not in [e.lower() for e in emails_adm]:
            emails_adm.append(EMAIL_INTERNO)
        email_destino = ", ".join(emails_adm)
        logger.info(
            f"[EMAIL] 0 falhas — enviando para ADM + interno: {email_destino}"
        )

    modalidade_exibicao = normalizar_modalidade_exibicao(modalidade)
    mes_ref_banco = _obter_mes_ref_banco(id_fila_adm, logger)
    if mes_ref_banco is not None:
        mes_formatado = formatar_mes_extenso(mes_ref_banco)
    else:
        logger.warn(
            f"[EMAIL] Usando mes_ref de datetime.now() como fallback | id_fila_adm={id_fila_adm}"
        )
        mes_formatado = formatar_mes_extenso(int(datetime.now().strftime("%Y%m")))

    secao_txt, secao_html = _montar_secao_falhas(cotas_falha)

    assunto = (
        f"Ofertar Lance {modalidade_exibicao} — "
        f"{nome_adm} · {mes_formatado}"
    )

    corpo_txt = _montar_corpo_txt(
        nome_adm=nome_adm,
        modalidade_exibicao=modalidade_exibicao,
        mes_formatado=mes_formatado,
        total_cotas=total_cotas or 0,
        cotas_ofertadas=cotas_ofertadas or 0,
        cotas_nao_ofertadas=cotas_nao_ofertadas or 0,
        cotas_erro=cotas_erro or 0,
        link_drive=link_drive,
        secao_txt=secao_txt,
    )

    corpo_html = _montar_corpo_html(
        nome_adm=nome_adm,
        modalidade_exibicao=modalidade_exibicao,
        mes_formatado=mes_formatado,
        total_cotas=total_cotas or 0,
        cotas_ofertadas=cotas_ofertadas or 0,
        cotas_nao_ofertadas=cotas_nao_ofertadas or 0,
        cotas_erro=cotas_erro or 0,
        link_drive=link_drive,
        secao_html=secao_html,
    )

    try:
        service = criar_servico_gmail()

        msg = MIMEMultipart("alternative")
        msg["to"] = email_destino
        msg["subject"] = assunto
        msg.attach(MIMEText(corpo_txt, "plain", "utf-8"))
        msg.attach(MIMEText(corpo_html, "html", "utf-8"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()

        logger.info(
            f"[EMAIL OK] Enviado para {email_destino} | "
            f"modalidade={modalidade_exibicao} cotas_falha={len(cotas_falha)}"
        )
    except Exception as e:
        logger.error(
            f"[EMAIL] Falha ao enviar | id_fila_adm={id_fila_adm} | "
            f"{type(e).__name__}: {e}"
        )
        raise
