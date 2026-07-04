# saida/pos_processamento.py
from saida.atualizar_planilha import atualizar_planilha_lote
from saida.drive import processar_drive_lote
from saida.envio_email import enviar_email_lote_lance
from saida.relatorio_interno import gerar_relatorio_interno
from saida.reexecucao import (
    marcar_reexecutar_planilha,
    tem_cotas_falha,
    lote_era_reexecucao,
)
from saida.whatsapp import enviar_whatsapp_conclusao


def rodar_pos_processamento(id_fila_adm: int, modalidade: str, logger) -> dict:
    logger.info(f"[POS] Iniciando | id_fila_adm={id_fila_adm} modalidade={modalidade}")

    resultado = {
        "id_fila_adm": id_fila_adm,
        "modalidade": modalidade,
        "cells_atualizadas": 0,
        "link_drive": None,
        "email_enviado": False,
        "relatorio_interno_enviado": False,
        "reexecucao_planilha_marcada": False,
        "whatsapp_enviado": False,
        "etapas": {
            "planilha": "NAO_EXECUTADO",
            "drive": "NAO_EXECUTADO",
            "email": "NAO_EXECUTADO",
            "relatorio_interno": "NAO_EXECUTADO",
            "reexecucao_planilha": "NAO_EXECUTADO",
            "whatsapp": "NAO_EXECUTADO",
        },
    }

    # ── 1. Atualizar planilha ────────────────────────────────────────────────
    try:
        logger.info("[POS] ETAPA 1/6 — Atualizando planilha...")
        cells_atualizadas = atualizar_planilha_lote(id_fila_adm, logger)
        resultado["cells_atualizadas"] = cells_atualizadas
        resultado["etapas"]["planilha"] = "OK"
        logger.info(f"[POS] ETAPA 1/6 — Planilha atualizada | cells={cells_atualizadas}")
    except Exception as e:
        logger.error(f"[POS] ETAPA 1/6 — FALHA ao atualizar planilha | erro={e}")
        resultado["etapas"]["planilha"] = "ERRO"

    # ── 2. Drive ─────────────────────────────────────────────────────────────
    try:
        logger.info("[POS] ETAPA 2/6 — Processando Drive...")
        link_drive = processar_drive_lote(id_fila_adm, logger)
        resultado["link_drive"] = link_drive
        resultado["etapas"]["drive"] = "OK"
        logger.info(f"[POS] ETAPA 2/6 — Drive processado | link={link_drive}")
    except Exception as e:
        logger.error(f"[POS] ETAPA 2/6 — FALHA ao processar Drive | erro={e}")
        resultado["etapas"]["drive"] = "ERRO"

    # ── 3. Email para o ADM ──────────────────────────────────────────────────
    try:
        logger.info("[POS] ETAPA 3/6 — Enviando email ao ADM...")
        enviar_email_lote_lance(id_fila_adm, modalidade, logger)
        resultado["email_enviado"] = True
        resultado["etapas"]["email"] = "OK"
        logger.info("[POS] ETAPA 3/6 — Email ADM enviado com sucesso")
    except Exception as e:
        logger.error(f"[POS] ETAPA 3/6 — FALHA ao enviar email | erro={e}")
        resultado["email_enviado"] = False
        resultado["etapas"]["email"] = "ERRO"

    # ── 4. Relatorio interno de erros (rpa.ademicon@gmail.com) ───────────────
    try:
        logger.info("[POS] ETAPA 4/6 — Gerando relatorio interno de erros...")
        enviado = gerar_relatorio_interno(id_fila_adm, modalidade, logger)
        resultado["relatorio_interno_enviado"] = enviado
        resultado["etapas"]["relatorio_interno"] = "OK" if enviado else "ERRO"
        if enviado:
            logger.info("[POS] ETAPA 4/6 — Relatorio interno enviado com sucesso")
        else:
            logger.warn("[POS] ETAPA 4/6 — Relatorio interno nao enviado (ver log acima)")
    except Exception as e:
        logger.error(f"[POS] ETAPA 4/6 — FALHA no relatorio interno | erro={e}")
        resultado["etapas"]["relatorio_interno"] = "ERRO"

    # ── 5. Marcar cotas FALHA como REEXECUTAR na planilha (se houver) ────────
    # LIMITE: no maximo 1 reexecucao automatica. Se este lote JA e uma
    # reexecucao (flag reexecucao ainda TRUE no tbl_adm neste momento) e
    # falhou de novo, NAO marca REEXECUTAR — as cotas ficam como FALHA na
    # planilha e a reexecucao passa a ser manual. Evita loop infinito.
    try:
        logger.info("[POS] ETAPA 5/6 — Verificando cotas FALHA para reexecucao...")
        if tem_cotas_falha(id_fila_adm):
            if lote_era_reexecucao(id_fila_adm, modalidade):
                resultado["etapas"]["reexecucao_planilha"] = "LIMITE_ATINGIDO"
                logger.warn(
                    "[POS] ETAPA 5/6 — Lote ja era reexecucao e falhou de novo; "
                    "limite de 1 reexecucao automatica atingido. "
                    "Cotas permanecem como FALHA (reexecucao manual necessaria)."
                )
            else:
                cells = marcar_reexecutar_planilha(id_fila_adm, logger)
                resultado["reexecucao_planilha_marcada"] = cells > 0
                resultado["etapas"]["reexecucao_planilha"] = "OK" if cells > 0 else "ERRO"
                logger.info(
                    f"[POS] ETAPA 5/6 — Cotas FALHA marcadas como REEXECUTAR | cells={cells}"
                )
        else:
            resultado["etapas"]["reexecucao_planilha"] = "NAO_NECESSARIO"
            logger.info("[POS] ETAPA 5/6 — Nenhuma cota FALHA; reexecucao nao necessaria")
    except Exception as e:
        logger.error(f"[POS] ETAPA 5/6 — FALHA ao marcar REEXECUTAR | erro={e}")
        resultado["etapas"]["reexecucao_planilha"] = "ERRO"

    # ── 6. WhatsApp: avisa ADM para checar o email (so quando 0 falhas) ──────
    try:
        if not tem_cotas_falha(id_fila_adm):
            logger.info("[POS] ETAPA 6/6 — Enviando aviso WhatsApp ao ADM (0 falhas)...")
            wpp_ok = enviar_whatsapp_conclusao(id_fila_adm, modalidade, logger)
            resultado["whatsapp_enviado"] = wpp_ok
            resultado["etapas"]["whatsapp"] = "OK" if wpp_ok else "ERRO"
            if wpp_ok:
                logger.info("[POS] ETAPA 6/6 — WhatsApp enviado com sucesso")
            else:
                logger.warn("[POS] ETAPA 6/6 — WhatsApp nao enviado (ver log acima)")
        else:
            resultado["etapas"]["whatsapp"] = "NAO_ENVIADO_HA_FALHAS"
            logger.info("[POS] ETAPA 6/6 — Ha falhas; WhatsApp nao enviado")
    except Exception as e:
        logger.error(f"[POS] ETAPA 6/6 — FALHA ao enviar WhatsApp | erro={e}")
        resultado["etapas"]["whatsapp"] = "ERRO"

    logger.info(
        f"[POS] Concluido | id_fila_adm={id_fila_adm} "
        f"cells={resultado['cells_atualizadas']} "
        f"link_drive={resultado['link_drive']} "
        f"email_enviado={resultado['email_enviado']} "
        f"relatorio_interno={resultado['relatorio_interno_enviado']} "
        f"reexecucao_planilha={resultado['reexecucao_planilha_marcada']} "
        f"whatsapp={resultado['whatsapp_enviado']}"
    )

    return resultado