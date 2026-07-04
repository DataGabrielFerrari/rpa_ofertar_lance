"""
Prepara a reexecucao automatica das cotas que falharam.

Responsabilidades:
  1. Planilha: sobrescreve status FALHA -> "REEXECUTAR" para cotas com
     status FALHA no banco, com observacao indicando que e reexecucao.
  2. Banco: ativa reexecucao_imovel ou reexecucao_motors = TRUE no
     tbl_adm correspondente ao lote.

IMPORTANTE: ativar_flag_reexecucao deve ser chamada APOS fechar_lote_adm,
pois fechar_lote_adm reseta reexecucao=FALSE. A marcacao na planilha pode
ser feita antes (dentro do pos_processamento), pois nao ha conflito.

A flag reexecucao=TRUE garante que na proxima execucao do entrada o ADM
seja selecionado com prioridade maxima (ORDER BY reexecucao DESC no banco),
e o leitor so processara cotas marcadas como "REEXECUTAR" na planilha.

LIMITE: no maximo 1 reexecucao automatica por lote. Se o lote que falhou
ja era uma reexecucao (ver lote_era_reexecucao), a marcacao REEXECUTAR e
a flag NAO sao aplicadas de novo — as cotas ficam como FALHA na planilha
e a reexecucao passa a ser manual. Isso evita loop infinito de
reprocessamento quando a falha e persistente.
"""

from typing import List, Dict
from db.db import fetchall, fetchone, execute
from credenciais.google_auth import criar_servico_sheets
from saida.atualizar_planilha import (
    _obter_link_planilha_por_lote,
    _montar_mapa_linhas,
)
from entrada.utils.cabecalho import encontrar_cabecalho
from entrada.utils.sheets import (
    extrair_id_planilha,
    ler_range,
    coluna_para_letra,
    atualizar_multiplas_celulas,
)


OBS_REEXECUCAO = "REEXECUTAR - Falha tecnica na execucao anterior"


# =========================================================
# QUERIES
# =========================================================

def _cotas_falha(id_fila_adm: int) -> List[Dict]:
    """Retorna todas as cotas com status FALHA do lote."""
    sql = """
        SELECT nome_aba, grupo, cota, tentativas, observacao
        FROM tbl_fila_cotas
        WHERE id_fila_adm = %s
          AND status = 'FALHA'
        ORDER BY id_cota
    """
    rows = fetchall(sql, (id_fila_adm,))
    return [
        {
            "nome_aba":   str(r[0] or "").strip(),
            "grupo":      str(r[1] or "").strip(),
            "cota":       str(r[2] or "").strip(),
            "tentativas": int(r[3] or 0),
            "observacao": str(r[4] or "").strip(),
        }
        for r in rows
    ] if rows else []


def _obter_id_adm(id_fila_adm: int):
    row = fetchone(
        "SELECT id_adm FROM tbl_fila_adm WHERE id_fila_adm = %s",
        (id_fila_adm,),
    )
    return int(row[0]) if row else None


# =========================================================
# PLANILHA: marca FALHA -> REEXECUTAR
# =========================================================

def marcar_reexecutar_planilha(id_fila_adm: int, logger) -> int:
    """
    Sobrescreve na planilha o status das cotas FALHA para 'REEXECUTAR'.
    Retorna o numero de celulas atualizadas (0 se nenhuma FALHA).
    Nunca levanta excecao — falha silenciosa com log.
    """
    try:
        cotas = _cotas_falha(id_fila_adm)
        if not cotas:
            logger.info("[REEXEC] Nenhuma cota FALHA — planilha nao alterada")
            return 0

        link_planilha  = _obter_link_planilha_por_lote(id_fila_adm)
        spreadsheet_id = extrair_id_planilha(link_planilha)
        service        = criar_servico_sheets()

        # Agrupa por aba
        por_aba: Dict[str, List[Dict]] = {}
        for c in cotas:
            if c["nome_aba"]:
                por_aba.setdefault(c["nome_aba"], []).append(c)

        atualizacoes = []

        for aba, itens_aba in por_aba.items():
            try:
                valores = ler_range(service, spreadsheet_id, f"{aba}!A:Z")
            except Exception as e:
                logger.error(f"[REEXEC] Erro ao ler aba={aba}: {e}")
                continue

            if not valores:
                continue

            try:
                idx_cabecalho, idx = encontrar_cabecalho(valores, max_linhas_busca=20)
            except Exception as e:
                logger.error(f"[REEXEC] Cabecalho invalido na aba={aba}: {e}")
                continue

            idx_grupo  = idx.get("GRUPO")
            idx_cota   = idx.get("COTA")
            idx_status = idx.get("LANCE")
            idx_obs    = idx.get("OBSERVAÇÃO LANCE")

            if idx_grupo is None or idx_cota is None or idx_status is None:
                logger.error(f"[REEXEC] Colunas obrigatorias ausentes na aba={aba}")
                continue

            mapa_num, mapa_str = _montar_mapa_linhas(
                valores=valores,
                idx_cabecalho=idx_cabecalho,
                idx_grupo=idx_grupo,
                idx_cota=idx_cota,
            )

            col_status = coluna_para_letra(idx_status)
            col_obs    = coluna_para_letra(idx_obs) if idx_obs is not None else None

            for item in itens_aba:
                grupo     = item["grupo"].strip()
                cota      = item["cota"].strip()

                from saida.atualizar_planilha import _key_num
                grupo_num = _key_num(grupo)
                cota_num  = _key_num(cota)

                row_num = None
                if grupo_num is not None and cota_num is not None:
                    row_num = mapa_num.get((grupo_num, cota_num))
                if not row_num:
                    row_num = mapa_str.get((grupo, cota))

                if not row_num:
                    logger.warn(
                        f"[REEXEC] Linha nao encontrada | aba={aba} grupo={grupo} cota={cota}"
                    )
                    continue

                atualizacoes.append({
                    "aba":    aba,
                    "coluna": col_status,
                    "linha":  row_num,
                    "valor":  "REEXECUTAR",
                })

                if col_obs:
                    obs = (
                        f"REEXECUTAR | tentativas={item['tentativas']} | {item['observacao']}"
                        if item["observacao"]
                        else f"REEXECUTAR | tentativas={item['tentativas']}"
                    )
                    atualizacoes.append({
                        "aba":    aba,
                        "coluna": col_obs,
                        "linha":  row_num,
                        "valor":  obs,
                    })

        if not atualizacoes:
            logger.warn("[REEXEC] Nenhuma celula mapeada para atualizar na planilha")
            return 0

        atualizar_multiplas_celulas(
            service=service,
            spreadsheet_id=spreadsheet_id,
            atualizacoes=atualizacoes,
        )

        logger.info(
            f"[REEXEC] Planilha atualizada | {len(cotas)} cota(s) FALHA -> REEXECUTAR "
            f"| cells={len(atualizacoes)}"
        )
        return len(atualizacoes)

    except Exception as e:
        try:
            logger.error(f"[REEXEC] Falha ao marcar REEXECUTAR na planilha: {e}")
        except Exception:
            pass
        return 0


# =========================================================
# BANCO: ativa flag reexecucao no tbl_adm
# =========================================================

def ativar_flag_reexecucao(id_fila_adm: int, modalidade: str, logger) -> bool:
    """
    Ativa reexecucao_imovel ou reexecucao_motors = TRUE no tbl_adm.

    DEVE ser chamada APOS fechar_lote_adm, pois essa funcao reseta
    a flag para FALSE.

    Com a flag TRUE, reservar_proximo_adm_e_criar_fila seleciona este
    ADM com prioridade maxima (ORDER BY reexecucao DESC) na proxima
    execucao do entrada.
    """
    try:
        id_adm = _obter_id_adm(id_fila_adm)
        if not id_adm:
            logger.error(f"[REEXEC] id_adm nao encontrado para id_fila_adm={id_fila_adm}")
            return False

        modalidade = (modalidade or "").strip().upper()
        if modalidade == "MOTORS":
            coluna = "reexecucao_motors"
        elif modalidade == "IMOVEL":
            coluna = "reexecucao_imovel"
        else:
            logger.error(f"[REEXEC] Modalidade invalida: {modalidade}")
            return False

        execute(
            f"UPDATE tbl_adm SET {coluna} = TRUE WHERE id_adm = %s",
            (id_adm,),
        )

        logger.info(
            f"[REEXEC] Flag ativada | id_adm={id_adm} {coluna}=TRUE "
            f"(proxima entrada priorizara este ADM)"
        )
        return True

    except Exception as e:
        try:
            logger.error(f"[REEXEC] Falha ao ativar flag reexecucao: {e}")
        except Exception:
            pass
        return False


# =========================================================
# VERIFICACAO
# =========================================================

def tem_cotas_falha(id_fila_adm: int) -> bool:
    """Retorna True se o lote tem ao menos uma cota com status FALHA."""
    row = fetchone(
        "SELECT COUNT(*) FROM tbl_fila_cotas WHERE id_fila_adm = %s AND status = 'FALHA'",
        (id_fila_adm,),
    )
    return bool(row and int(row[0]) > 0)


def lote_era_reexecucao(id_fila_adm: int, modalidade: str) -> bool:
    """
    Retorna True se o lote atual JA E uma reexecucao automatica.

    Le a flag reexecucao_motors/reexecucao_imovel do tbl_adm, que
    permanece TRUE durante todo o lote de reexecucao (so e resetada
    por fechar_lote_adm). Portanto, DEVE ser chamada ANTES de
    fechar_lote_adm.

    Usada para limitar a reexecucao automatica a 1 tentativa:
    se o lote que falhou ja era reexecucao, nao marca REEXECUTAR
    de novo — evita loop infinito de reprocessamento.
    """
    modalidade = (modalidade or "").strip().upper()
    coluna = {
        "MOTORS": "reexecucao_motors",
        "IMOVEL": "reexecucao_imovel",
    }.get(modalidade)
    if not coluna:
        return False

    row = fetchone(
        f"""
        SELECT a.{coluna}
        FROM tbl_fila_adm fa
        JOIN tbl_adm a ON a.id_adm = fa.id_adm
        WHERE fa.id_fila_adm = %s
        """,
        (id_fila_adm,),
    )
    return bool(row and row[0])
