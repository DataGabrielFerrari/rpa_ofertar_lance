"""
SAIDA / pos-processamento. Chamado pelo PAD 1x ao final do lote.

Argumentos:
  argv[1] = id_fila_adm (int)

Saida (stdout): JSON unica linha
{
  "status": "SUCESSO|FALHA",
  "observacao": str,
  "cells_atualizadas": int,
  "link_drive": str|null,
  "email_enviado": bool,
  "etapas": {"planilha": "OK|ERRO|NAO_EXECUTADO", "drive": "...", "email": "..."}
}
"""

import json
import os
import sys
import traceback

# Forca stdout/stderr em UTF-8 para evitar mojibake quando o PAD
# captura a saida via PowerShell (default do Windows e cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR    = os.path.dirname(CURRENT_DIR)

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

try:
    from dotenv import load_dotenv
    ENV_PATH = os.path.join(ROOT_DIR, ".env")
    load_dotenv(ENV_PATH, override=True)
except Exception:
    pass

from db.db import fetchone
from db.funcoes import fechar_lote_adm
from saida.pos_processamento import rodar_pos_processamento
from saida.reexecucao import (
    tem_cotas_falha,
    ativar_flag_reexecucao,
    lote_era_reexecucao,
)
from shared.log import Logger
from shared.notificador import notificar_falha


# =========================================================
# HELPERS DE SAIDA
# =========================================================

def _emitir(status: str, observacao: str, **extras) -> None:
    payload = {"status": status, "observacao": observacao, **extras}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


_ETAPAS_NAO_EXECUTADO = {
    "planilha": "NAO_EXECUTADO",
    "drive": "NAO_EXECUTADO",
    "email": "NAO_EXECUTADO",
    "relatorio_interno": "NAO_EXECUTADO",
}


def _payload_falha_dict(observacao: str, etapas: dict | None = None) -> dict:
    return {
        "status": "FALHA",
        "observacao": observacao,
        "cells_atualizadas": 0,
        "link_drive": None,
        "email_enviado": False,
        "etapas": etapas if etapas is not None else dict(_ETAPAS_NAO_EXECUTADO),
    }


# =========================================================
# CONTEXTO E FECHAMENTO
# =========================================================

def _obter_contexto_lote(id_fila_adm: int) -> dict:
    sql = """
        SELECT fa.modalidade, fa.caminho_log
        FROM tbl_fila_adm fa
        WHERE fa.id_fila_adm = %s
    """
    row = fetchone(sql, (id_fila_adm,))
    if not row:
        raise ValueError(f"Lote nao encontrado: id_fila_adm={id_fila_adm}")

    modalidade  = str(row[0] or "").strip().upper()
    caminho_log = str(row[1] or "").strip()

    if not modalidade:
        raise ValueError(f"modalidade vazia para id_fila_adm={id_fila_adm}")

    return {"modalidade": modalidade, "caminho_log": caminho_log}


def _contar_cotas_presas(id_fila_adm: int) -> dict:
    """
    Conta cotas que ficaram em PENDENTE/PROCESSANDO no fechamento do lote.
    Indica que o loop do PAD encerrou antes de finalizar todas as cotas
    (ex.: processo morto, exception nao tratada, sessao do AVAPRO caiu).
    """
    sql = """
        SELECT
            COUNT(*) FILTER (WHERE status = 'PENDENTE')    AS pendentes,
            COUNT(*) FILTER (WHERE status = 'PROCESSANDO') AS processando
        FROM tbl_fila_cotas
        WHERE id_fila_adm = %s
    """
    row = fetchone(sql, (id_fila_adm,))
    pendentes   = int(row[0]) if row and row[0] is not None else 0
    processando = int(row[1]) if row and row[1] is not None else 0
    return {
        "pendentes":   pendentes,
        "processando": processando,
        "total":       pendentes + processando,
    }


def _fechar_lote(id_fila_adm: int, status: str, observacao: str, logger: Logger) -> None:
    row = fechar_lote_adm(id_fila_adm, status, observacao)
    logger.info(
        f"[FECHAMENTO] Lote encerrado | "
        f"id_fila_adm={id_fila_adm} status={status} "
        f"id_adm={row[1] if row else '?'} "
        f"cotas_pendentes={row[7] if row else '?'}"
    )


# =========================================================
# MAIN
# =========================================================

def _executar(id_fila_adm: int, logger: Logger) -> dict:
    logger.info(f"[SAIDA] id_fila_adm={id_fila_adm}")

    ctx         = _obter_contexto_lote(id_fila_adm)
    modalidade  = ctx["modalidade"]
    caminho_log = ctx["caminho_log"]

    logger.info(
        f"[SAIDA] Contexto obtido | "
        f"modalidade={modalidade} caminho_log={caminho_log}"
    )

    if caminho_log:
        logger.configurar_arquivo(
            caminho_log,
            cabecalho=f"ETAPA=POS_PROCESSAMENTO id_fila_adm={id_fila_adm}",
        )
    else:
        logger.warn("[SAIDA] caminho_log vazio - logando apenas no console")

    logger.info(
        f"[SAIDA] Iniciando pos-processamento | "
        f"id_fila_adm={id_fila_adm} modalidade={modalidade}"
    )

    # Captura ANTES de fechar_lote_adm (que reseta a flag): indica se este
    # lote ja era uma reexecucao automatica. Limite = 1 reexecucao.
    try:
        era_reexecucao = lote_era_reexecucao(id_fila_adm, modalidade)
    except Exception as e_flag:
        logger.warn(f"[SAIDA] Falha ao verificar flag reexecucao: {e_flag}")
        era_reexecucao = False

    if era_reexecucao:
        logger.info(
            f"[SAIDA] Este lote e uma REEXECUCAO automatica | "
            f"id_fila_adm={id_fila_adm} (nova reexecucao automatica NAO sera agendada)"
        )

    resultado = rodar_pos_processamento(
        id_fila_adm=id_fila_adm,
        modalidade=modalidade,
        logger=logger,
    )

    etapas = resultado.get("etapas", dict(_ETAPAS_NAO_EXECUTADO))

    # Planilha e Drive são etapas críticas; email não determina status geral
    etapas_criticas_com_erro = [
        e for e in ("planilha", "drive") if etapas.get(e) == "ERRO"
    ]

    # Detecta cotas que ficaram para tras (loop do PAD encerrou antes de
    # finalizar todas). PENDENTE + PROCESSANDO no fechamento = lote falho.
    try:
        cotas_presas = _contar_cotas_presas(id_fila_adm)
    except Exception as e_cont:
        logger.warn(f"[SAIDA] Falha ao contar cotas presas: {e_cont}")
        cotas_presas = {"pendentes": 0, "processando": 0, "total": 0}

    motivos_falha = []
    if etapas_criticas_com_erro:
        motivos_falha.append(
            f"etapas com erro: {', '.join(etapas_criticas_com_erro)}"
        )
    if cotas_presas["total"] > 0:
        motivos_falha.append(
            f"{cotas_presas['total']} cota(s) nao finalizadas no fechamento "
            f"(PENDENTE={cotas_presas['pendentes']}, "
            f"PROCESSANDO={cotas_presas['processando']})"
        )

    if motivos_falha:
        observacao = "Pos-processamento concluido com falhas: " + " | ".join(motivos_falha)
        status_lote = "FALHA"
    else:
        observacao = "Pos-processamento concluido com sucesso"
        status_lote = "SUCESSO"

    _fechar_lote(id_fila_adm, status_lote, observacao, logger)

    # Ativa reexecucao no tbl_adm APOS fechar_lote_adm (que reseta a flag).
    # Com reexecucao=TRUE, a proxima chamada ao entrada priorizara este ADM
    # e o leitor so processara cotas marcadas como REEXECUTAR na planilha.
    # LIMITE: no maximo 1 reexecucao automatica — se este lote JA era uma
    # reexecucao e falhou de novo, a flag NAO e reativada (evita loop).
    try:
        if tem_cotas_falha(id_fila_adm):
            if era_reexecucao:
                logger.warn(
                    "[SAIDA] Limite de 1 reexecucao automatica atingido — "
                    "flag reexecucao NAO reativada. Cotas FALHA exigem "
                    "reexecucao manual (marcar REEXECUTAR na planilha)."
                )
            else:
                ativar_flag_reexecucao(id_fila_adm, modalidade, logger)
    except Exception as e_reexec:
        try:
            logger.warn(f"[SAIDA] Falha ao ativar flag reexecucao: {e_reexec}")
        except Exception:
            pass

    logger.info(
        f"[SAIDA] Concluido | id_fila_adm={id_fila_adm} "
        f"status={status_lote} "
        f"cells={resultado.get('cells_atualizadas', 0)} "
        f"link_drive={resultado.get('link_drive')} "
        f"email_enviado={resultado.get('email_enviado', False)} "
        f"etapas={etapas} "
        f"cotas_presas={cotas_presas}"
    )

    # Quando o lote vira FALHA por qualquer motivo (etapa critica ou cotas
    # presas), envia email do Python alem do que o PAD eventualmente
    # notificar - assim o operador recebe um email com log + script anexados,
    # com contexto completo do que falhou.
    if status_lote == "FALHA":
        try:
            erro_sintetico = RuntimeError(observacao)
            notificar_falha(
                etapa="SAIDA",
                erro=erro_sintetico,
                id_fila_adm=id_fila_adm,
                caminho_log=caminho_log,
                script_path=__file__,
                contexto_extra=(
                    f"etapas={etapas} "
                    f"cells_atualizadas={resultado.get('cells_atualizadas', 0)} "
                    f"link_drive={resultado.get('link_drive')} "
                    f"email_enviado={resultado.get('email_enviado', False)} "
                    f"cotas_presas={cotas_presas}"
                ),
            )
        except Exception as e_notif:
            try:
                logger.warn(f"[SAIDA] Falha ao notificar lote em FALHA: {e_notif}")
            except Exception:
                pass

    return {
        "status": status_lote,
        "observacao": observacao,
        "cells_atualizadas": resultado.get("cells_atualizadas", 0),
        "link_drive": resultado.get("link_drive"),
        "email_enviado": resultado.get("email_enviado", False),
        "etapas": etapas,
    }


def main() -> int:
    logger = Logger()
    logger.info("[SAIDA] Processo iniciado")

    if len(sys.argv) < 2:
        _emitir(**_payload_falha_dict("Uso: main.py <id_fila_adm>"))
        return 1

    try:
        id_fila_adm = int(str(sys.argv[1]).strip())
    except Exception:
        _emitir(**_payload_falha_dict(f"id_fila_adm invalido: {sys.argv[1]!r}"))
        return 1

    caminho_log_para_notificar = None

    try:
        payload = _executar(id_fila_adm, logger)
        _emitir(**payload)
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        _stderr(tb)
        try:
            logger.error(f"[SAIDA] ERRO FATAL: {e}")
            logger.error(f"[SAIDA] TRACEBACK:\n{tb}")
        except Exception:
            pass

        # tenta fechar o lote como FALHA mesmo com excecao
        try:
            _fechar_lote(id_fila_adm, "FALHA", str(e), logger)
        except Exception as e_fecha:
            try:
                logger.error(f"[SAIDA] Falha ao fechar lote no banco: {e_fecha}")
            except Exception:
                pass

        # tenta resgatar caminho_log via banco para anexar no email
        try:
            row = fetchone(
                "SELECT caminho_log FROM tbl_fila_adm WHERE id_fila_adm = %s",
                (id_fila_adm,),
            )
            if row and row[0]:
                caminho_log_para_notificar = row[0]
        except Exception:
            pass

        try:
            notificar_falha(
                etapa="SAIDA",
                erro=e,
                id_fila_adm=id_fila_adm,
                caminho_log=caminho_log_para_notificar,
                script_path=__file__,
                contexto_extra=f"id_fila_adm={id_fila_adm}",
            )
        except Exception:
            pass

        _emitir(**_payload_falha_dict(f"{type(e).__name__}: {e}"))
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        _stderr(traceback.format_exc())
        try:
            _emitir(**_payload_falha_dict(
                f"Excecao toplevel: {type(e).__name__}: {e}"
            ))
        except Exception:
            pass
        sys.exit(1)
