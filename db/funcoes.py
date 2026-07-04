import json

from db.db import fetchone, fetchall, execute


# =========================================================
# FUNÇÕES - COTA
# =========================================================

def buscar_proxima_cota_pendente(id_fila_adm):
    sql = "SELECT * FROM buscar_proxima_cota_pendente(%s);"
    return fetchone(sql, (id_fila_adm,))


def marcar_cota_processando(id_cota):
    sql = "SELECT marcar_cota_processando(%s);"
    execute(sql, (id_cota,))


def finalizar_cota_resultado(
    id_cota,
    status,
    observacao=None,
    caminho_comprovante=None,
    caminho_evidencia=None
):
    sql = "SELECT finalizar_cota_resultado(%s, %s, %s, %s, %s);"
    execute(
        sql,
        (
            id_cota,
            status,
            observacao,
            caminho_comprovante,
            caminho_evidencia,
        ),
    )


def finalizar_cota_falha(id_cota, observacao, caminho_evidencia):
    sql = "SELECT finalizar_cota_falha(%s, %s, %s);"
    execute(sql, (id_cota, observacao, caminho_evidencia))


# =========================================================
# FUNÇÕES - LOTE / ADM
# =========================================================

def reservar_lote_interrompido(modalidade, maquina):
    """
    Busca lote PENDENTE/FALHA do mês atual
    e já faz lock + update para PROCESSANDO
    garantindo que só 1 worker pegue o lote.
    """
    sql = "SELECT * FROM reservar_lote_interrompido(%s, %s);"
    return fetchone(sql, (modalidade, maquina))


def marcar_lotes_parados_como_falha(minutos=10):
    """
    Marca como FALHA todos os lotes em PROCESSANDO parados ha mais
    de X minutos. Retorna lista (pode ter varios lotes na mesma execucao).
    """
    sql = "SELECT * FROM marcar_lotes_parados_como_falha(%s);"
    return fetchall(sql, (minutos,)) or []


def reservar_proximo_adm_e_criar_fila(modalidade, maquina):
    sql = "SELECT * FROM reservar_proximo_adm_e_criar_fila(%s, %s);"
    return fetchone(sql, (modalidade, maquina))


def obter_credenciais_adm_por_fila(id_fila_adm):
    sql = "SELECT * FROM obter_credenciais_adm_por_fila(%s);"
    return fetchone(sql, (id_fila_adm,))


def atualizar_caminhos_fila_adm(id_fila_adm, caminho_base=None, caminho_log=None):
    sql = "SELECT atualizar_caminhos_fila_adm(%s, %s, %s);"
    execute(sql, (id_fila_adm, caminho_base, caminho_log))


def finalizar_fila_adm(id_fila_adm, status, observacao=None):
    sql = "SELECT finalizar_fila_adm(%s, %s, %s);"
    execute(sql, (id_fila_adm, status, observacao))


def fechar_lote_adm(id_fila_adm, status, observacao=None):
    """
    Fecha o lote chamando a function fechar_lote_adm do banco.

    A function recalcula contadores (cotas_ofertadas, cotas_nao_ofertadas,
    cotas_erro), atualiza status do lote, marca hora_fim, e quando status =
    SUCESSO ja atualiza ultima_execucao_motors/imovel da tbl_adm.

    Retorna a row com:
      id_fila_adm, id_adm, status_final, total_cotas,
      cotas_ofertadas, cotas_nao_ofertadas, cotas_erro, cotas_pendentes
    """
    sql = "SELECT * FROM fechar_lote_adm(%s, %s, %s);"
    return fetchone(sql, (id_fila_adm, status, observacao))


def atualizar_link_drive_fila_adm(id_fila_adm, link_drive):
    sql = "SELECT atualizar_link_drive_fila_adm(%s, %s);"
    execute(sql, (id_fila_adm, link_drive))


def atualizar_ultima_execucao_adm(id_adm, modalidade, data_execucao=None):
    if data_execucao is None:
        sql = "SELECT atualizar_ultima_execucao_adm(%s, %s);"
        execute(sql, (id_adm, modalidade))
    else:
        sql = "SELECT atualizar_ultima_execucao_adm(%s, %s, %s);"
        execute(sql, (id_adm, modalidade, data_execucao))


def atualizar_total_cotas_fila_adm(id_fila_adm, total_cotas):
    sql = "SELECT atualizar_total_cotas_fila_adm(%s, %s);"
    execute(sql, (id_fila_adm, total_cotas))


def obter_dados_adm_por_fila(id_fila_adm):
    sql = "SELECT * FROM obter_dados_adm_por_fila(%s);"
    return fetchone(sql, (id_fila_adm,))


def inserir_fila_cotas_em_lote(id_fila_adm, cotas):
    if not cotas:
        return 0

    sql = "SELECT inserir_fila_cotas_em_lote(%s, %s::jsonb);"
    row = fetchone(sql, (id_fila_adm, json.dumps(cotas, ensure_ascii=False)))
    return row[0] if row else 0


# =========================================================
# FUNÇÕES - PARÂMETROS
# =========================================================

def obter_parametro(nome: str) -> str | None:
    sql = """
        SELECT valor
        FROM tbl_parametros
        WHERE nome = %s
    """
    row = fetchone(sql, (nome,))
    return row[0] if row else None


def obter_url() -> str | None:
    return obter_parametro("url")


def obter_timeout() -> str | None:
    return obter_parametro("timeout_padrao")


def obter_pasta_base() -> str | None:
    return obter_parametro("pasta_base")
