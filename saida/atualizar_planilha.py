import re
from typing import Dict, List, Optional, Tuple

from credenciais.google_auth import criar_servico_sheets
from db.db import fetchone, fetchall
from entrada.utils.cabecalho import encontrar_cabecalho
from entrada.utils.sheets import (
    extrair_id_planilha,
    ler_range,
    coluna_para_letra,
    atualizar_multiplas_celulas,
)


def _so_digitos(s: str) -> str:
    return re.sub(r"\D+", "", str(s or ""))


def _key_num(s: str) -> Optional[int]:
    d = _so_digitos(s)
    if not d:
        return None
    try:
        return int(d)
    except Exception:
        return None


def _formatar_status_planilha(status: str) -> str:
    status = (status or "").strip().upper()
    mapa = {
        "OFERTADO":    "OFERTADO",
        "NAO_OFERTADO": "NÃO OFERTADO",
        "FALHA":       "FALHA",
        "PROCESSANDO": "PROCESSANDO",
        "PENDENTE":    "PENDENTE",
    }
    return mapa.get(status, status)


def _obter_link_planilha_por_lote(id_fila_adm: int) -> str:
    sql = """
        SELECT a.link_planilha
        FROM tbl_fila_adm fa
        INNER JOIN tbl_adm a ON a.id_adm = fa.id_adm
        WHERE fa.id_fila_adm = %s
    """
    row = fetchone(sql, (id_fila_adm,))
    if not row:
        raise ValueError(f"Lote não encontrado: id_fila_adm={id_fila_adm}")
    link_planilha = row[0]
    if not link_planilha:
        raise ValueError(f"link_planilha vazio para id_fila_adm={id_fila_adm}")
    return link_planilha


def _obter_itens_lote(id_fila_adm: int) -> List[Dict[str, str]]:
    sql = """
        SELECT nome_aba, grupo, cota, status, observacao
        FROM tbl_fila_cotas
        WHERE id_fila_adm = %s
        ORDER BY id_cota
    """
    rows = fetchall(sql, (id_fila_adm,))
    return [
        {
            "nome_aba":  str(row[0] or "").strip(),
            "grupo":     str(row[1] or "").strip(),
            "cota":      str(row[2] or "").strip(),
            "status":    str(row[3] or "").strip(),
            "observacao": str(row[4] or "").strip(),
        }
        for row in rows
    ]


def _montar_mapa_linhas(
    valores: List[List[str]],
    idx_cabecalho: int,
    idx_grupo: int,
    idx_cota: int,
) -> Tuple[Dict[Tuple[int, int], int], Dict[Tuple[str, str], int]]:
    mapa_num: Dict[Tuple[int, int], int] = {}
    mapa_str: Dict[Tuple[str, str], int] = {}

    for row_num, linha in enumerate(valores[idx_cabecalho + 1:], start=idx_cabecalho + 2):
        grupo_raw = linha[idx_grupo] if idx_grupo < len(linha) else ""
        cota_raw  = linha[idx_cota]  if idx_cota  < len(linha) else ""

        grupo = str(grupo_raw or "").strip()
        cota  = str(cota_raw  or "").strip()

        if not grupo or not cota:
            continue

        mapa_str[(grupo, cota)] = row_num

        grupo_num = _key_num(grupo)
        cota_num  = _key_num(cota)

        if grupo_num is not None and cota_num is not None:
            mapa_num[(grupo_num, cota_num)] = row_num

    return mapa_num, mapa_str


def atualizar_planilha_lote(id_fila_adm: int, logger) -> int:
    itens = _obter_itens_lote(id_fila_adm)
    if not itens:
        logger.warn(f"[PLANILHA] Nenhum item para atualizar | id_fila_adm={id_fila_adm}")
        return 0

    link_planilha  = _obter_link_planilha_por_lote(id_fila_adm)
    spreadsheet_id = extrair_id_planilha(link_planilha)
    service        = criar_servico_sheets()

    por_aba: Dict[str, List[Dict[str, str]]] = {}
    for item in itens:
        aba = item["nome_aba"]
        if aba:
            por_aba.setdefault(aba, []).append(item)

    atualizacoes = []

    for aba, itens_aba in por_aba.items():
        logger.info(f"[PLANILHA] Atualizando aba={aba}")

        try:
            valores = ler_range(service, spreadsheet_id, f"{aba}!A:Z")
        except Exception as e:
            logger.error(f"[PLANILHA] Erro ao ler aba={aba}: {e}")
            continue

        if not valores:
            logger.warn(f"[PLANILHA] Aba sem dados: {aba}")
            continue

        try:
            idx_cabecalho, idx = encontrar_cabecalho(valores, max_linhas_busca=20)
        except Exception as e:
            logger.error(f"[PLANILHA] Cabeçalho inválido na aba={aba}: {e}")
            continue

        # encontrar_cabecalho retorna chaves em MAIÚSCULO conforme cabecalho.py
        idx_grupo  = idx.get("GRUPO")
        idx_cota   = idx.get("COTA")
        idx_status = idx.get("LANCE")
        idx_obs    = idx.get("OBSERVAÇÃO LANCE")

        if idx_grupo is None or idx_cota is None or idx_status is None:
            logger.error(
                f"[PLANILHA] Colunas obrigatórias não encontradas na aba={aba} "
                f"| idx={idx}"
            )
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
            grupo = item["grupo"].strip()
            cota  = item["cota"].strip()

            grupo_num = _key_num(grupo)
            cota_num  = _key_num(cota)

            row_num = None

            if grupo_num is not None and cota_num is not None:
                row_num = mapa_num.get((grupo_num, cota_num))

            if not row_num:
                row_num = mapa_str.get((grupo, cota))

            if not row_num:
                logger.warn(
                    f"[PLANILHA] Linha não encontrada | aba={aba} grupo={grupo} cota={cota}"
                )
                continue

            atualizacoes.append({
                "aba":    aba,
                "coluna": col_status,
                "linha":  row_num,
                "valor":  _formatar_status_planilha(item["status"]),
            })

            if col_obs:
                atualizacoes.append({
                    "aba":    aba,
                    "coluna": col_obs,
                    "linha":  row_num,
                    "valor":  item["observacao"],
                })

    if not atualizacoes:
        logger.warn(f"[PLANILHA] Nenhuma célula para atualizar | id_fila_adm={id_fila_adm}")
        return 0

    atualizar_multiplas_celulas(
        service=service,
        spreadsheet_id=spreadsheet_id,
        atualizacoes=atualizacoes,
    )

    logger.info(
        f"[PLANILHA] Concluída | id_fila_adm={id_fila_adm} cells={len(atualizacoes)}"
    )
    return len(atualizacoes)