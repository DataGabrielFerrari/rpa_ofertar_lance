"""
Envia mensagem WhatsApp Web ao ADM apos execucao sem falhas.

Estrategia:
  1. Detecta o perfil Chrome onde rpa.ademicon@gmail.com esta logado,
     lendo os arquivos Preferences de cada perfil no User Data do Chrome.
  2. Fecha temporariamente as instancias Chrome que usam esse perfil
     (necessario para abrir uma nova instancia com CDP na mesma pasta).
  3. Abre Chrome com --remote-debugging-port=9224 e o perfil encontrado.
  4. Playwright conecta via CDP, navega para WhatsApp Web com a mensagem
     pre-preenchida e clica em Enviar.
  5. Fecha a instancia Chrome apos o envio.

Variaveis de ambiente opcionais:
  WHATSAPP_EMAIL_CONTA  - email da conta Chrome com WhatsApp logado
                          (default: rpa.ademicon@gmail.com)
  WHATSAPP_CDP_PORTA    - porta CDP (default: 9224)
  WHATSAPP_PERFIL       - nome fixo do perfil (ex: "Profile 1"), ignora auto-deteccao
"""

import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime
from typing import Optional

from playwright.sync_api import sync_playwright

from db.db import fetchone
from saida.envio_email import (
    formatar_mes_extenso,
    normalizar_modalidade_exibicao,
    _obter_resumo_lote_email,
    _obter_mes_ref_banco,
)


# =========================================================
# CONSTANTES
# =========================================================

CHROME_EXE = next(
    (p for p in [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ] if os.path.exists(p)),
    "chrome.exe",
)
CDP_PORTA_WHATSAPP = int(os.getenv("WHATSAPP_CDP_PORTA", "9224"))
EMAIL_CONTA_WPP    = os.getenv("WHATSAPP_EMAIL_CONTA", "rpa.ademicon@gmail.com")

# Diretorio User Data do Chrome principal
CHROME_USER_DATA = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Google", "Chrome", "User Data",
)

# Perfil fixo via variavel de ambiente (opcional — se vazio, auto-detecta)
WHATSAPP_PERFIL_FIXO = os.getenv("WHATSAPP_PERFIL", "").strip()


# =========================================================
# DETECCAO DO PERFIL CHROME
# =========================================================

def _encontrar_perfil_chrome(logger) -> Optional[str]:
    """
    Retorna o nome do subdiretorio de perfil (ex: 'Default', 'Profile 1')
    cujo arquivo Preferences contem o email EMAIL_CONTA_WPP.
    Retorna None se nao encontrado.
    """
    # Se perfil fixo definido via env, usa direto
    if WHATSAPP_PERFIL_FIXO:
        caminho = os.path.join(CHROME_USER_DATA, WHATSAPP_PERFIL_FIXO)
        if os.path.isdir(caminho):
            logger.info(
                f"[WHATSAPP] Usando perfil fixo (WHATSAPP_PERFIL={WHATSAPP_PERFIL_FIXO})"
            )
            return WHATSAPP_PERFIL_FIXO
        logger.warn(
            f"[WHATSAPP] Perfil fixo nao encontrado: {caminho} — tentando auto-deteccao"
        )

    if not os.path.isdir(CHROME_USER_DATA):
        logger.warn(f"[WHATSAPP] Chrome User Data nao encontrado: {CHROME_USER_DATA}")
        return None

    candidatos = ["Default"] + [f"Profile {i}" for i in range(1, 20)]
    for nome in candidatos:
        prefs_path = os.path.join(CHROME_USER_DATA, nome, "Preferences")
        if not os.path.isfile(prefs_path):
            continue
        try:
            with open(prefs_path, encoding="utf-8", errors="ignore") as f:
                prefs = json.load(f)
            for acc in prefs.get("account_info", []):
                if acc.get("email", "").lower() == EMAIL_CONTA_WPP.lower():
                    logger.info(
                        f"[WHATSAPP] Perfil encontrado: {nome} (conta: {EMAIL_CONTA_WPP})"
                    )
                    return nome
        except Exception:
            pass

    logger.warn(
        f"[WHATSAPP] Perfil com {EMAIL_CONTA_WPP} nao encontrado em {CHROME_USER_DATA}"
    )
    return None


# =========================================================
# FECHAR CHROME DO PERFIL (para liberar o lock)
# =========================================================

def _fechar_chrome_do_perfil(nome_perfil: str, logger) -> None:
    """
    Fecha TODAS as instancias Chrome para liberar o lock do User Data.
    Necessario para abrir uma nova instancia com --remote-debugging-port.

    NOTA: O Chrome pode ser reaberto normalmente pelo usuario apos o envio.
          Os dados (historico, favoritos, abas) NAO sao apagados.
          O Chrome restaura as abas automaticamente na proxima abertura.
    """
    try:
        resultado = subprocess.run(
            ["taskkill", "/F", "/IM", "chrome.exe"],
            capture_output=True, timeout=10,
        )
        saida = resultado.stdout.decode(errors="ignore").strip()
        logger.info(
            f"[WHATSAPP] Chrome encerrado (taskkill) | perfil={nome_perfil} | {saida or 'ok'}"
        )
    except Exception as e:
        logger.warn(f"[WHATSAPP] taskkill falhou: {e}")

    # Aguarda processos Chrome encerrarem completamente
    for _ in range(10):
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=5,
        )
        if b"chrome.exe" not in result.stdout:
            break
        time.sleep(0.5)
    else:
        logger.warn("[WHATSAPP] Alguns processos Chrome ainda em execucao apos taskkill")

    time.sleep(1.0)  # margem extra para locks de arquivo serem liberados


# =========================================================
# CHROME COM CDP
# =========================================================

def _porta_livre(porta: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", porta)) != 0


def _abrir_chrome_debug(nome_perfil: str, logger) -> Optional[subprocess.Popen]:
    """
    Abre Chrome com remote debugging usando o perfil que tem WhatsApp logado.
    Se a porta ja estiver ocupada, assume que Chrome debug ja esta ativo.
    """
    porta = CDP_PORTA_WHATSAPP

    if not _porta_livre(porta):
        logger.info(f"[WHATSAPP] Porta {porta} ja ativa — conectando via CDP")
        return None  # Playwright conecta direto; None = sem processo para fechar

    # Fechar Chrome do perfil para liberar o lock
    _fechar_chrome_do_perfil(nome_perfil, logger)

    cmd = [
        CHROME_EXE,
        f"--remote-debugging-port={porta}",
        f"--user-data-dir={CHROME_USER_DATA}",
        f"--profile-directory={nome_perfil}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "about:blank",
    ]

    logger.info(
        f"[WHATSAPP] Abrindo Chrome | perfil={nome_perfil} porta={porta}"
    )

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for _ in range(60):  # 30s max
            if not _porta_livre(porta):
                logger.info(f"[WHATSAPP] Chrome disponivel na porta {porta}")
                return proc
            time.sleep(0.5)

        logger.error(f"[WHATSAPP] Chrome nao respondeu em 30s na porta {porta}")
        proc.terminate()
        return None
    except Exception as e:
        logger.error(f"[WHATSAPP] Falha ao abrir Chrome: {e}")
        return None


# =========================================================
# MENSAGEM
# =========================================================

def _formatar_telefone(telefone: str) -> Optional[str]:
    """
    Normaliza para formato internacional sem '+' (ex: 5511999999999).
    Retorna None se o numero for invalido.
    """
    digitos = re.sub(r"\D", "", str(telefone or ""))

    if not digitos:
        return None

    if digitos.startswith("55") and len(digitos) >= 12:
        return digitos

    if len(digitos) in (10, 11):
        return "55" + digitos

    return None


def _montar_mensagem(
    nome_adm: str,
    modalidade: str,
    mes_formatado: str,
    cotas_ofertadas: int,
    cotas_nao_ofertadas: int,
    total_cotas: int,
    email_destino: str,
) -> str:
    modalidade_exib = normalizar_modalidade_exibicao(modalidade)

    emails = [e.strip() for e in (email_destino or "").split(",") if e.strip()]
    if len(emails) == 1:
        linha_email = f"📧 *{emails[0]}*"
    elif len(emails) > 1:
        lista = "\n".join(f"  • {e}" for e in emails)
        linha_email = f"📧 E-mails enviados para:\n{lista}"
    else:
        linha_email = "📧 Verifique seu e-mail"

    return (
        f"✅ *Ofertar Lance {modalidade_exib} — {mes_formatado}*\n\n"
        f"Olá {nome_adm}, o processamento foi concluído com *0 falhas*!\n\n"
        f"• Total processado: *{total_cotas}* cotas\n"
        f"• Ofertadas: *{cotas_ofertadas}*\n"
        f"• Não ofertadas: *{cotas_nao_ofertadas}*\n\n"
        f"Acesse seu e-mail para o relatório completo com os comprovantes.\n"
        f"{linha_email}"
    )


# =========================================================
# ENVIO VIA PLAYWRIGHT
# =========================================================

def _enviar_via_playwright(telefone_fmt: str, mensagem: str, logger) -> bool:
    """
    Conecta ao Chrome debug e envia a mensagem via WhatsApp Web.
    """
    texto_url = mensagem.replace(" ", "%20").replace("\n", "%0A").replace("*", "")
    url = f"https://web.whatsapp.com/send?phone={telefone_fmt}&text={texto_url}"

    porta = CDP_PORTA_WHATSAPP

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{porta}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            logger.info(f"[WHATSAPP] Navegando para WhatsApp | telefone={telefone_fmt}")
            page.goto(url, timeout=30_000)

            seletor_caixa = (
                'div[data-testid="conversation-compose-box-input"],'
                'div[contenteditable="true"][data-tab="10"],'
                'footer div[contenteditable="true"]'
            )

            try:
                page.wait_for_selector(seletor_caixa, timeout=30_000)
            except Exception:
                logger.warn("[WHATSAPP] Caixa de texto demorou — aguardando +15s")
                time.sleep(15)

            seletor_btn_enviar = (
                'button[data-testid="compose-btn-send"],'
                'span[data-testid="send"],'
                'button[aria-label="Enviar"]'
            )

            try:
                page.wait_for_selector(seletor_btn_enviar, timeout=10_000)
                page.click(seletor_btn_enviar)
            except Exception:
                logger.warn("[WHATSAPP] Botao Enviar nao encontrado — usando Enter")
                page.keyboard.press("Enter")

            time.sleep(3)

            logger.info(f"[WHATSAPP] Mensagem enviada | telefone={telefone_fmt}")
            page.close()
            browser.close()
            return True

    except Exception as e:
        logger.error(f"[WHATSAPP] Falha ao enviar via Playwright: {e}")
        return False


# =========================================================
# PONTO DE ENTRADA
# =========================================================

def enviar_whatsapp_conclusao(id_fila_adm: int, modalidade: str, logger) -> bool:
    """
    Envia mensagem WhatsApp ao ADM informando que o processamento
    terminou com 0 falhas e que deve checar o email.

    Retorna True se enviou com sucesso, False caso contrario.
    Nunca levanta excecao.
    """
    chrome_proc = None
    try:
        # 1. Dados do lote
        row = _obter_resumo_lote_email(id_fila_adm)
        if not row:
            logger.warn("[WHATSAPP] Lote nao encontrado — mensagem nao enviada")
            return False

        (
            total_cotas, cotas_ofertadas, cotas_nao_ofertadas, cotas_erro,
            _link_drive, _email, nome_adm,
        ) = row

        # 2. Telefone do ADM
        row_tel = fetchone(
            """
            SELECT a.telefone
            FROM tbl_fila_adm fa
            INNER JOIN tbl_adm a ON a.id_adm = fa.id_adm
            WHERE fa.id_fila_adm = %s
            """,
            (id_fila_adm,),
        )
        telefone_raw = str(row_tel[0] or "").strip() if row_tel else ""
        telefone_fmt = _formatar_telefone(telefone_raw)

        if not telefone_fmt:
            logger.warn(
                f"[WHATSAPP] Telefone invalido para id_fila_adm={id_fila_adm} "
                f"(telefone_raw={telefone_raw!r}) — mensagem nao enviada"
            )
            return False

        # 3. Mes de referencia
        mes_ref = _obter_mes_ref_banco(id_fila_adm, logger)
        mes_formatado = (
            formatar_mes_extenso(mes_ref) if mes_ref
            else datetime.now().strftime("%B/%Y")
        )

        # 4. Monta mensagem
        mensagem = _montar_mensagem(
            nome_adm=nome_adm or "ADM",
            modalidade=modalidade,
            mes_formatado=mes_formatado,
            cotas_ofertadas=int(cotas_ofertadas or 0),
            cotas_nao_ofertadas=int(cotas_nao_ofertadas or 0),
            total_cotas=int(total_cotas or 0),
            email_destino=str(_email or ""),
        )

        # 5. Detecta perfil Chrome com WhatsApp logado
        nome_perfil = _encontrar_perfil_chrome(logger)
        if not nome_perfil:
            logger.error(
                f"[WHATSAPP] Perfil Chrome com {EMAIL_CONTA_WPP} nao encontrado — "
                f"configure WHATSAPP_PERFIL no .env (ex: WHATSAPP_PERFIL=Profile 1)"
            )
            return False

        # 6. Abre Chrome com CDP (fecha instancias existentes do perfil se necessario)
        chrome_proc = _abrir_chrome_debug(nome_perfil, logger)
        if chrome_proc is None and _porta_livre(CDP_PORTA_WHATSAPP):
            logger.error(
                f"[WHATSAPP] Chrome nao disponivel na porta {CDP_PORTA_WHATSAPP}"
            )
            return False

        # 7. Envia via Playwright
        enviado = _enviar_via_playwright(telefone_fmt, mensagem, logger)
        return enviado

    except Exception as e:
        try:
            logger.error(f"[WHATSAPP] Erro inesperado: {e}")
        except Exception:
            pass
        return False

    finally:
        if chrome_proc:
            try:
                chrome_proc.terminate()
                chrome_proc.wait(timeout=5)
            except Exception:
                pass
