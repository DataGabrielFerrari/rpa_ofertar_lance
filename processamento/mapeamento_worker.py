from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


def estado_elemento(loc, timeout_ms: int = 1000) -> str:
    """
    Descreve o estado atual de um elemento para log de auditoria:
        visivel=sim habilitado=sim texto='Pesquisar'

    Nunca levanta excecao — se nao conseguir ler, informa 'indeterminado'.
    """
    try:
        visivel = loc.is_visible()
    except Exception:
        visivel = None
    try:
        habilitado = loc.is_enabled(timeout=timeout_ms) if visivel else None
    except Exception:
        habilitado = None
    try:
        texto = (loc.inner_text(timeout=timeout_ms) or "").strip()[:60] if visivel else ""
    except Exception:
        texto = ""

    def _fmt(v):
        if v is None:
            return "indeterminado"
        return "sim" if v else "nao"

    return f"visivel={_fmt(visivel)} habilitado={_fmt(habilitado)} texto='{texto}'"


def _log(logger, msg: str) -> None:
    """Log seguro: ignora silenciosamente quando logger=None."""
    if logger is not None:
        try:
            logger.info(msg)
        except Exception:
            pass


def clicar_se_visivel(page: Page, seletor: str, timeout_ms: int = 3000) -> bool:
    try:
        loc = page.locator(seletor).first
        loc.wait_for(state="visible", timeout=timeout_ms)
        loc.click()
        return True
    except Exception:
        return False


def clicar_texto_se_visivel(page: Page, texto: str, timeout_ms: int = 3000) -> bool:
    try:
        loc = page.locator(f"text={texto}").first
        loc.wait_for(state="visible", timeout=timeout_ms)
        loc.click()
        return True
    except Exception:
        return False


def clicar_botao_por_texto(
    page: Page,
    texto: str,
    timeout_ms: int = 5000,
    force: bool = False,
    no_wait_after: bool = False,
    logger=None,
) -> bool:
    """
    Clica num botao pelo texto visivel com 3 tentativas progressivas:
      1) click normal
      2) click via JavaScript (bypassa overlays e React synthetic events)
      3) click forcado (ignora checks de actionability)
    """
    try:
        btn = page.locator(f"button:has-text('{texto}')").first
        btn.wait_for(state="visible", timeout=timeout_ms)
        try:
            btn.scroll_into_view_if_needed()
        except Exception:
            pass

        _log(logger, f"[BOTAO] '{texto}' antes do clique | {estado_elemento(btn)}")

        # Tentativa 1: click normal
        try:
            btn.click(force=force, no_wait_after=no_wait_after)
            _log(logger, f"[BOTAO] '{texto}' clicado com sucesso (tentativa 1: click normal)")
            return True
        except Exception as e1:
            _log(logger, f"[BOTAO] '{texto}' tentativa 1 (click normal) falhou: {e1}")

        # Tentativa 2: click via JavaScript
        try:
            handle = btn.element_handle(timeout=2000)
            if handle:
                page.evaluate("el => el.click()", handle)
                _log(logger, f"[BOTAO] '{texto}' clicado com sucesso (tentativa 2: JS click)")
                return True
        except Exception as e2:
            _log(logger, f"[BOTAO] '{texto}' tentativa 2 (JS click) falhou: {e2}")

        # Tentativa 3: force click
        try:
            btn.click(force=True, no_wait_after=no_wait_after)
            _log(logger, f"[BOTAO] '{texto}' clicado com sucesso (tentativa 3: force click)")
            return True
        except Exception as e3:
            _log(logger, f"[BOTAO] '{texto}' tentativa 3 (force click) falhou: {e3}")

        return False
    except Exception as e_wait:
        _log(logger, f"[BOTAO] '{texto}' nao ficou visivel em {timeout_ms}ms: {e_wait}")
        return False


def _preencher_input(inp, valor: str, timeout_ms: int = 3000) -> None:
    """
    Preenche um input de forma robusta:
      1) click para focar
      2) Ctrl+A para selecionar tudo
      3) fill (limpa e digita de uma vez — sem delay de keystroke)
      4) verifica se o valor ficou correto; se nao, tenta press_sequentially
    """
    inp.wait_for(state="visible", timeout=timeout_ms)
    try:
        inp.click(timeout=timeout_ms)
    except Exception:
        pass
    try:
        inp.press("Control+a")
    except Exception:
        pass
    inp.fill(str(valor))

    # Verifica se o valor foi aceito
    try:
        atual = inp.input_value(timeout=1000)
        if atual != str(valor):
            # Fallback: digita caractere a caractere
            inp.click()
            inp.press("Control+a")
            inp.press_sequentially(str(valor), delay=30)
    except Exception:
        pass


def preencher_grupo_cota(page: Page, grupo: str, cota: str, timeout_ms: int = 5000, logger=None) -> None:
    inp_grupo = page.locator("input[name='group']").first
    inp_cota  = page.locator("input[name='quota']").first

    _log(logger, f"[INPUT] Aguardando campos Grupo/Cota | seletores=input[name='group'], input[name='quota'] timeout_ms={timeout_ms}")
    inp_grupo.wait_for(state="visible", timeout=timeout_ms)
    inp_cota.wait_for(state="visible", timeout=timeout_ms)

    _log(logger, f"[INPUT] Preenchendo campo Grupo | valor='{grupo}' | estado_antes: {estado_elemento(inp_grupo)}")
    _preencher_input(inp_grupo, grupo, timeout_ms=timeout_ms)
    _log(logger, f"[INPUT] Preenchendo campo Cota | valor='{cota}' | estado_antes: {estado_elemento(inp_cota)}")
    _preencher_input(inp_cota,  cota,  timeout_ms=timeout_ms)

    # Confirma o valor efetivamente aceito pelos inputs (auditoria)
    try:
        _log(logger,
             f"[INPUT] Valores confirmados nos campos | "
             f"grupo='{inp_grupo.input_value(timeout=1000)}' cota='{inp_cota.input_value(timeout=1000)}'")
    except Exception:
        pass


def clicar_pesquisar(page: Page, timeout_ms: int = 5000, logger=None) -> None:
    btn = page.locator("button:has-text('Pesquisar')").first
    btn.wait_for(state="visible", timeout=timeout_ms)
    _log(logger, f"[BOTAO] 'Pesquisar' antes do clique | {estado_elemento(btn)}")
    btn.click()
    _log(logger, "[BOTAO] 'Pesquisar' clicado com sucesso (click normal)")


def _num(s: str) -> str:
    """Remove zeros a esquerda para comparacao numerica."""
    d = "".join(ch for ch in str(s or "") if ch.isdigit())
    return str(int(d)) if d else d


def obter_card_cota(page: Page, grupo: str, cota: str):
    """
    Localiza o <p> do card usando regex que ignora zeros a esquerda.

    AVAPRO pode exibir 'Grupo 001010 / Cota 586' mesmo quando o banco
    armazena grupo='1010'. A regex 0*{n} casa ambos os formatos.
    """
    import re as _re
    g = _re.escape(_num(grupo))
    c = _re.escape(_num(cota))
    padrao = _re.compile(
        rf"Grupo\s+0*{g}\s*/\s*Cota\s+0*{c}\b",
        _re.IGNORECASE,
    )
    return page.locator("p").filter(has_text=padrao).first


def clicar_card_cota(page: Page, grupo: str, cota: str, timeout_ms: int = 5000, logger=None):
    """
    Acha o <p> do card e clica no <div class='cursor-pointer'> pai —
    que e onde o React registra o onClick, nao no <p> filho.

    Estrutura real do AVAPRO:
      <div class="p-6 ... cursor-pointer">   <- onClick aqui
        ...
        <p class="text-xl text-dark-50">Grupo X / Cota Y</p>
        ...
      </div>

    Usa filter(has=card_title) para achar o div.cursor-pointer
    que contem o <p> — mais confiavel que XPath ancestor.
    """
    card_title = obter_card_cota(page, grupo, cota)
    card_title.wait_for(state="visible", timeout=timeout_ms)

    try:
        card_title.scroll_into_view_if_needed()
    except Exception:
        pass

    # Div clicavel: o div.cursor-pointer que contem o <p> do card
    card_div = page.locator("div.cursor-pointer").filter(has=card_title)
    _log(logger, f"[CARD] Card da cota grupo={grupo} cota={cota} localizado | {estado_elemento(card_title)}")

    # Tentativa 1: click normal no div
    try:
        card_div.click(timeout=3000)
        _log(logger, "[CARD] Clique no card OK (tentativa 1: click normal no div.cursor-pointer)")
        return card_title
    except Exception as e1:
        _log(logger, f"[CARD] Tentativa 1 (click normal no div) falhou: {e1}")

    # Tentativa 2: JS click no div
    try:
        handle = card_div.element_handle(timeout=2000)
        if handle:
            page.evaluate("el => el.click()", handle)
            _log(logger, "[CARD] Clique no card OK (tentativa 2: JS click no div)")
            return card_title
    except Exception as e2:
        _log(logger, f"[CARD] Tentativa 2 (JS click no div) falhou: {e2}")

    # Tentativa 3: force click no div
    try:
        card_div.click(force=True)
        _log(logger, "[CARD] Clique no card OK (tentativa 3: force click no div)")
        return card_title
    except Exception as e3:
        _log(logger, f"[CARD] Tentativa 3 (force click no div) falhou: {e3}")

    # Tentativa 4: JS click no <p> (event bubbles ao React)
    try:
        handle = card_title.element_handle(timeout=1000)
        if handle:
            page.evaluate("el => el.click()", handle)
            _log(logger, "[CARD] Clique no card OK (tentativa 4: JS click no <p>)")
            return card_title
    except Exception as e4:
        _log(logger, f"[CARD] Tentativa 4 (JS click no <p>) falhou: {e4}")

    # Tentativa 5: force click no <p>
    card_title.click(force=True)
    _log(logger, "[CARD] Clique no card OK (tentativa 5: force click no <p>)")
    return card_title


def obter_container_card(card_title):
    return card_title.page().locator("div.cursor-pointer").filter(has=card_title)


def card_tem_lance_realizado(card_title) -> bool:
    try:
        container = obter_container_card(card_title)
        badge = container.locator("text=Lance realizado").first
        return badge.is_visible()
    except Exception:
        return False


def clicar_tipo_lance(
    page: Page,
    botao_lance: str,
    timeout_ms: int = 5000,
    logger=None,
) -> None:
    """
    Clica no card do tipo de lance (ex: 'Lance Fixo', 'Segundo Lance Fixo').

    Estrategia em 3 camadas — a primaria e identica a versao original:

      CAMADA 1 (rapida — identica a versao anterior):
        Tenta seletores Playwright diretos em ordem; cada um com ate 3
        estrategias de clique: normal → JS → force.
        Retorna imediatamente no primeiro clique bem-sucedido.

      CAMADA 2 (fallback — JS DOM traversal):
        Varre o DOM, acha o no com o texto exato e sobe ate o ancestral
        clicavel. Aciona dispatchEvent + .click() para cobrir React onClick.

      CAMADA 3 (ultimo recurso — get_by_text Playwright):
        Playwright semantico com force e clique no pai imediato.

    Cada tentativa e logada com [TIPO_LANCE] para facilitar diagnostico.
    Levanta PlaywrightTimeoutError se nenhuma camada encontrar o elemento.
    """
    import time

    def _log(msg: str) -> None:
        if logger:
            logger.info(f"[TIPO_LANCE] {msg}")

    t0 = time.perf_counter()

    # ── PRE-CHECK em 2 etapas ────────────────────────────────────────────────
    # ETAPA 1: aguarda a tela "Selecione o tipo de lance" carregar. Essa tela
    # so aparece DEPOIS de digitar grupo/cota, pesquisar e clicar no card do
    # cliente — a transicao pode ser lenta, entao espera ate timeout_ms.
    #
    # ETAPA 2: com a tela carregada, verifica se o card da modalidade
    # (ex: 'Lance Fixo', 'Segundo Lance Fixo') existe. Se nao aparecer em
    # ate 10s, a cota nao oferece esta modalidade → PlaywrightTimeoutError
    # imediato, e o worker responde NAO_OFERTADO bem antes do watchdog de
    # 90s do PAD (antes: 6 seletores x timeout_ms = ate 6 min → FALHA/TIMEOUT).
    try:
        page.get_by_text("Selecione o tipo de lance").first.wait_for(
            state="visible", timeout=timeout_ms
        )
        _log(
            f"PRE-CHECK: tela de tipo de lance visivel | "
            f"elapsed={time.perf_counter()-t0:.2f}s"
        )
    except Exception:
        # Texto do cabecalho pode variar — nao aborta por isso; segue para a
        # verificacao do card, que e o criterio que decide de verdade.
        _log(
            f"PRE-CHECK: cabecalho 'Selecione o tipo de lance' nao visivel "
            f"apos {timeout_ms}ms — verificando card mesmo assim"
        )

    # Usa o MESMO seletor primario da Camada 1 (button:has-text = substring),
    # pois o texto do botao vem junto com a descricao ("Segundo Lance Fixo\n
    # Os lances fixos desse grupo sao de ate 25%...") — busca exata nao casa.
    pre_check_ms = min(timeout_ms, 10000)
    try:
        page.locator(f"button:has-text('{botao_lance}')").first.wait_for(
            state="visible", timeout=pre_check_ms
        )
    except Exception:
        _log(
            f"PRE-CHECK: card '{botao_lance}' nao encontrado em "
            f"{pre_check_ms}ms — abortando sem varrer camadas"
        )
        raise PlaywrightTimeoutError(
            f"Tipo de lance '{botao_lance}' nao encontrado (pre-check {pre_check_ms}ms)"
        )
    _log(f"PRE-CHECK: card '{botao_lance}' presente | elapsed={time.perf_counter()-t0:.2f}s")

    # Card existe: limita a espera por seletor para o caso de algum seletor
    # da Camada 1 nao casar (evita 6 x 60s mesmo com card presente).
    timeout_ms = min(timeout_ms, 8000)

    # ── CAMADA 1: seletores Playwright (rapida — caminho principal) ──────────
    # PRIMARY: button:has-text — confirmado via teste no AVAPRO (lance types sao <button>).
    # Os demais ficam como fallback para eventuais variacoes de implementacao.
    seletores = [
        f"button:has-text('{botao_lance}')",        # PRIMARY (confirmado live test)
        f"[role='radio']:has-text('{botao_lance}')",
        f"[role='option']:has-text('{botao_lance}')",
        f"label:has-text('{botao_lance}')",
        f"text={botao_lance}",
        f"span:has-text('{botao_lance}')",
    ]

    for sel in seletores:
        t_sel = time.perf_counter()
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)

            try:
                loc.scroll_into_view_if_needed()
            except Exception:
                pass

            # Estrategia A: click normal
            try:
                loc.click(timeout=3000)
                _log(
                    f"C1-A click normal OK | sel={sel!r} "
                    f"elapsed={time.perf_counter()-t0:.2f}s"
                )
                return
            except Exception as e_a:
                _log(f"C1-A falhou | sel={sel!r} erro={e_a}")

            # Estrategia B: JS click
            try:
                handle = loc.element_handle(timeout=1000)
                if handle:
                    page.evaluate("el => el.click()", handle)
                    _log(
                        f"C1-B JS click OK | sel={sel!r} "
                        f"elapsed={time.perf_counter()-t0:.2f}s"
                    )
                    return
            except Exception as e_b:
                _log(f"C1-B falhou | sel={sel!r} erro={e_b}")

            # Estrategia C: force click
            try:
                loc.click(force=True)
                _log(
                    f"C1-C force click OK | sel={sel!r} "
                    f"elapsed={time.perf_counter()-t0:.2f}s"
                )
                return
            except Exception as e_c:
                _log(f"C1-C falhou | sel={sel!r} erro={e_c}")

        except Exception as e_wait:
            _log(
                f"C1 sel nao encontrado | sel={sel!r} "
                f"t_sel={time.perf_counter()-t_sel:.2f}s erro={e_wait}"
            )
            continue

    _log(f"C1 esgotada sem sucesso | {len(seletores)} seletores testados | elapsed={time.perf_counter()-t0:.2f}s")

    # ── CAMADA 2: JS puro — sobe ao ancestral clicavel ──────────────────────
    _log("C2 iniciando JS DOM traversal")
    t_c2 = time.perf_counter()
    try:
        resultado_js = page.evaluate(
            """
            (texto) => {
                function ehClicavel(el) {
                    if (!el) return false;
                    const tag = el.tagName.toLowerCase();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    return (
                        tag === 'button' || tag === 'a' || tag === 'label' ||
                        role === 'radio' || role === 'button' || role === 'option' ||
                        role === 'tab' || role === 'menuitem' ||
                        el.onclick !== null ||
                        (el.style && el.style.cursor === 'pointer') ||
                        el.hasAttribute('data-value') || el.hasAttribute('data-id')
                    );
                }
                const candidatos = Array.from(document.querySelectorAll('*')).filter(el => {
                    const t = (el.innerText || el.textContent || '').trim();
                    return t === texto && el.offsetParent !== null;
                });
                if (candidatos.length === 0) return { ok: false, motivo: 'nenhum_candidato' };
                for (const el of candidatos) {
                    const tag = el.tagName.toLowerCase();
                    if (ehClicavel(el)) {
                        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        el.click();
                        return { ok: true, motivo: 'proprio_elemento', tag: tag };
                    }
                    let ancestor = el.parentElement;
                    let nivel = 0;
                    while (ancestor && nivel < 6) {
                        nivel++;
                        if (ehClicavel(ancestor)) {
                            ancestor.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                            ancestor.click();
                            return { ok: true, motivo: 'ancestral_nivel_' + nivel, tag: ancestor.tagName.toLowerCase() };
                        }
                        ancestor = ancestor.parentElement;
                    }
                    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    el.click();
                    return { ok: true, motivo: 'fallback_sem_ancestral', tag: tag };
                }
                return { ok: false, motivo: 'loop_sem_candidato' };
            }
            """,
            botao_lance,
        )
        _log(
            f"C2 JS resultado={resultado_js} "
            f"elapsed={time.perf_counter()-t0:.2f}s "
            f"t_c2={time.perf_counter()-t_c2:.2f}s"
        )
        if resultado_js and resultado_js.get("ok"):
            return
    except Exception as e_js:
        _log(f"C2 excecao={e_js} elapsed={time.perf_counter()-t0:.2f}s")

    # ── CAMADA 3: get_by_text + force + clique no pai ────────────────────────
    _log("C3 iniciando get_by_text")
    t_c3 = time.perf_counter()
    try:
        el = page.get_by_text(botao_lance, exact=True).first
        el.wait_for(state="visible", timeout=2000)
        try:
            el.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            el.click(timeout=2000)
            _log(f"C3 click normal OK | elapsed={time.perf_counter()-t0:.2f}s")
            return
        except Exception as e3a:
            _log(f"C3 click normal falhou={e3a}")
        try:
            el.click(force=True)
            _log(f"C3 force click OK | elapsed={time.perf_counter()-t0:.2f}s")
            return
        except Exception as e3b:
            _log(f"C3 force click falhou={e3b}")
        try:
            page.evaluate(
                "el => { el.parentElement && el.parentElement.click(); }",
                el.element_handle(timeout=1000),
            )
            _log(f"C3 clique no pai OK | elapsed={time.perf_counter()-t0:.2f}s")
            return
        except Exception as e3c:
            _log(f"C3 clique no pai falhou={e3c}")
    except Exception as e_c3:
        _log(f"C3 excecao={e_c3} t_c3={time.perf_counter()-t_c3:.2f}s")

    elapsed = time.perf_counter() - t0
    _log(f"TODAS AS CAMADAS FALHARAM | elapsed={elapsed:.2f}s")
    raise PlaywrightTimeoutError(
        f"Tipo de lance '{botao_lance}' nao encontrado em nenhuma camada "
        f"(elapsed={elapsed:.2f}s)"
    )


def clicar_continuar(page: Page, timeout_ms: int = 5000, logger=None) -> None:
    btn = page.locator("button:has-text('Continuar')").first
    btn.wait_for(state="visible", timeout=timeout_ms)
    try:
        btn.scroll_into_view_if_needed()
    except Exception:
        pass
    _log(logger, f"[BOTAO] 'Continuar' antes do clique | {estado_elemento(btn)}")
    btn.click()
    _log(logger, "[BOTAO] 'Continuar' clicado com sucesso (click normal)")


def toast_ta_negativo_visivel(page: Page, timeout_ms: int = 6000) -> "str | None":
    """
    Verifica se o toast de TA negativo esta visivel.

    Retorna o texto completo capturado (ex: 'Percentual de TA da parcela
    ficou negativo. Cota: 000830/3161-00') ou None se nao encontrado.
    Captura o texto imediatamente ao detectar — o toast some rapido.
    """
    padroes = [
        "Percentual de TA da parcela ficou negativo",
        "TA da parcela ficou negativo",
        "parcela ficou negativo",
        "TA negativo",
    ]

    for padrao in padroes:
        try:
            el = page.get_by_text(padrao, exact=False).first
            el.wait_for(state="visible", timeout=timeout_ms)
            try:
                texto_completo = el.inner_text(timeout=500).strip()
                return texto_completo if texto_completo else padrao
            except Exception:
                return padrao
        except Exception:
            pass

    # Fallback: varre o body — cobre toasts com estrutura HTML variada
    try:
        corpo = page.locator("body").inner_text(timeout=500)
        for padrao in padroes:
            if padrao.lower() in corpo.lower():
                for linha in corpo.splitlines():
                    if padrao.lower() in linha.lower():
                        return linha.strip()
                return padrao
    except Exception:
        pass

    return None


def aguardar_botoes_pos_continuar(page: Page, timeout_ms: int = 10000) -> None:
    try:
        page.wait_for_function(
            """() => {
                const isVisible = (el) => !!(el && el.offsetParent);
                const findBtn = (txt) => Array.from(document.querySelectorAll('button'))
                    .find(b => ((b.innerText || '').trim().includes(txt)));
                return (
                    isVisible(findBtn('Comprovante')) ||
                    isVisible(findBtn('Ofertar lance')) ||
                    isVisible(findBtn('+ Nova oferta de lance'))
                );
            }""",
            timeout=timeout_ms
        )
    except Exception:
        pass


def estado_pos_continuar(page: Page) -> dict:
    btn_ofertar = page.locator("button:has-text('Ofertar lance')").first
    btn_nova = page.locator("button:has-text('+ Nova oferta de lance')").first
    btn_comprovante = page.locator("button:has-text('Comprovante')").first

    def visivel(locator) -> bool:
        try:
            return locator.is_visible()
        except Exception:
            return False

    return {
        "btn_ofertar": btn_ofertar,
        "btn_nova": btn_nova,
        "btn_comprovante": btn_comprovante,
        "ofertar_visivel": visivel(btn_ofertar),
        "nova_visivel": visivel(btn_nova),
        "comprovante_visivel": visivel(btn_comprovante),
    }


def clicar_ofertar_lance(page: Page, timeout_ms: int = 10000, logger=None) -> None:
    btn = page.locator("button:has-text('Ofertar lance')").first
    btn.wait_for(state="visible", timeout=timeout_ms)

    try:
        btn.scroll_into_view_if_needed()
    except Exception:
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass

    _log(logger, f"[BOTAO] 'Ofertar lance' antes do clique | {estado_elemento(btn)}")
    try:
        btn.click()
        _log(logger, "[BOTAO] 'Ofertar lance' clicado com sucesso (click normal)")
    except Exception as e_click:
        _log(logger, f"[BOTAO] Click normal em 'Ofertar lance' falhou ({e_click}) — tentando force click")
        btn.click(force=True)
        _log(logger, "[BOTAO] 'Ofertar lance' clicado com sucesso (force click)")


def toast_sucesso_visivel(page: Page, timeout_ms: int = 8000) -> bool:
    try:
        toast = page.locator("text=/Lance.*sucesso/i").first
        toast.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def garantir_tela_busca(page: Page, timeout_ms: int = 30000, logger=None) -> None:
    """
    Garante que a tela de busca (input[name='group']) esta visivel.

    Estrategia em 3 fases:
      1) Aguarda ate timeout_ms o input aparecer diretamente (pagina ja' esta se
         carregando ou transicionando — da' tempo para ela terminar).
      2) Se nao apareceu, tenta clicar 'Voltar' ate 3 vezes com espera entre tentativas.
      3) Em ultimo caso, clica em 'Ofertar Lance' no menu lateral para navegar
         diretamente para a pagina de busca.
    """
    # FASE 1: espera direta — cobre transicoes lentas sem nenhuma acao extra
    _log(logger, f"[BUSCA] FASE 1: aguardando input[name='group'] aparecer | timeout_ms={timeout_ms}")
    try:
        page.locator("input[name='group']").first.wait_for(
            state="visible", timeout=timeout_ms
        )
        _log(logger, "[BUSCA] FASE 1 OK: tela de busca visivel sem acao extra")
        return
    except Exception:
        _log(logger, "[BUSCA] FASE 1 expirou — partindo para FASE 2 (clicar 'Voltar' ate 3x)")

    # FASE 2: tenta clicar 'Voltar' ate 3 vezes, esperando 5s entre tentativas.
    # "Voltar" pode ser <button> (tela de tipo de lance) ou <a>/<span> (outras telas).
    _SELETORES_VOLTAR = [
        "button:has-text('Voltar')",
        "a:has-text('Voltar')",
        "span:has-text('Voltar')",
        "text=Voltar",
    ]
    for _tent_voltar in range(1, 4):
        for sel_v in _SELETORES_VOLTAR:
            try:
                el_v = page.locator(sel_v).first
                if el_v.is_visible():
                    _log(logger, f"[BUSCA] FASE 2 tentativa {_tent_voltar}/3: clicando 'Voltar' | seletor={sel_v!r} | {estado_elemento(el_v)}")
                    el_v.click()
                    break
            except Exception:
                continue

        try:
            page.locator("input[name='group']").first.wait_for(
                state="visible", timeout=5000
            )
            _log(logger, f"[BUSCA] FASE 2 OK: tela de busca visivel apos 'Voltar' (tentativa {_tent_voltar}/3)")
            return
        except Exception:
            pass

    # FASE 3: navega direto pelo menu 'Ofertar Lance'
    _log(logger, "[BUSCA] FASE 2 esgotada — FASE 3: navegando pelo menu lateral 'Ofertar Lance'")
    try:
        page.locator("text=Ofertar Lance").first.wait_for(state="visible", timeout=10000)
        page.locator("text=Ofertar Lance").first.click()
        page.locator("input[name='group']").first.wait_for(
            state="visible", timeout=15000
        )
        _log(logger, "[BUSCA] FASE 3 OK: tela de busca visivel apos navegar pelo menu")
    except Exception as e_f3:
        _log(logger, f"[BUSCA] FASE 3 falhou: {e_f3} — tela de busca NAO confirmada")



def clicar_nova_oferta(page: Page, logger=None) -> bool:
    return clicar_botao_por_texto(page, "+ Nova oferta de lance", timeout_ms=5000, logger=logger)

def obter_texto_toast(page: Page, timeout_ms: int = 2000) -> str | None:
    try:
        toast = page.locator("div[role='alert'], .Toastify__toast, .toast").first
        toast.wait_for(state="visible", timeout=timeout_ms)

        texto = toast.inner_text().strip()
        return texto if texto else None

    except Exception:
        return None


def clicar_alterar_cota_se_visivel(page: Page, timeout_ms: int = 3000, logger=None) -> bool:
    """
    Clica em 'Alterar cota' que pode ser <a>, <span> ou <button> dependendo
    da tela. Tenta seletores do mais especifico ao mais amplo.
    """
    seletores = [
        "button:has-text('Alterar cota')",
        "a:has-text('Alterar cota')",
        "span:has-text('Alterar cota')",
        "text=Alterar cota",
    ]
    for sel in seletores:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=timeout_ms)
            try:
                el.scroll_into_view_if_needed()
            except Exception:
                pass
            _log(logger, f"[BOTAO] 'Alterar cota' encontrado | seletor={sel!r} | {estado_elemento(el)}")
            try:
                el.click(timeout=2000)
                _log(logger, f"[BOTAO] 'Alterar cota' clicado com sucesso (click normal) | seletor={sel!r}")
                return True
            except Exception as e_click:
                _log(logger, f"[BOTAO] 'Alterar cota' click normal falhou ({e_click}) — tentando JS click")
                try:
                    handle = el.element_handle(timeout=1000)
                    if handle:
                        page.evaluate("el => el.click()", handle)
                        _log(logger, f"[BOTAO] 'Alterar cota' clicado com sucesso (JS click) | seletor={sel!r}")
                        return True
                except Exception as e_js:
                    _log(logger, f"[BOTAO] 'Alterar cota' JS click tambem falhou: {e_js}")
        except Exception:
            continue
    _log(logger, f"[BOTAO] 'Alterar cota' nao visivel em nenhum dos {len(seletores)} seletores (ok se a tela nao exibe esse link)")
    return False