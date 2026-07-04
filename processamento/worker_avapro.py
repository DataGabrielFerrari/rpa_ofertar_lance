import os
import re
from datetime import datetime
from typing import Dict, Optional
from time import sleep
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from config.modalidades import obter_config_modalidade
from db.db import fetchone
from db.funcoes import marcar_cota_processando
from shared.pastas import (
    pasta_falha_cota,
    pasta_erro_login_processamento,
    pasta_nao_baixado_cota,
)
from processamento.mapeamento_worker import (
    preencher_grupo_cota,
    clicar_pesquisar,
    obter_card_cota,
    clicar_card_cota,
    card_tem_lance_realizado,
    clicar_tipo_lance,
    clicar_continuar,
    toast_ta_negativo_visivel,
    aguardar_botoes_pos_continuar,
    estado_pos_continuar,
    clicar_ofertar_lance,
    obter_texto_toast,
    toast_sucesso_visivel,
    clicar_botao_por_texto,
    clicar_nova_oferta,
    clicar_alterar_cota_se_visivel,
    garantir_tela_busca,
)
from shared.log import Logger
import unicodedata

# ---------------------------------------------------------------------------
# Helpers gerais
# ---------------------------------------------------------------------------

def _so_digitos(txt: str) -> str:
    return "".join(ch for ch in str(txt or "") if ch.isdigit())


def _nome_arquivo_seguro(txt: str, max_len: int = 80) -> str:
    t = str(txt or "").strip()

    # Remove acentos para evitar erro de encoding no PAD/PowerShell/DB
    t = unicodedata.normalize("NFKD", t)
    t = t.encode("ascii", "ignore").decode("ascii")

    t = re.sub(r'[<>:"/\\|?*\n\r\t]', "-", t)
    t = re.sub(r"\s+", " ", t).strip()

    if len(t) > max_len:
        t = t[:max_len].rstrip()

    return t


def _screenshot(page: Page, pasta: str, prefixo: str, logger: Logger) -> str:
    os.makedirs(pasta, exist_ok=True)
    # inclui milissegundos para evitar colisao quando dois screenshots
    # caem no mesmo segundo
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = os.path.join(pasta, f"{prefixo}_{ts}.png")
    page.screenshot(path=path, full_page=True)
    logger.info(
        f"[EVIDENCIA] Screenshot salvo | caminho_completo={os.path.abspath(path)} "
        f"| pasta={os.path.abspath(pasta)} | arquivo={os.path.basename(path)}"
    )
    return path


def _fmt_brl(valor: Optional[float]) -> str:
    """Formata float como moeda BRL: 1234.56 -> 'R$ 1.234,56'."""
    if valor is None:
        return "N/A"
    s = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _saida(
    status: str,
    observacao: str,
    caminho_comprovante: Optional[str] = None,
    caminho_print_falha: Optional[str] = None,
    valor_pagar: Optional[float] = None,
) -> dict:
    return {
        "status":              status,
        "observacao":          observacao,
        "caminho_comprovante": caminho_comprovante,
        "caminho_print_falha": caminho_print_falha,
        "valor_pagar":         valor_pagar,
    }


# ---------------------------------------------------------------------------
# Le o "Valor a pagar" usando o container pai como ancora
# ---------------------------------------------------------------------------

def _obter_valor_a_pagar(page: Page, logger: Logger, timeout_ms: int = 3000) -> Optional[float]:
    """
    Le o valor do campo 'Valor a pagar' usando o div container como ancora,
    evitando confusao com outros valores zerados na tela.

    Estrutura esperada:
        <div class="flex items-center justify-between">
            <p>Valor a pagar</p>
            <p class="font-bold">R$ 0,00</p>
        </div>
    """
    try:
        logger.info("[DEBUG] _obter_valor_a_pagar — buscando container na pagina")
        container = page.locator(
            "div.flex.items-center.justify-between",
            has=page.locator("p", has_text="Valor a pagar"),
        ).first

        container.wait_for(state="visible", timeout=timeout_ms)

        valor_texto = container.locator("p.font-bold").first.inner_text(timeout=timeout_ms)
        logger.info(f"[DEBUG] Texto bruto lido do 'Valor a pagar': '{valor_texto}'")

        valor_limpo = (
            valor_texto
            .replace("R$", "")
            .replace("\xa0", "")
            .replace(" ", "")
            .replace(".", "")
            .replace(",", ".")
            .strip()
        )
        logger.info(f"[DEBUG] Valor limpo apos formatacao: '{valor_limpo}'")

        resultado = float(valor_limpo)
        logger.info(f"[DEBUG] Float convertido com sucesso: {resultado}")
        return resultado
    except Exception as e:
        logger.warn(f"[WARN] _obter_valor_a_pagar falhou com excecao: {e}")
        return None


# ---------------------------------------------------------------------------
# Busca dados da cota diretamente pelo id_cota
# ---------------------------------------------------------------------------

def _buscar_dados_cota(id_cota: int, logger: Optional[Logger] = None) -> dict:
    sql = """
        SELECT
            fc.nome_cliente,
            fc.grupo,
            fc.cota,
            fc.tentativas,
            fc.nome_consultor
        FROM tbl_fila_cotas fc
        WHERE fc.id_cota = %s
    """
    if logger:
        logger.info(f"[BANCO] SELECT tbl_fila_cotas (dados da cota) | id_cota={id_cota}")
    row = fetchone(sql, (id_cota,))

    if not row:
        if logger:
            logger.error(f"[BANCO] SELECT tbl_fila_cotas retornou vazio | id_cota={id_cota}")
        raise ValueError(f"Cota nao encontrada no banco: id_cota={id_cota}")

    dados = {
        "nome_cliente":   str(row[0] or "").strip(),
        "grupo":          str(row[1] or "").strip(),
        "cota":           str(row[2] or "").strip(),
        "tentativas":     int(row[3] or 0),
        "nome_consultor": str(row[4] or "").strip(),
    }
    if logger:
        logger.info(
            f"[BANCO] Dados da cota carregados | id_cota={id_cota} "
            f"cliente='{dados['nome_cliente']}' grupo={dados['grupo']} "
            f"cota={dados['cota']} tentativas={dados['tentativas']} "
            f"consultor='{dados['nome_consultor']}'"
        )
    return dados


def _arquivo_ja_existe_na_pasta_consultor(
    caminhos: Dict[str, str],
    grupo_dig: str,
    cota_dig: str,
    nome_consultor: str,
    logger: Logger,
) -> Optional[str]:
    """
    Verifica se ja existe um comprovante para esta cota na pasta do consultor.
    Usa grupo+cota como chave (nome do cliente pode variar).
    Retorna o caminho do arquivo se encontrado, None caso contrario.
    """
    pasta_consultor = os.path.join(
        caminhos["ofertados"], _nome_arquivo_seguro(nome_consultor)
    )
    if not os.path.isdir(pasta_consultor):
        return None

    prefixo = _nome_arquivo_seguro(f"{grupo_dig} {cota_dig} ")
    try:
        for nome_arq in os.listdir(pasta_consultor):
            if nome_arq.lower().endswith(".pdf") and nome_arq.startswith(prefixo):
                caminho = os.path.join(pasta_consultor, nome_arq)
                logger.info(f"[CHECK] Arquivo ja existe na pasta do consultor: {caminho}")
                return caminho
    except Exception as e:
        logger.warn(f"[CHECK] Erro ao verificar pasta do consultor: {e}")
    return None


def _montar_caminho_comprovante(
    caminhos: Dict[str, str],
    grupo_dig: str,
    cota_dig: str,
    nome_cliente: str,
    nome_consultor: str,
    ja_ofertado: bool,
) -> str:
    nome_arquivo = _nome_arquivo_seguro(f"{grupo_dig} {cota_dig} {nome_cliente}") + ".pdf"

    if ja_ofertado:
        pasta = caminhos["ja_ofertados"]
    else:
        pasta = os.path.join(caminhos["ofertados"], _nome_arquivo_seguro(nome_consultor))

    os.makedirs(pasta, exist_ok=True)
    return os.path.join(pasta, nome_arquivo)


# ---------------------------------------------------------------------------
# Helpers de deteccao de tela
# ---------------------------------------------------------------------------

def _diagnostico_pagina(page: Page, logger: Logger) -> str:
    """
    Inspeciona a pagina atual e retorna um prefixo de diagnostico para
    enriquecer mensagens de FALHA apos timeout.

    Retorna uma string curta, ex:
      "PAGINA_404"            — site retornou pagina de erro 404
      "PAGINA_OFFLINE"        — sem conexao / ERR_CONNECTION_REFUSED
      "PAGINA_INESPERADA:<url>"  — URL fora do AVAPRO
      ""                      — nenhum problema detectado (tela normal)
    """
    try:
        url = page.url or ""
    except Exception:
        return ""

    # Detecta 404 pelo conteudo da pagina
    try:
        corpo = page.locator("body").inner_text(timeout=1500)
    except Exception:
        corpo = ""

    corpo_lower = corpo.lower()

    if "nao encontramos essa pagina" in corpo_lower or "404" in corpo_lower:
        logger.warn(f"[DIAG] Pagina 404 detectada | url={url}")
        return "PAGINA_404"

    # Detecta erro de conexao (ERR_NET_*, ERR_CONNECTION_*)
    if "err_connection" in url.lower() or "err_name_not_resolved" in url.lower():
        logger.warn(f"[DIAG] Erro de conexao detectado | url={url}")
        return "PAGINA_OFFLINE"

    # Pagina fora do AVAPRO (ex: login expirado, redirect para outro dominio)
    if url and "avapro.ademicon.com.br" not in url:
        logger.warn(f"[DIAG] URL fora do AVAPRO | url={url}")
        return f"PAGINA_INESPERADA:{url[:120]}"

    return ""


def cota_indisponivel_visivel(page: Page, timeout_ms: int = 2000) -> bool:
    """
    Detecta a mensagem de cota indisponivel via Playwright puro (sem JS).

    Estrategia:
      1) p.text-lg com has_text='indispon' (substring, sem acento — mais rapido)
      2) Fallback: qualquer <p> com textos acentuados comuns
    """
    # Estrategia 1: classe especifica do AVAPRO para essa mensagem
    try:
        page.locator("p.text-lg").filter(has_text="indispon").first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except Exception:
        pass

    # Estrategia 2: fallback texto completo
    for texto in [
        "indisponível para oferta de lance",
        "A cota está indisponível",
        "cota está indisponível",
    ]:
        try:
            page.locator("p", has_text=texto).first.wait_for(state="visible", timeout=500)
            return True
        except Exception:
            pass

    return False


def _comprovante_visivel_na_tela(page: Page, timeout_ms: int = 2000) -> bool:
    try:
        page.locator("button:has-text('Comprovante')").first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except Exception:
        return False


def _tela_acompanhamento_visivel(page: Page, timeout_ms: int = 3000) -> bool:
    try:
        page.locator("text=Acompanhamento da oferta de lance").first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helper centralizado: clica Comprovante e emite resultado OFERTADO
# ---------------------------------------------------------------------------

def _clicar_comprovante_robusto(page: Page, logger: Logger) -> bool:
    """
    Clica o botao Comprovante com no_wait_after=True para nao travar quando
    a nova aba dispara window.print() automaticamente.

    Tenta 3 estrategias em sequencia: seletor padrao, seletor por icone FA,
    seletor por texto parcial (fallback mais amplo).
    """
    seletores = [
        "button:has-text('Comprovante')",
        "button:has(.fa-download)",
        "button[data-react-aria-pressable='true']:has-text('Comprovante')",
    ]

    for sel in seletores:
        try:
            btn = page.locator(sel).first
            btn.wait_for(state="visible", timeout=5000)
            try:
                btn.scroll_into_view_if_needed()
            except Exception:
                pass

            # Tentativa 1: click normal sem esperar navegacao posterior
            try:
                btn.click(no_wait_after=True)
                logger.info(f"[COMPROVANTE] Click normal | seletor={sel!r}")
                return True
            except Exception:
                pass

            # Tentativa 2: click via JavaScript (bypassa React synthetic events)
            try:
                handle = btn.element_handle(timeout=2000)
                if handle:
                    page.evaluate("el => el.click()", handle)
                    logger.info(f"[COMPROVANTE] JS click | seletor={sel!r}")
                    return True
            except Exception:
                pass

            # Tentativa 3: force click
            try:
                btn.click(force=True, no_wait_after=True)
                logger.info(f"[COMPROVANTE] Force click | seletor={sel!r}")
                return True
            except Exception:
                pass

        except Exception:
            continue

    logger.error("[COMPROVANTE] Todos os seletores falharam")
    return False


def _ir_para_comprovante(
    page: Page,
    id_cota: int,
    grupo_dig: str,
    cota_dig: str,
    nome_cliente: str,
    nome_consultor: str,
    caminhos: Dict[str, str],
    obs: str,
    ja_ofertado: bool,
    logger: Logger,
    valor_pagar: Optional[float] = None,
) -> dict:
    logger.info(f"[PASSO] Clicar Comprovante | obs={obs}")

    # Clica com no_wait_after=True para nao travar quando a nova aba
    # dispara window.print() automaticamente — o PAD e quem clica Imprimir.
    clicou = _clicar_comprovante_robusto(page, logger)

    if not clicou:
        evidencia = _screenshot(
            page, caminhos["falha"],
            f"comprovante_nao_encontrado_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
            logger,
        )
        logger.error("[ERRO] Botao Comprovante nao encontrado apos todas as tentativas")
        return _saida(
            "FALHA",
            "COMPROVANTE NAO ENCONTRADO APOS LANCE",
            caminho_print_falha=evidencia,
            valor_pagar=valor_pagar,
        )

    caminho_comprovante = _montar_caminho_comprovante(
        caminhos=caminhos,
        grupo_dig=grupo_dig,
        cota_dig=cota_dig,
        nome_cliente=nome_cliente,
        nome_consultor=nome_consultor,
        ja_ofertado=ja_ofertado,
    )

    logger.info(
        f"[RESULTADO] OFERTADO | valor_pagar={_fmt_brl(valor_pagar)} | {obs} | caminho={caminho_comprovante}"
    )
    return _saida("OFERTADO", obs, caminho_comprovante=caminho_comprovante, valor_pagar=valor_pagar)


# ---------------------------------------------------------------------------
# Voltar para tela inicial — replica voltar_tela_inicial.py inline.
# Reusa as mesmas funcoes do mapeamento_worker e e' chamada SEMPRE no
# finally do worker, para que a proxima cota encontre a tela de busca
# pronta independente do desfecho da cota atual.
# ---------------------------------------------------------------------------

def _voltar_para_tela_inicial(page: Page, logger: Logger) -> None:
    """
    Garante que o Edge volte para a tela de busca de cota.

    Sequencia (mesma de voltar_tela_inicial.py):
      1) clicar '+ Nova oferta de lance' (se visivel)
      2) clicar 'Alterar cota' (se visivel)
      3) garantir_tela_busca: tenta 'Voltar' ate o input[name=group] aparecer
      4) valida que group_input esta visivel; se nao, levanta RuntimeError

    Levanta RuntimeError em caso de falha — o caller deve capturar e logar
    (nao propagar, para nao invalidar o resultado da cota).
    """
    logger.info("[VOLTAR] Iniciando retorno a tela de busca")

    page.set_default_timeout(30000)

    clicar_nova_oferta(page, logger=logger)
    clicar_alterar_cota_se_visivel(page, logger=logger)

    logger.info("[VOLTAR] Garantindo tela de busca")
    garantir_tela_busca(page, logger=logger)

    group_input = page.locator("input[name='group']").first
    if not group_input.is_visible():
        raise RuntimeError("Tela de busca nao confirmada apos tentativas de retorno.")

    logger.info("[VOLTAR] Tela de busca confirmada")


# ---------------------------------------------------------------------------
# Worker principal — processa UMA cota e para
#
# Garantia de tela inicial:
#   - O voltar pra tela de busca acontece no INICIO do worker, NAO no fim.
#   - Motivo: quando uma cota anterior foi OFERTADO, o PAD (apos receber o
#     resultado do worker) ainda precisa fazer UI Edge — clicar Imprimir,
#     preencher campo "Nome:" com o caminho_comprovante, clicar Salvar.
#     Se o worker voltasse pra tela inicial no finally, a UI do PAD nao
#     conseguiria mais clicar Imprimir (a tela ja' teria mudado).
#   - Por isso, antes de pesquisar a proxima cota, o worker checa se ja'
#     esta na tela de busca; se nao, chama _voltar_para_tela_inicial.
#     Pra primeira cota (logo apos o login) o input ja' aparece e o check
#     passa direto sem custo.
# ---------------------------------------------------------------------------

def rodar_worker_lance(
    page: Page,
    id_cota: int,
    id_fila_adm: int,
    modalidade: str,
    caminhos: Dict[str, str],
    logger: Logger,
    timeout_s: int = 30,
) -> dict:
    return _executar_lance(
        page=page,
        id_cota=id_cota,
        id_fila_adm=id_fila_adm,
        modalidade=modalidade,
        caminhos=caminhos,
        logger=logger,
        timeout_s=timeout_s,
    )


def _executar_lance(
    page: Page,
    id_cota: int,
    id_fila_adm: int,
    modalidade: str,
    caminhos: Dict[str, str],
    logger: Logger,
    timeout_s: int = 30,
) -> dict:
    timeout_ms = min(max(timeout_s, 2), 60) * 1000
    wait_pos_continuar_ms = 60000

    config = obter_config_modalidade(modalidade)

    page.set_default_timeout(timeout_ms)
    page.set_default_navigation_timeout(timeout_ms)

    logger.info(
        f"[WORKER] Iniciado | id_cota={id_cota} id_fila_adm={id_fila_adm} "
        f"modalidade={config.modalidade} timeout_ms={timeout_ms}"
    )

    dados = _buscar_dados_cota(id_cota, logger=logger)

    nome_cliente   = dados["nome_cliente"]
    grupo_dig      = dados["grupo"]
    cota_dig       = _so_digitos(dados["cota"])
    nome_consultor = dados["nome_consultor"]

    # Subpasta de FALHA por cota, igual ao rpa_gerar_boleto:
    # evidencias/FALHA/{NOME_CLIENTE}_{GRUPO}_{COTA}/
    caminhos["falha_cota"] = pasta_falha_cota(
        caminhos["falha"], nome_cliente, grupo_dig, cota_dig
    )

    # Subpasta de NAO_BAIXADOS por cota — NAO_OFERTADO por regra de negocio
    # (cota indisponivel, tipo de lance inexistente, valor a pagar, TA
    # negativo). Falhas TECNICAS continuam indo para FALHA.
    caminhos["nao_baixado_cota"] = pasta_nao_baixado_cota(
        caminhos["evidencias"], nome_cliente, grupo_dig, cota_dig
    )

    # ---------------------------------------------------------
    # VERIFICACAO ANTECIPADA — arquivo ja existe na pasta do consultor?
    # Evita tentativas desnecessarias quando o lance ja foi realizado mas
    # o status no banco ficou como FALHA (ex: safeguard, timeout no comprovante).
    # ---------------------------------------------------------
    caminho_existente = _arquivo_ja_existe_na_pasta_consultor(
        caminhos, grupo_dig, cota_dig, nome_consultor, logger
    )
    if caminho_existente:
        logger.info(
            f"[WORKER] Arquivo ja existe na pasta do consultor — retornando OFERTADO | {caminho_existente}"
        )
        return _saida(
            "OFERTADO",
            "LANCE JA OFERTADO (ARQUIVO ENCONTRADO NA PASTA DO CONSULTOR)",
            caminho_comprovante=caminho_existente,
        )

    logger.info(f"[BANCO] UPDATE tbl_fila_cotas -> status=PROCESSANDO (marcar_cota_processando) | id_cota={id_cota}")
    marcar_cota_processando(id_cota)
    logger.info(f"[BANCO] Cota marcada como PROCESSANDO com sucesso | id_cota={id_cota}")

    logger.info(
        f"[ITEM] id_cota={id_cota} grupo={grupo_dig} "
        f"cota={cota_dig} cliente={nome_cliente}"
    )

    # ---------------------------------------------------------
    # PASSO 0 — Garantir tela de busca antes de pesquisar
    # ---------------------------------------------------------
    # Cobre o cenario tipico: cota anterior foi OFERTADO -> PAD fez UI
    # (clicar Imprimir, preencher Nome, clicar Salvar) -> tela ficou em
    # "comprovante salvo" ou similar. Sem voltar primeiro, o
    # preencher_grupo_cota nao acharia o input[name='group'].
    # Pra primeira cota (logo apos o login) a tela de busca ja' esta visivel
    # e o check passa direto sem invocar _voltar_para_tela_inicial.
    try:
        ja_na_busca = page.locator("input[name='group']").first.is_visible()
    except Exception:
        ja_na_busca = False

    if ja_na_busca:
        logger.info("[PASSO] Tela de busca ja' visivel — pulando voltar")
    else:
        logger.info("[PASSO] Tela de busca nao detectada — voltando")
        try:
            _voltar_para_tela_inicial(page, logger)
        except Exception as e_voltar:
            diag = _diagnostico_pagina(page, logger)
            prefixo = f"{diag} | " if diag else ""
            evidencia = _screenshot(
                page, caminhos["falha"],
                f"falha_voltar_inicial_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
                logger,
            )
            logger.error(f"[WORKER] Nao foi possivel garantir tela de busca: {e_voltar}")
            return _saida(
                "FALHA",
                f"{prefixo}NAO FOI POSSIVEL VOLTAR PARA TELA DE BUSCA: {e_voltar}",
                caminho_print_falha=evidencia,
            )

    try:
        # ---------------------------------------------------------
        # PASSO 1 — Pesquisar cota
        # ---------------------------------------------------------
        logger.info("[PASSO] Preencher Grupo/Cota")
        preencher_grupo_cota(page, grupo_dig, cota_dig, timeout_ms=timeout_ms, logger=logger)

        logger.info("[PASSO] Pesquisar")
        # A tela pode ainda exibir o RESULTADO DA COTA ANTERIOR (mensagem de
        # indisponivel residual), ja que o Edge e reaproveitado entre cotas.
        # Registra o estado ANTES do clique para nao confundir residuo com o
        # resultado da pesquisa atual.
        _indisp_loc = page.locator("p.text-lg").filter(has_text="indispon")
        try:
            _indisp_residuo = _indisp_loc.first.is_visible()
        except Exception:
            _indisp_residuo = False
        if _indisp_residuo:
            logger.info("[INFO] Mensagem de indisponivel RESIDUAL visivel antes de pesquisar")

        clicar_pesquisar(page, timeout_ms=timeout_ms, logger=logger)

        logger.info("[PASSO] Aguardar card ou mensagem de indisponivel")
        card_title = obter_card_cota(page, grupo_dig, cota_dig)

        # Poll a cada 150ms ate um sinal CONFIAVEL:
        #   "card"         -> card da cota visivel
        #   "indisponivel" -> mensagem confirmada em 2 leituras com 1s de
        #                     intervalo, e somente depois de qualquer residuo
        #                     da cota anterior ter sumido da tela
        #   "timeout"      -> nenhum sinal no prazo -> FALHA TECNICA (retry).
        #                     NUNCA marcar indisponivel sem ver a mensagem.
        import time as _t
        _espera_s  = max(timeout_ms / 1000.0, 30.0)  # pesquisa lenta nao vira indisponivel
        _t0_busca  = _t.perf_counter()
        _deadline  = _t0_busca + _espera_s
        _pode_confiar_indisp = not _indisp_residuo
        _resultado_sinal = "timeout"
        while _t.perf_counter() < _deadline:
            try:
                if card_title.is_visible():
                    _resultado_sinal = "card"
                    break
            except Exception:
                pass

            try:
                _indisp_visivel = _indisp_loc.first.is_visible()
            except Exception:
                _indisp_visivel = False

            if not _indisp_visivel:
                # A pagina limpou os resultados (skeleton de loading): a partir
                # daqui, se a mensagem aparecer, e desta pesquisa.
                _pode_confiar_indisp = True
            elif _pode_confiar_indisp:
                # Confirmacao: espera 1s e rele — da chance do card renderizar
                _t.sleep(1.0)
                try:
                    if card_title.is_visible():
                        _resultado_sinal = "card"
                        break
                except Exception:
                    pass
                try:
                    if _indisp_loc.first.is_visible():
                        _resultado_sinal = "indisponivel"
                        break
                except Exception:
                    pass

            _t.sleep(0.15)

        logger.info(
            f"[INFO] Sinal pos-pesquisar: {_resultado_sinal!r} "
            f"(apos {_t.perf_counter()-_t0_busca:.1f}s)"
        )

        if _resultado_sinal == "timeout":
            # Sem card e sem mensagem: NAO da para afirmar que a cota esta
            # indisponivel. FALHA tecnica -> PAD retenta.
            obs = "TIMEOUT: resultado da pesquisa nao carregou (nem card nem mensagem de indisponivel)"
            evidencia = _screenshot(
                page, caminhos["falha_cota"],
                f"timeout_pesquisa_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
                logger,
            )
            logger.error(f"[RESULTADO] FALHA | {obs}")
            return _saida("FALHA", obs, caminho_print_falha=evidencia)

        if _resultado_sinal == "indisponivel":
            obs = "COTA INDISPONIVEL PARA LANCE"
            evidencia = _screenshot(
                page, caminhos["nao_baixado_cota"],
                f"cota_indisponivel_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
                logger,
            )
            logger.warn(f"[RESULTADO] NAO_OFERTADO | {obs}")
            return _saida("NAO_OFERTADO", obs, caminho_print_falha=evidencia)

        # ---------------------------------------------------------
        # PASSO 2 — Logar badge do card
        # ---------------------------------------------------------
        lance_badge = card_tem_lance_realizado(card_title)
        logger.info(f"[INFO] Badge 'Lance realizado' no card: {lance_badge}")

        # ---------------------------------------------------------
        # PASSO 3 — Abrir card
        # ---------------------------------------------------------
        logger.info("[PASSO] Abrir card")
        clicar_card_cota(page, grupo_dig, cota_dig, timeout_ms=timeout_ms, logger=logger)

        if _tela_acompanhamento_visivel(page, timeout_ms=3000):
            logger.info("[INFO] Tela de acompanhamento detectada — lance ja realizado")
            return _ir_para_comprovante(
                page=page,
                id_cota=id_cota,
                grupo_dig=grupo_dig,
                cota_dig=cota_dig,
                nome_cliente=nome_cliente,
                nome_consultor=nome_consultor,
                caminhos=caminhos,
                obs="LANCE JA OFERTADO (TELA DE ACOMPANHAMENTO)",
                ja_ofertado=True,
                logger=logger,
            )

        # ---------------------------------------------------------
        # PASSO 4 — Fluxo normal: tipo de lance -> Continuar
        # ---------------------------------------------------------
        logger.info(f"[PASSO] Clicar tipo de lance: {config.botao_lance}")
        try:
            clicar_tipo_lance(page, config.botao_lance, timeout_ms=timeout_ms, logger=logger)
        except PlaywrightTimeoutError:
            # Erro mapeado: o card especifico da modalidade nao apareceu na
            # tela (ex: cota so aceita Lance Livre / Lance Quitacao). Nao e
            # falha tecnica - e regra do AVAPRO que invalida essa cota para
            # esta modalidade. Retornamos NAO_OFERTADO com print, evitando
            # as 3 tentativas de retry desnecessarias.
            #
            # IMPORTANTE: NAO usar aspas simples (') na observacao, pois o
            # PAD faz UPDATE manual usando ' como delimitador de string SQL,
            # e qualquer apostrofo aqui quebra o UPDATE com erro de sintaxe.
            obs = f"Opcao de {config.botao_lance.lower()} nao apareceu"
            evidencia = _screenshot(
                page, caminhos["nao_baixado_cota"],
                f"tipo_lance_indisponivel_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
                logger,
            )
            logger.warn(f"[RESULTADO] NAO_OFERTADO | {obs}")
            return _saida("NAO_OFERTADO", obs, caminho_print_falha=evidencia)
        sleep(2)

        logger.info("[PASSO] Clicar Continuar")
        clicar_continuar(page, timeout_ms=timeout_ms, logger=logger)

        # ---------------------------------------------------------
        # PASSOs 5-7 UNIFICADO — wait_for_function detecta qualquer sinal
        # pos-Continuar simultaneamente: TA negativo, Valor a pagar, botoes.
        # Elimina esperas sequenciais — age assim que o primeiro sinal surgir.
        # ---------------------------------------------------------
        _JS_SINAL_POS_CONTINUAR = """
        () => {
            const isVis = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && window.getComputedStyle(el).visibility !== 'hidden';
            };
            const btn = txt => {
                for (const b of document.querySelectorAll('button')) {
                    if (isVis(b) && (b.innerText || '').trim().includes(txt)) return b;
                }
                return null;
            };
            const bodyTxt = document.body ? (document.body.innerText || '') : '';
            return (
                !!btn('Ofertar lance') ||
                !!btn('Comprovante') ||
                !!btn('+ Nova oferta de lance') ||
                bodyTxt.includes('Valor a pagar') ||
                bodyTxt.includes('ficou negativo') ||
                bodyTxt.includes('TA da parcela') ||
                bodyTxt.includes('TA negativo')
            );
        }
        """

        logger.info("[WAIT] Aguardando sinal pos-Continuar (TA / Valor a pagar / botoes)")
        try:
            page.wait_for_function(_JS_SINAL_POS_CONTINUAR, timeout=wait_pos_continuar_ms)
        except Exception as e_wait:
            logger.warn(f"[WARN] wait_for_function pos-Continuar expirou: {e_wait}")

        # Leitura unica do estado — sem waits extras porque wait_for_function
        # ja' garantiu que pelo menos um elemento esta visivel
        estado            = estado_pos_continuar(page)
        ofertar_visivel   = estado["ofertar_visivel"]
        nova_visivel      = estado["nova_visivel"]
        comprovante_visivel = estado["comprovante_visivel"]

        logger.info(
            f"[INFO] Pos-Continuar: comprovante={comprovante_visivel} "
            f"ofertar={ofertar_visivel} nova={nova_visivel}"
        )

        # 5 — TA negativo? (verificacao rapida — sinal ja' pode estar na pagina)
        # Usa timeout curto pois wait_for_function ja' confirmou que o sinal esta presente.
        # Captura o texto completo (inclui numero da cota do toast, ex: "Cota: 000830/3161-00")
        texto_ta = toast_ta_negativo_visivel(page, timeout_ms=500)
        if texto_ta is not None:
            obs = f"TA DA PARCELA NEGATIVO: {texto_ta}"
            evidencia = _screenshot(
                page, caminhos["nao_baixado_cota"],
                f"ta_negativo_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
                logger,
            )
            logger.warn(f"[RESULTADO] TA negativo | {obs}")
            return _saida("NAO_OFERTADO", obs, caminho_print_falha=evidencia)

        # 7a — Comprovante ja visivel (lance ja foi feito)
        if comprovante_visivel:
            return _ir_para_comprovante(
                page=page, id_cota=id_cota, grupo_dig=grupo_dig, cota_dig=cota_dig,
                nome_cliente=nome_cliente, nome_consultor=nome_consultor,
                caminhos=caminhos, obs="LANCE JA OFERTADO (COMPROVANTE VISIVEL)",
                ja_ofertado=True, logger=logger,
            )

        # Nenhum botao esperado — busca extra
        if not ofertar_visivel and not nova_visivel and not comprovante_visivel:
            if _comprovante_visivel_na_tela(page, timeout_ms=4000):
                logger.info("[INFO] Comprovante encontrado em busca extra")
                return _ir_para_comprovante(
                    page=page, id_cota=id_cota, grupo_dig=grupo_dig, cota_dig=cota_dig,
                    nome_cliente=nome_cliente, nome_consultor=nome_consultor,
                    caminhos=caminhos, obs="LANCE JA OFERTADO (COMPROVANTE VISIVEL POS-CONTINUAR)",
                    ja_ofertado=True, logger=logger,
                )
            obs = "SEM BOTOES APOS CONTINUAR"
            evidencia = _screenshot(
                page, caminhos["falha_cota"],
                f"sem_botoes_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
                logger,
            )
            return _saida("NAO_OFERTADO", obs, caminho_print_falha=evidencia)

        # 7b — Valor a pagar: ja esta na tela (wait_for_function detectou),
        #       le imediatamente sem sleep adicional
        logger.info("[PASSO] Lendo Valor a pagar")
        valor_a_pagar = _obter_valor_a_pagar(page, logger, timeout_ms=3000)
        logger.info(f"[INFO] Valor a pagar: {_fmt_brl(valor_a_pagar)}")

        if valor_a_pagar is None:
            logger.warn("[WARN] Nao foi possivel ler Valor a pagar — prosseguindo")
        elif valor_a_pagar > 0.0:
            obs = f"VALOR A PAGAR MAIOR QUE ZERO: {_fmt_brl(valor_a_pagar)}"
            evidencia = _screenshot(
                page, caminhos["nao_baixado_cota"],
                f"valor_a_pagar_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
                logger,
            )
            logger.warn(f"[RESULTADO] NAO_OFERTADO | {obs}")
            return _saida("NAO_OFERTADO", obs, caminho_print_falha=evidencia, valor_pagar=valor_a_pagar)
        else:
            logger.info("[INFO] Valor a pagar OK (R$ 0,00) — clicando Ofertar lance")

        # 7c — Re-checar visibilidade do botao Ofertar lance
        btn_re = page.locator("button:has-text('Ofertar lance')").first
        try:
            ofertar_ainda_visivel = btn_re.is_visible()
        except Exception:
            ofertar_ainda_visivel = False

        if not ofertar_ainda_visivel:
            if _comprovante_visivel_na_tela(page, timeout_ms=3000):
                logger.info("[INFO] Ofertar sumiu mas Comprovante visivel — assumindo lance feito")
                return _ir_para_comprovante(
                    page=page, id_cota=id_cota, grupo_dig=grupo_dig, cota_dig=cota_dig,
                    nome_cliente=nome_cliente, nome_consultor=nome_consultor,
                    caminhos=caminhos, obs="LANCE JA OFERTADO (COMPROVANTE VISIVEL POS-VALIDACAO)",
                    ja_ofertado=True, logger=logger, valor_pagar=valor_a_pagar,
                )
            evidencia = _screenshot(
                page, caminhos["falha_cota"],
                f"ofertar_btn_sumiu_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
                logger,
            )
            logger.error("[ERRO] Botao Ofertar lance desapareceu apos validacao")
            return _saida(
                "FALHA", "BOTAO OFERTAR LANCE DESAPARECEU APOS VALIDACAO",
                caminho_print_falha=evidencia, valor_pagar=valor_a_pagar,
            )

        # 7d — Clicar Ofertar lance
        logger.info("[PASSO] Clicar Ofertar lance")
        clicar_ofertar_lance(page, timeout_ms=wait_pos_continuar_ms, logger=logger)
        logger.info("[INFO] clicar_ofertar_lance executado — verificando resposta")

        toast_texto = obter_texto_toast(page, timeout_ms=3000)

        if toast_texto:
            logger.info(f"[TOAST] Texto capturado: '{toast_texto}'")
            if "sucesso" not in toast_texto.lower():
                # Toast sem sucesso apos clicar Ofertar = tratado como FALHA
                # TECNICA: evidencia em FALHA e status FALHA para o PAD
                # retentar a cota.
                obs = f"ERRO NO TOAST: {toast_texto}"
                evidencia = _screenshot(
                    page, caminhos["falha_cota"],
                    f"erro_toast_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
                    logger,
                )
                return _saida("FALHA", obs, caminho_print_falha=evidencia, valor_pagar=valor_a_pagar)
        else:
            logger.warn("[WARN] Sem toast — verificando transicao de tela")
            if _comprovante_visivel_na_tela(page, timeout_ms=3000):
                logger.info("[INFO] Comprovante visivel — lance feito com sucesso")
            elif _tela_acompanhamento_visivel(page, timeout_ms=3000):
                logger.info("[INFO] Tela de acompanhamento visivel — lance feito")
            elif toast_sucesso_visivel(page, timeout_ms=3000):
                logger.info("[INFO] Toast de sucesso confirmado por seletor alternativo")
            else:
                evidencia = _screenshot(
                    page, caminhos["falha_cota"],
                    f"sem_resposta_ofertar_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
                    logger,
                )
                logger.error("[ERRO] Sem toast e sem transicao de tela apos clicar Ofertar lance")
                return _saida(
                    "FALHA", "SEM RESPOSTA APOS CLICAR OFERTAR LANCE",
                    caminho_print_falha=evidencia, valor_pagar=valor_a_pagar,
                )

        # ---------------------------------------------------------
        # PASSO 8 — Lance feito — clicar Comprovante
        # ---------------------------------------------------------
        return _ir_para_comprovante(
            page=page,
            id_cota=id_cota,
            grupo_dig=grupo_dig,
            cota_dig=cota_dig,
            nome_cliente=nome_cliente,
            nome_consultor=nome_consultor,
            caminhos=caminhos,
            obs="LANCE REALIZADO COM SUCESSO",
            ja_ofertado=False,
            logger=logger,
            valor_pagar=valor_a_pagar,
        )

    except Exception as e:
        diag = _diagnostico_pagina(page, logger)

        # Detecta sessao AVAPRO expirada (pagina redirecionou para /login)
        _e_erro_login = diag and "PAGINA_INESPERADA" in diag and "login" in diag.lower()

        if diag:
            obs = f"{diag} | {type(e).__name__}: {e}"
        else:
            obs = f"FALHA NO FLUXO: {e}"

        if _e_erro_login:
            # Screenshot vai para pasta dedicada no mesmo nivel de FALHA
            pasta_print = pasta_erro_login_processamento(
                caminhos["evidencias"], grupo_dig, cota_dig
            )
        else:
            pasta_print = caminhos.get("falha_cota", caminhos["falha"])

        evidencia = _screenshot(
            page, pasta_print,
            f"falha_{grupo_dig}_{cota_dig}_{_nome_arquivo_seguro(nome_cliente)}",
            logger,
        )
        logger.error(f"[ERRO] {obs}")
        return _saida("FALHA", obs, caminho_print_falha=evidencia)
