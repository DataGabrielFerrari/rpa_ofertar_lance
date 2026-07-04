from typing import List
import unicodedata
import re

from credenciais.google_auth import criar_servico_sheets
from db.funcoes import (
    inserir_fila_cotas_em_lote,
    obter_dados_adm_por_fila,
    atualizar_total_cotas_fila_adm,
)
from entrada.utils.cabecalho import encontrar_cabecalho
from entrada.utils.sheets import extrair_id_planilha, ler_range
from entrada.utils.lance_rules import (
    deve_bloquear,
    esta_nao_ofertado,
    esta_reexecucao,
)
from shared.log import Logger
from shared.notificador import notificar_falha


class LinhasPuladasPlanilha(Exception):
    """Alerta: linhas da planilha puladas (campos faltando ou duplicadas)."""
    pass


def normalizar_cabecalho(texto: str) -> str:
    texto = str(texto or "").strip().upper()
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("utf-8")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def split_abas(nome_aba: str) -> List[str]:
    abas = [a.strip() for a in (nome_aba or "").split(",") if a.strip()]
    if not abas:
        raise ValueError("Nenhuma aba válida foi informada.")
    return abas


def numero_para_coluna_excel(numero_coluna: int) -> str:
    resultado = ""
    numero_coluna += 1

    while numero_coluna > 0:
        numero_coluna, resto = divmod(numero_coluna - 1, 26)
        resultado = chr(65 + resto) + resultado

    return resultado


def montar_range_a1(aba: str, numero_linha_sheet: int, indice_coluna: int) -> str:
    coluna = numero_para_coluna_excel(indice_coluna)
    return f"{aba}!{coluna}{numero_linha_sheet}"


def atualizar_status_lance_em_lote(service, spreadsheet_id: str, atualizacoes: list[dict]):
    if not atualizacoes:
        return

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "valueInputOption": "RAW",
            "data": atualizacoes,
        },
    ).execute()


def ler_planilhas(id_fila_adm: int, modalidade_execucao: str, logger: Logger) -> int:
    logger.info(
        f"[PLANILHA] Iniciando leitura | id_fila_adm={id_fila_adm} modalidade={modalidade_execucao}"
    )

    dados_adm = obter_dados_adm_por_fila(id_fila_adm)
    if not dados_adm:
        raise ValueError(f"Lote não encontrado: id_fila_adm={id_fila_adm}")

    link_planilha = dados_adm[3]
    nome_aba_raw = dados_adm[4] or ""
    reexecucao = bool(dados_adm[5])

    if not link_planilha:
        raise ValueError(f"link_planilha vazio para id_fila_adm={id_fila_adm}")

    if not nome_aba_raw:
        raise ValueError(
            f"Nenhuma aba configurada para modalidade={modalidade_execucao} no id_fila_adm={id_fila_adm}"
        )

    abas = split_abas(nome_aba_raw)
    spreadsheet_id = extrair_id_planilha(link_planilha)
    service = criar_servico_sheets()

    total_bloqueadas = 0
    total_ignoradas = 0
    total_invalidas = 0
    total_atualizadas_nao_ofertado = 0

    cotas_para_inserir = []
    atualizacoes_status = []
    chaves_vistas = set()

    # Detalhe das linhas PULADAS (para log linha a linha + email de alerta):
    #   invalidas_detalhe  -> campos essenciais em branco (nome/grupo/cota)
    #   duplicadas_detalhe -> mesma aba+grupo+cota repetida na planilha
    invalidas_detalhe: list[dict] = []
    duplicadas_detalhe: list[dict] = []

    for aba in abas:
        logger.info(f"[PLANILHA] Lendo aba={aba}")

        try:
            valores = ler_range(service, spreadsheet_id, f"{aba}!A:Z")
        except Exception as e:
            logger.error(f"[PLANILHA] Erro ao ler aba={aba}: {e}")
            raise RuntimeError(
                f"Falha ao ler aba='{aba}' da planilha (id_fila_adm={id_fila_adm}): {e}"
            ) from e

        if not valores:
            logger.error(f"[PLANILHA] Aba sem dados: {aba}")
            raise RuntimeError(
                f"Aba '{aba}' sem dados (id_fila_adm={id_fila_adm})"
            )

        try:
            idx_cabecalho, idx = encontrar_cabecalho(valores)
            idx = {normalizar_cabecalho(k): v for k, v in idx.items()}
            logger.info(f"[PLANILHA] Cabecalhos encontrados na aba={aba}: {list(idx.keys())}")
        except Exception as e:
            logger.error(f"[PLANILHA] Cabecalho invalido na aba={aba}: {e}")
            raise RuntimeError(
                f"Cabecalho invalido na aba='{aba}' (id_fila_adm={id_fila_adm}): {e}"
            ) from e

        idx_status_lance = idx.get("LANCE")
        if idx_status_lance is None:
            logger.error(f"[PLANILHA] Coluna LANCE nao encontrada na aba={aba}")
            logger.error(f"[PLANILHA] Cabecalhos lidos: {list(idx.keys())}")
            raise RuntimeError(
                f"Coluna LANCE nao encontrada na aba='{aba}' (id_fila_adm={id_fila_adm}). "
                f"Cabecalhos: {list(idx.keys())}"
            )

        linhas_dados = valores[idx_cabecalho + 1:]

        bloqueadas_aba = 0
        ignoradas_aba = 0
        invalidas_aba = 0
        atualizadas_aba = 0
        preparadas_aba = 0

        for i, linha in enumerate(linhas_dados, start=idx_cabecalho + 2):

            def cell(chave: str) -> str:
                pos = idx.get(normalizar_cabecalho(chave))
                if pos is None:
                    return ""
                return str(linha[pos]).strip() if pos < len(linha) else ""

            nome_cliente = cell("NOME DO CLIENTE")
            grupo = cell("GRUPO")
            cota = cell("COTA")
            status_lance = cell("LANCE")
            consultor = cell("CONSULTOR")

            # Linha completamente em branco: pula sem alarde (linhas vazias
            # no meio da planilha sao normais e nao devem gerar alerta).
            if not nome_cliente and not grupo and not cota:
                continue

            if not nome_cliente or not grupo or not cota:
                invalidas_aba += 1
                total_invalidas += 1
                campos_faltando = []
                if not nome_cliente:
                    campos_faltando.append("NOME DO CLIENTE")
                if not grupo:
                    campos_faltando.append("GRUPO")
                if not cota:
                    campos_faltando.append("COTA")
                invalidas_detalhe.append({
                    "aba": aba,
                    "linha": i,
                    "campos_faltando": campos_faltando,
                    "nome_cliente": nome_cliente or "(vazio)",
                    "grupo": grupo or "(vazio)",
                    "cota": cota or "(vazio)",
                })
                logger.warn(
                    f"[PLANILHA] LINHA PULADA (campo essencial em branco) | "
                    f"aba={aba} linha={i} faltando=[{', '.join(campos_faltando)}] "
                    f"cliente='{nome_cliente or '(vazio)'}' grupo='{grupo or '(vazio)'}' "
                    f"cota='{cota or '(vazio)'}'"
                )
                continue

            if deve_bloquear(status_lance):
                bloqueadas_aba += 1
                total_bloqueadas += 1
                logger.info(
                    f"[PLANILHA] Linha bloqueada por status | aba={aba} linha={i} "
                    f"cliente='{nome_cliente}' grupo={grupo} cota={cota} "
                    f"status_lance='{status_lance}'"
                )
                continue

            if reexecucao:
                if not esta_reexecucao(status_lance):
                    ignoradas_aba += 1
                    total_ignoradas += 1
                    logger.info(
                        f"[PLANILHA] Linha ignorada (modo reexecucao, status != REEXECUTAR) | "
                        f"aba={aba} linha={i} cliente='{nome_cliente}' "
                        f"grupo={grupo} cota={cota} status_lance='{status_lance}'"
                    )
                    continue
            else:
                # Modo normal: TUDO que nao for bloqueado/invalido vai para a
                # fila de processamento. Se o status na planilha ainda nao e'
                # "NÃO OFERTADO", agenda atualizacao da celula como auditoria
                # do estado inicial — mas a cota PROSSEGUE para insercao no
                # banco normalmente (sem continue).
                if not esta_nao_ofertado(status_lance):
                    range_a1 = montar_range_a1(
                        aba=aba,
                        numero_linha_sheet=i,
                        indice_coluna=idx_status_lance,
                    )
                    atualizacoes_status.append({
                        "range": range_a1,
                        "values": [["NÃO OFERTADO"]],
                    })
                    atualizadas_aba += 1
                    total_atualizadas_nao_ofertado += 1

            chave = (aba, grupo, cota)
            if chave in chaves_vistas:
                duplicadas_detalhe.append({
                    "aba": aba,
                    "linha": i,
                    "nome_cliente": nome_cliente,
                    "grupo": grupo,
                    "cota": cota,
                })
                logger.warn(
                    f"[PLANILHA] LINHA PULADA (cota duplicada na planilha) | "
                    f"aba={aba} linha={i} cliente='{nome_cliente}' "
                    f"grupo={grupo} cota={cota}"
                )
                continue

            chaves_vistas.add(chave)

            cotas_para_inserir.append({
                "nome_cliente": nome_cliente,
                "nome_consultor": consultor if consultor else None,
                "grupo": grupo,
                "cota": cota,
                "nome_aba": aba,
            })
            preparadas_aba += 1

        logger.info(
            f"[PLANILHA] Aba={aba} preparadas={preparadas_aba} "
            f"bloqueadas={bloqueadas_aba} ignoradas={ignoradas_aba} "
            f"invalidas={invalidas_aba} atualizadas_nao_ofertado={atualizadas_aba}"
        )

    # Insercao unica e atomica no banco apos ler todas as abas
    try:
        total_inseridas = inserir_fila_cotas_em_lote(
            id_fila_adm=id_fila_adm,
            cotas=cotas_para_inserir,
        )
    except Exception as e:
        logger.error(f"[PLANILHA] Falha ao inserir cotas em lote: {e}")
        raise RuntimeError(
            f"Falha ao inserir cotas em lote (id_fila_adm={id_fila_adm}): {e}"
        ) from e

    # Batch update na planilha apos persistir no banco
    try:
        atualizar_status_lance_em_lote(service, spreadsheet_id, atualizacoes_status)
    except Exception as e:
        logger.error(f"[PLANILHA] Falha no batch update da planilha: {e}")
        raise RuntimeError(
            f"Falha ao atualizar status na planilha (id_fila_adm={id_fila_adm}): {e}"
        ) from e

    atualizar_total_cotas_fila_adm(id_fila_adm, total_inseridas)

    logger.info(
        f"[PLANILHA] Finalizada | id_fila_adm={id_fila_adm} "
        f"total_inseridas={total_inseridas} bloqueadas={total_bloqueadas} "
        f"ignoradas={total_ignoradas} invalidas={total_invalidas} "
        f"duplicadas={len(duplicadas_detalhe)} "
        f"atualizadas_nao_ofertado={total_atualizadas_nao_ofertado}"
    )

    # =========================================================
    # E-MAIL DE ALERTA: linhas PULADAS na leitura da planilha
    # (campos essenciais em branco e/ou duplicatas). O lote segue
    # normalmente com as linhas validas — o email e so um alerta
    # para o operador corrigir a planilha rapidamente.
    # =========================================================
    if invalidas_detalhe or duplicadas_detalhe:
        try:
            partes = []

            if invalidas_detalhe:
                linhas_fmt = "\n".join(
                    f"  - aba='{d['aba']}' | LINHA {d['linha']} | "
                    f"cliente='{d['nome_cliente']}' | grupo='{d['grupo']}' | "
                    f"cota='{d['cota']}' | campos em branco: {', '.join(d['campos_faltando'])}"
                    for d in invalidas_detalhe
                )
                partes.append(
                    f"LINHAS PULADAS POR CAMPO ESSENCIAL EM BRANCO "
                    f"({len(invalidas_detalhe)} linha(s)):\n{linhas_fmt}"
                )

            if duplicadas_detalhe:
                linhas_fmt = "\n".join(
                    f"  - aba='{d['aba']}' | LINHA {d['linha']} | "
                    f"cliente='{d['nome_cliente']}' | grupo='{d['grupo']}' | "
                    f"cota='{d['cota']}' (mesma aba+grupo+cota ja lida antes)"
                    for d in duplicadas_detalhe
                )
                partes.append(
                    f"LINHAS PULADAS POR DUPLICIDADE NA PLANILHA "
                    f"({len(duplicadas_detalhe)} linha(s)):\n{linhas_fmt}"
                )

            qtd_total = len(invalidas_detalhe) + len(duplicadas_detalhe)
            mensagem = (
                f"{qtd_total} linha(s) da planilha foram PULADAS na leitura do lote "
                f"id_fila_adm={id_fila_adm} (modalidade={modalidade_execucao}). "
                f"O lote foi processado normalmente com as linhas validas."
            )
            contexto_extra = (
                f"modalidade={modalidade_execucao}\n"
                f"id_fila_adm={id_fila_adm}\n"
                f"total_linhas_puladas={qtd_total}\n\n"
                + "\n\n".join(partes)
                + "\n\nAcao recomendada: corrigir as linhas indicadas na planilha "
                  "do ADM (numero da linha e o da propria planilha Google) antes "
                  "da proxima execucao."
            )
            logger.warn(
                f"[PLANILHA] Enviando email de alerta de linhas puladas | "
                f"total={qtd_total} invalidas={len(invalidas_detalhe)} "
                f"duplicadas={len(duplicadas_detalhe)}"
            )
            notificar_falha(
                etapa="ENTRADA/LINHAS_PULADAS",
                erro=LinhasPuladasPlanilha(mensagem),
                id_fila_adm=id_fila_adm,
                caminho_log=getattr(logger, "caminho_arquivo", None),
                script_path=__file__,
                contexto_extra=contexto_extra,
            )
        except Exception as e_notif:
            logger.error(
                f"[PLANILHA] Falha ao enviar email de linhas puladas: {e_notif}"
            )

    return total_inseridas