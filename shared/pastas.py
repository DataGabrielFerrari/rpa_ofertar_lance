import os
import re
import unicodedata

from config.modalidades import obter_config_modalidade


def _limpar_nome(nome: str) -> str:
    """
    Remove caracteres inválidos para nome de pasta no Windows.
    Substitui por '-'.
    """
    nome = str(nome or "").strip()
    nome = re.sub(r'[<>:"/\\|?*\n\r\t]', "-", nome)
    nome = re.sub(r"\s+", " ", nome)
    return nome.strip()


def criar_estrutura_lote(
    base_dir: str,
    nome_adm: str,
    id_adm: int,
    id_fila_adm: int,
    modalidade: str,
) -> dict:
    """
    Cria a estrutura:

    base_dir/
        lotes_lance/
            log/
                NOMEADM_ID/
                    log_ID_FILA.txt   <- log centralizado (fora do lote)
            motors|imovel/
                NOMEADM_ID/
                    fila_ID/
                        evidencias/
                            OFERTADOS/
                            JA_OFERTADOS/
                            FALHA/

    Retorna um dicionário com os caminhos.
    """
    config = obter_config_modalidade(modalidade)

    nome_limpo = _limpar_nome(nome_adm)

    pasta_raiz = os.path.join(
        base_dir,
        "lotes_lance",
        config.subpasta_lotes,
        f"{nome_limpo}_{id_adm}",
        f"fila_{id_fila_adm}",
    )

    pasta_evidencias = os.path.join(pasta_raiz, "evidencias")
    pasta_ofertados = os.path.join(pasta_evidencias, "OFERTADOS")
    pasta_ja_ofertados = os.path.join(pasta_evidencias, "JA_OFERTADOS")
    pasta_falha = os.path.join(pasta_evidencias, "FALHA")

    os.makedirs(pasta_ofertados, exist_ok=True)
    os.makedirs(pasta_ja_ofertados, exist_ok=True)
    os.makedirs(pasta_falha, exist_ok=True)

    # Log centralizado FORA da pasta do lote (igual ao rpa_gerar_boleto):
    #   base_dir/lotes_lance/log/{NOMEADM_ID}/log_{id_fila_adm}.txt
    pasta_log = os.path.join(
        base_dir, "lotes_lance", "log", f"{nome_limpo}_{id_adm}"
    )
    os.makedirs(pasta_log, exist_ok=True)
    caminho_log = os.path.join(pasta_log, f"log_{id_fila_adm}.txt")

    return {
        "raiz": pasta_raiz,
        "evidencias": pasta_evidencias,
        "ofertados": pasta_ofertados,
        "ja_ofertados": pasta_ja_ofertados,
        "falha": pasta_falha,
        "log": caminho_log,
    }


# ---------------------------------------------------------------------------
# Helpers para subpastas de FALHA
# ---------------------------------------------------------------------------

def _nome_seguro(nome: str, max_len: int = 60) -> str:
    """Remove acentos e caracteres invalidos para uso em nome de pasta."""
    t = unicodedata.normalize("NFKD", str(nome or "").strip())
    t = t.encode("ascii", "ignore").decode("ascii")
    t = re.sub(r'[<>:"/\\|?*\n\r\t]', "-", t)
    t = re.sub(r"\s+", "_", t).strip("_")
    return t[:max_len] if len(t) > max_len else t


def pasta_falha_cota(pasta_falha: str, nome_cliente: str, grupo: str, cota: str) -> str:
    """
    Subpasta de FALHA por cota, igual ao rpa_gerar_boleto:

        evidencias/FALHA/{NOME_CLIENTE}_{GRUPO}_{COTA}/

    Ex: evidencias/FALHA/HENRIQUE SOBRINHO DE SOUZA_000830_3161/
    """
    g = re.sub(r"\D", "", str(grupo or "")).zfill(6)
    c = re.sub(r"\D", "", str(cota or "")).zfill(4)
    nome = _nome_seguro(nome_cliente) or "SEM_CLIENTE"
    pasta = os.path.join(pasta_falha, f"{nome}_{g}_{c}")
    # NAO cria a pasta aqui — _screenshot ja chama os.makedirs antes de salvar.
    # Assim a subpasta de FALHA so aparece se houver evidencia real de falha.
    return pasta


def pasta_nao_baixado_cota(pasta_evidencias: str, nome_cliente: str, grupo: str, cota: str) -> str:
    """
    Subpasta de NAO_BAIXADOS por cota — evidencias de NAO_OFERTADO por REGRA
    DE NEGOCIO (cota indisponivel, tipo de lance inexistente, valor a pagar,
    TA negativo), separadas das falhas tecnicas (que ficam em FALHA):

        evidencias/NAO_BAIXADOS/{NOME_CLIENTE}_{GRUPO}_{COTA}/
    """
    g = re.sub(r"\D", "", str(grupo or "")).zfill(6)
    c = re.sub(r"\D", "", str(cota or "")).zfill(4)
    nome = _nome_seguro(nome_cliente) or "SEM_CLIENTE"
    pasta = os.path.join(pasta_evidencias, "NAO_BAIXADOS", f"{nome}_{g}_{c}")
    # NAO cria a pasta aqui — _screenshot ja chama os.makedirs antes de salvar.
    return pasta


def pasta_erro_login_processamento(pasta_evidencias: str, grupo: str, cota: str) -> str:
    """
    Pasta para erros de login detectados DURANTE o processamento de uma cota
    (ex: sessao AVAPRO expirou, pagina redirecionou para /login).

    Fica no mesmo nivel de FALHA, identificando exatamente a cota afetada:
        evidencias/ERRO_LOGIN_processamento_grupo{g}_cota{c}/

    Assim da para rastrear: em qual cota o login caiu.
    """
    g = re.sub(r"\D", "", str(grupo or "")).zfill(6)
    c = re.sub(r"\D", "", str(cota or "")).zfill(4)
    nome = f"ERRO_LOGIN_processamento_grupo{g}_cota{c}"
    pasta = os.path.join(pasta_evidencias, nome)
    os.makedirs(pasta, exist_ok=True)
    return pasta