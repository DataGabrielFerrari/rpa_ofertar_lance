from db.funcoes import (
    finalizar_fila_adm,
    atualizar_ultima_execucao_adm,
    obter_dados_adm_por_fila,
)


def _extrair_id_adm(dados_adm) -> int:
    if hasattr(dados_adm, "keys"):
        return int(dados_adm["id_adm"])
    return int(dados_adm[1])


def _extrair_modalidade(dados_adm) -> str:
    if hasattr(dados_adm, "keys"):
        return str(dados_adm["modalidade"]).strip().upper()
    return str(dados_adm[7]).strip().upper()


def fechar_lote(
    id_fila_adm: int,
    logger,
    status: str = "SUCESSO",
    observacao: str | None = None,
    atualizar_ultima_execucao: bool = True,
) -> None:
    status = (status or "").strip().upper()

    if status not in ("SUCESSO", "FALHA"):
        raise ValueError("Status inválido para fechar lote. Use SUCESSO ou FALHA.")

    dados_adm = obter_dados_adm_por_fila(id_fila_adm)
    if not dados_adm:
        raise ValueError(f"Lote não encontrado: id_fila_adm={id_fila_adm}")

    id_adm = _extrair_id_adm(dados_adm)
    modalidade = _extrair_modalidade(dados_adm)

    finalizar_fila_adm(
        id_fila_adm=id_fila_adm,
        status=status,
        observacao=observacao,
    )

    if status == "SUCESSO" and atualizar_ultima_execucao:
        atualizar_ultima_execucao_adm(id_adm, modalidade)

    logger.info(
        f"[FECHAMENTO] Lote encerrado | "
        f"id_fila_adm={id_fila_adm} "
        f"status={status} "
        f"id_adm={id_adm} "
        f"modalidade={modalidade}"
    )