"""
LOGIN no AVAPRO. Chamado pelo PAD 1x antes do loop de cotas.

Argumentos:
  argv[1] = id_fila_adm (int)

Saida (stdout): JSON unica linha
{
  "status": "SUCESSO|FALHA",
  "observacao": str
}

Comportamento em erro grave de login:
- detecta tela de login persistente / dialog de erro
- tira screenshot na pasta Evidencias do lote
- marca lote como FALHA no banco
- envia email com log + script + screenshot via notificar_falha
- limpa Edge para nao deixar processo zumbi
- retorna FALHA para o PAD
"""

import os
import sys
import json
import time
import subprocess
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

# Forca stdout/stderr em UTF-8 para evitar mojibake quando o PAD
# captura a saida via PowerShell (default do Windows e cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))   # processamento
ROOT_DIR = os.path.dirname(CURRENT_DIR)                    # rpa_ofertar_lance

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

try:
    from dotenv import load_dotenv
    ENV_PATH = os.path.join(ROOT_DIR, ".env")
    load_dotenv(ENV_PATH, override=True)
except Exception:
    pass

import requests
from playwright.sync_api import sync_playwright, expect

from db.db import get_conn
from db.funcoes import (
    obter_credenciais_adm_por_fila,
    obter_dados_adm_por_fila,
    finalizar_fila_adm,
    obter_url,
)
from shared.log import Logger
from shared.notificador import notificar_falha


URL_PADRAO = "https://avapro.ademicon.com.br/login"
PORTA_DEBUG = 9222


# ============================================================
# EXCEPTION ESPECIALIZADA
# ============================================================

class LoginGraveError(Exception):
    """
    Erro grave de login (credenciais invalidas, popup de erro, timeout).
    Carrega o caminho do screenshot capturado na hora do erro.
    """
    def __init__(self, mensagem: str, caminho_print: Optional[str] = None):
        super().__init__(mensagem)
        self.caminho_print = caminho_print


# ============================================================
# HELPERS DE SAIDA / LOG
# ============================================================

def _emitir_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _payload(status: str, observacao: str) -> dict:
    return {"status": status, "observacao": observacao}


# ============================================================
# CONTROLE DO EDGE
# ============================================================

def _existe_edge_rodando() -> bool:
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
        saida = ((r.stdout or "") + "\n" + (r.stderr or "")).lower()
        return "msedge.exe" in saida
    except Exception:
        return False


def _esperar_edge_morrer(timeout: int = 15) -> bool:
    inicio = time.time()
    while time.time() - inicio < timeout:
        if not _existe_edge_rodando():
            return True
        time.sleep(0.5)
    return False


def _matar_edge_total():
    """
    Fecha TODO o Edge: janelas normais, abas, popups, processos filhos.
    ATENCAO: fecha qualquer Edge aberto na maquina.
    """
    comandos = [
        ["taskkill", "/F", "/T", "/IM", "msedge.exe"],
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Process msedge -ErrorAction SilentlyContinue | Stop-Process -Force",
        ],
    ]

    for cmd in comandos:
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    if not _esperar_edge_morrer(timeout=15):
        raise RuntimeError(
            "Nao consegui encerrar totalmente o Microsoft Edge antes do login."
        )

    time.sleep(2)


def _esperar_cdp(porta: int = PORTA_DEBUG, timeout: int = 20) -> bool:
    inicio = time.time()
    while time.time() - inicio < timeout:
        try:
            r = requests.get(f"http://127.0.0.1:{porta}/json/version", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _resolver_edge_path() -> str:
    candidatos = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidatos:
        if os.path.exists(c):
            return c
    raise FileNotFoundError("msedge.exe nao encontrado")


# ============================================================
# CONSULTAS AO BANCO
# ============================================================

def _obter_credenciais_para_login(id_fila_adm: int) -> Tuple[str, str]:
    cred = obter_credenciais_adm_por_fila(id_fila_adm)
    if not cred:
        raise ValueError("Nao consegui obter credenciais para o lote")

    # Tenta acesso por chave; se a row vier como tupla, cai pra indices
    if hasattr(cred, "keys"):
        matricula = str(cred.get("matricula") or "").strip()
        senha = str(cred.get("senha") or "").strip()
    else:
        try:
            matricula = str(cred[2] or "").strip()
            senha = str(cred[3] or "").strip()
        except Exception:
            matricula = ""
            senha = ""

    if not matricula or not senha:
        # Fallback direto na tbl_adm caso a function nao retorne os campos esperados
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT a.matricula, a.senha
                    FROM tbl_adm a
                    INNER JOIN tbl_fila_adm f ON a.id_adm = f.id_adm
                    WHERE f.id_fila_adm = %s
                    """,
                    (id_fila_adm,),
                )
                row = cur.fetchone()
        if row:
            matricula = str(row[0] or "").strip()
            senha = str(row[1] or "").strip()

    if not matricula or not senha:
        raise ValueError(f"Matricula ou senha vazias para id_fila_adm={id_fila_adm}")

    return matricula, senha


def _get_dados_lote(id_fila_adm: int) -> Optional[dict]:
    try:
        return obter_dados_adm_por_fila(id_fila_adm)
    except Exception:
        return None


def _get_caminho_log_e_base(id_fila_adm: int) -> Tuple[Optional[str], Optional[str]]:
    dados = _get_dados_lote(id_fila_adm)
    if not dados:
        return None, None

    if hasattr(dados, "keys"):
        return dados.get("caminho_log"), dados.get("caminho_base")

    # Tupla retornada pela function obter_dados_adm_por_fila;
    # buscamos diretamente na tbl_fila_adm para garantir.
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT caminho_log, caminho_base FROM tbl_fila_adm WHERE id_fila_adm = %s",
                    (id_fila_adm,),
                )
                row = cur.fetchone()
        if row:
            return (row[0] or None, row[1] or None)
    except Exception:
        pass

    return None, None


# ============================================================
# SCREENSHOT / EVIDENCIA
# ============================================================

def _garantir_pasta_evidencias(caminho_base: Optional[str]) -> Path:
    if caminho_base:
        pasta = Path(caminho_base) / "evidencias" / "FALHA_LOGIN"
    else:
        pasta = Path(ROOT_DIR) / "Lotes" / "FALHA_LOGIN"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def _capturar_print(page, caminho_base: Optional[str], prefixo: str) -> Optional[str]:
    try:
        pasta = _garantir_pasta_evidencias(caminho_base)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        caminho = pasta / f"{prefixo}_{ts}.png"
        page.screenshot(path=str(caminho), full_page=True)
        return str(caminho)
    except Exception:
        return None


# ============================================================
# NAVEGACAO / LOGIN
# ============================================================

def _achar_pagina_login(context, url: str, logger: Optional["Logger"] = None):
    """
    Garante que reste apenas UMA aba no contexto, na URL alvo.

    Quando o Edge sobe com `--remote-debugging-port=9222 [URL]`, as vezes
    ele cria uma aba `about:blank` ou `edge://newtab/` extra antes da
    aba do AVAPRO terminar de carregar. Esse helper:

      1) Espera 2s para o Edge terminar de criar as abas iniciais.
      2) Identifica a melhor candidata por prioridade:
         a) aba ja na URL de login do AVAPRO
         b) aba ja em qualquer URL do dominio avapro.ademicon.com.br
         c) primeira aba nao-blank
         d) primeira aba qualquer
         e) cria uma nova se nao houver nenhuma
      3) Se a escolhida nao esta no dominio alvo, navega para a URL.
      4) FECHA todas as outras abas do contexto.
      5) Retorna a unica aba sobrevivente.

    Falha ao fechar uma aba extra nao interrompe o fluxo (apenas loga).
    """
    # 1) Aguarda Edge estabilizar suas abas iniciais
    time.sleep(2)

    paginas = list(context.pages)
    page_escolhida = None

    # 2a) Prioridade: aba ja na URL exata de login
    for p in paginas:
        try:
            if "avapro.ademicon.com.br/login" in (p.url or "").lower():
                page_escolhida = p
                break
        except Exception:
            continue

    # 2b) Prioridade: aba em qualquer URL do dominio
    if page_escolhida is None:
        for p in paginas:
            try:
                if "avapro.ademicon.com.br" in (p.url or "").lower():
                    page_escolhida = p
                    break
            except Exception:
                continue

    # 2c) Prioridade: primeira aba nao-blank (descarta about:blank, newtab)
    if page_escolhida is None:
        for p in paginas:
            try:
                cur = (p.url or "").lower().strip()
                if cur and cur not in ("about:blank", "edge://newtab/", "chrome://newtab/"):
                    page_escolhida = p
                    break
            except Exception:
                continue

    # 2d) Prioridade: primeira aba qualquer
    if page_escolhida is None and paginas:
        page_escolhida = paginas[0]

    # 2e) Cria nova se contexto vazio
    if page_escolhida is None:
        page_escolhida = context.new_page()

    # 3) Garante que a aba escolhida esta na URL correta
    try:
        url_atual = (page_escolhida.url or "").lower()
    except Exception:
        url_atual = ""

    if "avapro.ademicon.com.br" not in url_atual:
        try:
            page_escolhida.goto(url, wait_until="load", timeout=30000)
        except Exception:
            pass

    # 4) Fecha todas as outras abas
    for p in list(context.pages):
        if p is page_escolhida:
            continue
        try:
            url_fechar = ""
            try:
                url_fechar = (p.url or "")
            except Exception:
                pass
            p.close()
            if logger is not None:
                try:
                    logger.info(f"[LOGIN] Aba extra fechada | url={url_fechar}")
                except Exception:
                    pass
        except Exception as e:
            if logger is not None:
                try:
                    logger.warn(f"[LOGIN] Falha ao fechar aba extra: {e}")
                except Exception:
                    pass

    return page_escolhida


def _detectar_mensagem_erro_login(page) -> Optional[str]:
    """
    Procura na pagina por mensagem de erro de credenciais que o AVAPRO
    exibe inline apos o clique em Entrar, ex.:
        <p class="mt-1 text-sm text-red-600">Usuário ou senha inválida</p>

    A busca e por TEXTO (case-insensitive, com/sem acento), nao depende
    da classe CSS. Retorna o texto da mensagem encontrada ou None.
    """
    candidatos = [
        "Usuário ou senha inválida",
        "Usuário ou senha invalida",
        "Usuario ou senha inválida",
        "Usuario ou senha invalida",
        "usuário ou senha inválida",
        "usuario ou senha invalida",
    ]

    for texto in candidatos:
        try:
            loc = page.get_by_text(texto, exact=False)
            if loc.count() > 0:
                try:
                    real = (loc.first.inner_text() or "").strip()
                    return real or texto
                except Exception:
                    return texto
        except Exception:
            continue

    return None


def _esperar_resultado_login(page_alvo, caminho_base: Optional[str], timeout_s: int = 180) -> str:
    """
    Clica 'Entrar' e aguarda ate timeout_s segundos (padrao 3 min) por:

      1) URL saiu de /login E texto 'Meus Clientes' ficou visivel -> SUCESSO
      2) Mensagem de erro inline (credenciais invalidas)           -> LoginGraveError
      3) Timeout sem nenhum dos dois                               -> LoginGraveError
    """
    try:
        page_alvo.get_by_role("button", name="Entrar").click()
    except Exception as e:
        caminho_print = _capturar_print(page_alvo, caminho_base, "LOGIN_BOTAO")
        raise LoginGraveError(
            f"Nao consegui clicar no botao Entrar: {e}",
            caminho_print,
        )

    inicio = time.time()
    saiu_do_login = False
    url_atual = ""

    while time.time() - inicio < timeout_s:
        try:
            url_atual = (page_alvo.url or "").lower()
        except Exception:
            url_atual = ""

        # 1a) Ainda na tela de login?
        if not saiu_do_login:
            if url_atual and "avapro.ademicon.com.br/login" not in url_atual:
                saiu_do_login = True

        # 1b) Apos sair do /login, aguarda 'Meus Clientes' ficar visivel
        if saiu_do_login:
            try:
                visivel = page_alvo.locator("text=Meus Clientes").first.is_visible()
            except Exception:
                visivel = False
            if visivel:
                return "Login realizado com sucesso"

        # 2) Mensagem de erro inline de credenciais?
        msg_erro = _detectar_mensagem_erro_login(page_alvo)
        if msg_erro:
            page_alvo.wait_for_timeout(300)
            caminho_print = _capturar_print(
                page_alvo, caminho_base, "LOGIN_CREDENCIAIS_INVALIDAS"
            )
            raise LoginGraveError(msg_erro, caminho_print)

        page_alvo.wait_for_timeout(500)

    # 3) Timeout
    caminho_print = _capturar_print(page_alvo, caminho_base, "LOGIN_TIMEOUT")
    raise LoginGraveError(
        f"Login nao confirmou em {timeout_s}s — 'Meus Clientes' nao apareceu "
        f"(saiu_do_login={saiu_do_login}, url={url_atual!r})",
        caminho_print,
    )


def _fechar_abas_duplicadas_pos_login(context, page_alvo, logger: Logger) -> None:
    """
    Apos o login, garante que reste apenas a aba do robo (page_alvo).

    O AVAPRO/Edge pode abrir uma segunda aba durante ou apos o login
    (popup, target=_blank, restauracao de sessao). Com duas abas abertas,
    o worker pode conectar numa aba enquanto a tela responde na outra,
    causando timeout (ex.: sinal pos-Continuar nunca chega).

    Falha ao fechar uma aba nao interrompe o fluxo (apenas loga).
    """
    try:
        time.sleep(2)  # aguarda eventual popup/aba pos-login terminar de abrir
        for p in list(context.pages):
            if p is page_alvo:
                continue
            url_fechar = ""
            try:
                url_fechar = (p.url or "")
            except Exception:
                pass
            try:
                p.close()
                logger.info(f"[LOGIN] Aba duplicada pos-login fechada | url={url_fechar}")
            except Exception as e:
                logger.warn(f"[LOGIN] Falha ao fechar aba duplicada pos-login ({url_fechar}): {e}")
    except Exception as e:
        logger.warn(f"[LOGIN] Erro na limpeza de abas pos-login: {e}")


def _clicar_x_pos_login_se_existir(page, logger: Logger) -> None:
    """
    Valida se aparece o botao X depois do login.
    Se aparecer em ate 3 segundos, clica.
    Se nao aparecer, segue normal.
    """
    try:
        botao_x = page.locator(
            'button:has(svg path[d="M6 18 18 6M6 6l12 12"])'
        ).first

        botao_x.wait_for(state="visible", timeout=3000)
        botao_x.click(timeout=3000)

        logger.info("[LOGIN] Botao X pos-login apareceu e foi clicado.")

    except Exception:
        logger.info("[LOGIN] Botao X pos-login nao apareceu. Seguindo normal.")


def _abrir_menu_ofertar_lance(page, logger: Logger) -> None:
    """
    Apos login bem sucedido, navega para o menu 'Ofertar Lance'.
    Falha aqui nao deve marcar lote como FALHA - so loga.
    """
    try:
        logger.info("[NAV] Abrindo menu 'Ofertar Lance'...")
        page.locator("text=Ofertar Lance").first.wait_for(state="visible", timeout=20000)
        page.locator("text=Ofertar Lance").first.click()
    except Exception as e:
        logger.warn(f"[NAV] Nao consegui abrir 'Ofertar Lance': {e}")


# ============================================================
# CONSEQUENCIAS DE FALHA
# ============================================================

def _marcar_lote_falha(id_fila_adm: int, observacao: str, logger: Logger) -> None:
    try:
        finalizar_fila_adm(id_fila_adm, "FALHA", observacao)
        logger.info(f"[LOGIN] Lote marcado como FALHA | id_fila_adm={id_fila_adm}")
    except Exception as e:
        logger.error(f"[LOGIN] Falha ao marcar lote como FALHA: {e}")


# ============================================================
# EXECUCAO
# ============================================================

def _executar_login(id_fila_adm: int, logger: Logger) -> str:
    matricula, senha = _obter_credenciais_para_login(id_fila_adm)
    logger.info(f"[LOGIN] Credenciais obtidas | matricula={matricula}")

    url = obter_url() or URL_PADRAO
    logger.info(f"[LOGIN] URL alvo: {url}")

    _, caminho_base = _get_caminho_log_e_base(id_fila_adm)
    edge_path = _resolver_edge_path()
    logger.info(f"[LOGIN] Edge encontrado | path={edge_path}")

    logger.info("[LOGIN] Fechando TODO o Edge antigo...")
    _matar_edge_total()

    logger.info("[LOGIN] Abrindo Edge limpo com CDP...")
    subprocess.Popen(
        [
            edge_path,
            f"--remote-debugging-port={PORTA_DEBUG}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--start-maximized",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not _esperar_cdp(PORTA_DEBUG, 20):
        raise RuntimeError(f"Edge nao abriu CDP na porta {PORTA_DEBUG}")

    logger.info("[LOGIN] CDP disponivel, conectando Playwright")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORTA_DEBUG}")
        if not browser.contexts:
            raise RuntimeError("Nenhum contexto encontrado no Edge")

        context = browser.contexts[0]
        page_alvo = _achar_pagina_login(context, url, logger=logger)
        page_alvo.bring_to_front()
        page_alvo.wait_for_load_state("load")

        logger.info(f"[LOGIN] Aba encontrada | url={page_alvo.url}")

        # Garante que esta na tela de login
        if "avapro.ademicon.com.br" not in (page_alvo.url or "").lower():
            page_alvo.goto(url, wait_until="domcontentloaded", timeout=30000)

        inp_matricula = page_alvo.get_by_placeholder("Matrícula")
        inp_senha = page_alvo.get_by_placeholder("Senha")

        try:
            expect(inp_matricula).to_be_visible(timeout=15000)
            expect(inp_senha).to_be_visible(timeout=15000)
        except Exception as e:
            caminho_print = _capturar_print(page_alvo, caminho_base, "LOGIN_CAMPOS")
            raise LoginGraveError(
                f"Campos de matricula/senha nao apareceram: {e}",
                caminho_print,
            )

        inp_matricula.fill(matricula)
        inp_senha.fill(senha)

        msg = _esperar_resultado_login(page_alvo, caminho_base, timeout_s=180)
        logger.info(f"[LOGIN] {msg}")

        _fechar_abas_duplicadas_pos_login(context, page_alvo, logger)

        _clicar_x_pos_login_se_existir(page_alvo, logger)

        _abrir_menu_ofertar_lance(page_alvo, logger)

        # NAO FECHAR o browser - o PAD precisa enxergar a janela aberta e logada
        return msg


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    logger = Logger()

    if len(sys.argv) < 2:
        _emitir_json(_payload("FALHA", "argv[1] (id_fila_adm) ausente"))
        return 1

    try:
        id_fila_adm = int(str(sys.argv[1]).strip())
    except ValueError:
        _emitir_json(_payload("FALHA", f"id_fila_adm invalido: {sys.argv[1]!r}"))
        return 1

    # Configura o logger no caminho do lote (caminho_log vem do banco)
    caminho_log, _ = _get_caminho_log_e_base(id_fila_adm)
    if caminho_log:
        try:
            logger.configurar_arquivo(
                caminho_log,
                cabecalho=f"ETAPA=LOGIN_AVAPRO id_fila_adm={id_fila_adm}",
            )
        except Exception:
            pass

    logger.info(f"[LOGIN] Iniciando | id_fila_adm={id_fila_adm}")

    try:
        msg = _executar_login(id_fila_adm, logger)
        _emitir_json(_payload("SUCESSO", msg))
        return 0

    except Exception as e:
        _stderr(traceback.format_exc())
        try:
            logger.error(f"[LOGIN] ERRO: {e}")
            logger.error(traceback.format_exc())
        except Exception:
            pass

        observacao = f"{type(e).__name__}: {e}"
        caminho_print = getattr(e, "caminho_print", None)

        # 1) marca o lote como FALHA no banco
        _marcar_lote_falha(id_fila_adm, observacao, logger)

        # 2) envia email com anexos (log + script + screenshot)
        try:
            anexos = [caminho_print] if caminho_print else None
            notificar_falha(
                etapa="LOGIN",
                erro=e,
                id_fila_adm=id_fila_adm,
                caminho_log=caminho_log,
                script_path=__file__,
                contexto_extra=f"caminho_print={caminho_print or '-'}",
                anexos_extras=anexos,
            )
        except Exception:
            pass

        # 3) limpa Edge para nao deixar processo zumbi
        try:
            _matar_edge_total()
        except Exception:
            pass

        _emitir_json(_payload("FALHA", observacao))
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        _stderr(traceback.format_exc())
        try:
            _emitir_json(_payload("FALHA", f"Toplevel: {type(e).__name__}: {e}"))
        except Exception:
            pass
        sys.exit(1)