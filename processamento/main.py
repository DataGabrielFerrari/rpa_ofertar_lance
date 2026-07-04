"""
PROCESSAMENTO de UMA cota.

Chamado pelo PAD em loop, uma vez por id_cota.
Argumentos:
  argv[1] = id_cota (int)
  argv[2] = caminho_log (opcional, sobrescreve o do banco)

Saida (stdout): JSON unica linha
{
  "status": "OFERTADO|JA_OFERTADO|FALHA|...",
  "observacao": str,
  "caminho_comprovante": str|null,
  "caminho_print_falha": str|null,
  "valor_pagar": int|null
}

Logs detalhados vao para stderr e/ou para o log.txt do lote (NAO stdout).
"""

import os
import sys
import time
import traceback
import json

# Forca stdout/stderr em UTF-8 para evitar mojibake quando o PAD
# captura a saida via PowerShell (default do Windows e cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests
from playwright.sync_api import sync_playwright

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))   # processamento
ROOT_DIR    = os.path.dirname(CURRENT_DIR)                 # rpa_ofertar_lance

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

try:
    from dotenv import load_dotenv
    ENV_PATH = os.path.join(ROOT_DIR, ".env")
    load_dotenv(ENV_PATH, override=True)
except Exception:
    pass

from db.db import fetchone, execute
from processamento.worker_avapro import rodar_worker_lance
from shared.log import Logger
# notificar_falha removido daqui — falhas de cota individual nao geram email
# (apenas falhas graves que travam a execucao enviam email: login, entrada, saida)


PORTA      = 9222
URL_AVAPRO = "https://avapro.ademicon.com.br"


# =========================================================
# HELPERS DE SAIDA
# =========================================================

def _payload_base(
    status: str,
    observacao: str,
    caminho_comprovante=None,
    caminho_print_falha=None,
    valor_pagar=None,
) -> dict:
    return {
        "status": status,
        "observacao": observacao,
        "caminho_comprovante": caminho_comprovante,
        "caminho_print_falha": caminho_print_falha,
        "valor_pagar": valor_pagar,
    }


def _payload_falha(observacao: str, caminho_print_falha=None) -> dict:
    return _payload_base(
        status="FALHA",
        observacao=observacao,
        caminho_comprovante=None,
        caminho_print_falha=caminho_print_falha,
        valor_pagar=None,
    )


def _emitir_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# =========================================================
# CONEXAO COM O EDGE / AVAPRO
# =========================================================

def esperar_cdp(porta: int = PORTA, timeout: int = 10) -> bool:
    inicio = time.time()
    while time.time() - inicio < timeout:
        try:
            r = requests.get(f"http://127.0.0.1:{porta}/json/version", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def conectar_pagina_avapro(playwright, logger=None):
    """
    Conecta ao Edge via CDP, encontra a aba do AVAPRO e FECHA todas
    as demais — garante que so UMA aba fique aberta a cada cota
    (mesmo padrao do achar_aba_avapro do rpa_gerar_boleto).

    Com duas abas do AVAPRO abertas, o worker pode agir numa aba
    enquanto a tela responde na outra, estourando timeout
    (ex.: sinal pos-Continuar nunca chega).

    Prioridade para escolha:
      1) Aba em qualquer URL avapro.ademicon.com.br
      2) Primeira aba nao-blank
      3) Primeira aba qualquer
    """
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{PORTA}")

    if not browser.contexts:
        raise RuntimeError("Nenhum contexto encontrado no Edge.")

    context = browser.contexts[0]
    paginas = list(context.pages)
    escolhida = None

    # Prioridade 1: qualquer AVAPRO
    for p in paginas:
        try:
            if "avapro.ademicon.com.br" in (p.url or "").lower():
                escolhida = p
                break
        except Exception:
            continue

    # Prioridade 2: primeira nao-blank
    if escolhida is None:
        for p in paginas:
            try:
                url = (p.url or "").lower().strip()
                if url and url not in (
                    "about:blank", "edge://newtab/", "chrome://newtab/"
                ):
                    escolhida = p
                    break
            except Exception:
                continue

    # Prioridade 3: primeira qualquer
    if escolhida is None and paginas:
        escolhida = paginas[0]

    if escolhida is None:
        raise RuntimeError("Nenhuma aba do AVAPRO encontrada.")

    # Fecha TODAS as abas extras — so a escolhida sobrevive
    for p in list(context.pages):
        if p is escolhida:
            continue
        url_fechar = ""
        try:
            url_fechar = (p.url or "")
        except Exception:
            pass
        try:
            p.close()
            if logger is not None:
                logger.info(f"[MAIN] Aba extra fechada | url={url_fechar}")
        except Exception as e:
            if logger is not None:
                logger.warn(f"[MAIN] Falha ao fechar aba extra ({url_fechar}): {e}")

    return browser, escolhida


# =========================================================
# CONTEXTO DA COTA
# =========================================================

def obter_contexto_por_cota(id_cota: int) -> dict:
    sql = """
        SELECT
            fc.id_fila_adm,
            fa.modalidade,
            fa.caminho_base,
            fa.caminho_log
        FROM tbl_fila_cotas fc
        INNER JOIN tbl_fila_adm fa ON fa.id_fila_adm = fc.id_fila_adm
        WHERE fc.id_cota = %s
    """
    row = fetchone(sql, (id_cota,))

    if not row:
        raise ValueError(f"Cota nao encontrada no banco: id_cota={id_cota}")

    id_fila_adm  = int(row[0])
    modalidade   = str(row[1] or "").strip().upper()
    caminho_base = str(row[2] or "").strip()
    caminho_log  = str(row[3] or "").strip()

    if not caminho_base:
        raise ValueError(f"caminho_base vazio para id_fila_adm={id_fila_adm}")

    evidencias = os.path.join(caminho_base, "evidencias")
    caminhos = {
        "raiz":         caminho_base,
        "evidencias":   evidencias,
        "ofertados":    os.path.join(evidencias, "OFERTADOS"),
        "ja_ofertados": os.path.join(evidencias, "JA_OFERTADOS"),
        "falha":        os.path.join(evidencias, "FALHA"),
        "log":          caminho_log,
    }

    return {
        "id_fila_adm": id_fila_adm,
        "modalidade":  modalidade,
        "caminhos":    caminhos,
        "caminho_log": caminho_log,
    }


# =========================================================
# EXECUCAO
# =========================================================

COOLDOWN_FALHA_MINUTOS = 10  # minutos de espera entre tentativas apos FALHA


def _cota_em_cooldown(id_cota: int, logger: Logger) -> tuple:
    """
    Verifica se a cota esta em cooldown pos-FALHA.

    Usa hora_atualizado como referencia — e o campo atualizado pelo PAD
    quando finaliza a cota como FALHA.

    Retorna (em_cooldown: bool, minutos_restantes: int).
    """
    try:
        row = fetchone(
            """
            SELECT status,
                   EXTRACT(EPOCH FROM (NOW() - hora_atualizado)) / 60
                     AS minutos_desde_falha
            FROM tbl_fila_cotas
            WHERE id_cota = %s
            """,
            (id_cota,),
        )
    except Exception as e:
        logger.warn(f"[COOLDOWN] Erro ao consultar hora_atualizado: {e} — ignorando cooldown")
        return False, 0

    if not row:
        return False, 0

    status          = str(row[0] or "").strip()
    minutos_passados = float(row[1] or 0)

    if status == "FALHA" and minutos_passados < COOLDOWN_FALHA_MINUTOS:
        restantes = max(1, int(COOLDOWN_FALHA_MINUTOS - minutos_passados))
        logger.info(
            f"[COOLDOWN] Cota {id_cota} em cooldown | "
            f"ultima falha ha {minutos_passados:.1f}min | faltam ~{restantes}min"
        )
        return True, restantes

    return False, 0


def _processar(id_cota: int, caminho_log_argv, logger: Logger) -> dict:
    ctx = obter_contexto_por_cota(id_cota)

    id_fila_adm = ctx["id_fila_adm"]
    modalidade  = ctx["modalidade"]
    caminhos    = ctx["caminhos"]
    caminho_log = caminho_log_argv or ctx["caminho_log"]

    if caminho_log:
        logger.configurar_arquivo(
            caminho_log,
            cabecalho=f"ETAPA=WORKER id_cota={id_cota} id_fila_adm={id_fila_adm}",
        )

    logger.info(
        f"[MAIN] id_cota={id_cota} id_fila_adm={id_fila_adm} modalidade={modalidade}"
    )

    # ---------------------------------------------------------
    # COOLDOWN: nao retentar cota FALHA antes de 10 minutos
    # Checagem ANTES do UPDATE de tentativas para nao consumir
    # uma tentativa desnecessaria nem abrir o browser.
    # ---------------------------------------------------------
    em_cooldown, minutos_restantes = _cota_em_cooldown(id_cota, logger)
    if em_cooldown:
        obs = f"AGUARDAR: cota em cooldown pos-FALHA (faltam ~{minutos_restantes}min)"
        logger.info(f"[MAIN] {obs} | id_cota={id_cota}")
        return _payload_base(
            status="AGUARDAR",
            observacao=obs,
        )

    execute(
        """
        UPDATE tbl_fila_cotas
        SET status = 'PROCESSANDO',
            tentativas = COALESCE(tentativas, 0) + 1,
            hora_atualizado = NOW()
        WHERE id_cota = %s
        """,
        (id_cota,),
    )
    logger.info(f"[MAIN] Cota marcada como PROCESSANDO | id_cota={id_cota}")

    if not esperar_cdp(PORTA, timeout=10):
        raise RuntimeError(
            f"Edge nao acessivel na porta {PORTA}. "
            "Execute login.py antes de chamar este script."
        )

    pw = sync_playwright().start()

    try:
        browser, page = conectar_pagina_avapro(pw, logger=logger)
        logger.info(f"[MAIN] Conectado | url={page.url}")

        resultado = rodar_worker_lance(
            page=page,
            id_cota=id_cota,
            id_fila_adm=id_fila_adm,
            modalidade=modalidade,
            caminhos=caminhos,
            logger=logger,
        )

        if not resultado:
            logger.warn("[MAIN] Worker retornou None - emitindo saida de seguranca")
            return _payload_falha("WORKER NAO RETORNOU RESULTADO")

        # Garante que tem todos os campos esperados pelo PAD
        return _payload_base(
            status=resultado.get("status") or "FALHA",
            observacao=resultado.get("observacao") or "",
            caminho_comprovante=resultado.get("caminho_comprovante"),
            caminho_print_falha=resultado.get("caminho_print_falha"),
            valor_pagar=resultado.get("valor_pagar"),
        )

    finally:
        try:
            pw.stop()
        except Exception:
            pass


def main() -> int:
    logger = Logger()

    if len(sys.argv) < 2:
        _emitir_json(_payload_falha("Uso: main.py <id_cota> [caminho_log]"))
        return 1

    try:
        id_cota = int(str(sys.argv[1]).strip())
    except Exception:
        _emitir_json(_payload_falha(f"id_cota invalido: {sys.argv[1]!r}"))
        return 1

    caminho_log_argv = (
        str(sys.argv[2]).strip()
        if len(sys.argv) >= 3 and str(sys.argv[2]).strip()
        else None
    )

    try:
        payload = _processar(id_cota, caminho_log_argv, logger)
        _emitir_json(payload)
        return 0 if payload["status"] != "FALHA" else 1

    except Exception as e:
        # Falha de cota individual: loga, NAO envia email.
        # O usuario vê o resultado consolidado no email de saída do lote.
        _stderr(traceback.format_exc())
        try:
            logger.error(f"[MAIN] ERRO GERAL: {e}")
            logger.error(traceback.format_exc())
        except Exception:
            pass

        _emitir_json(_payload_falha(f"{type(e).__name__}: {e}"))
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        _stderr(traceback.format_exc())
        try:
            _emitir_json(_payload_falha(
                f"Excecao toplevel: {type(e).__name__}: {e}"
            ))
        except Exception:
            pass
        sys.exit(1)
