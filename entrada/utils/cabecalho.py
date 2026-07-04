from typing import Dict, List, Optional, Tuple


def mapear_indices_cabecalho(cabecalho: List[str]) -> Dict[str, Optional[int]]:
    cabecalho_limpo = [str(h).strip() if h is not None else "" for h in cabecalho]

    def achar_exato(nome: str) -> Optional[int]:
        if nome in cabecalho_limpo:
            return cabecalho_limpo.index(nome)
        return None

    idx_grupo = achar_exato("GRUPO")
    idx_cota = achar_exato("COTA")
    idx_status_lance = achar_exato("LANCE")
    idx_cliente = achar_exato("NOME DO CLIENTE")
    idx_obs_lance = achar_exato("OBSERVAÇÃO LANCE")

    # A coluna CONSULTOR (ou NOME DA PASTA) e obrigatoria EXISTIR no cabecalho,
    # mas os valores podem ficar vazios. Aceita qualquer um dos nomes.
    # Obs: checagem explicita de None porque o indice 0 e' uma posicao valida
    # (um simples `a or b` trataria 0 como ausente).
    idx_consultor = None
    for _nome_col in ("CONSULTOR", "NOME DA PASTA", "NOME_DA_PASTA", "PASTA"):
        _pos = achar_exato(_nome_col)
        if _pos is not None:
            idx_consultor = _pos
            break

    faltando = []

    if idx_grupo is None:
        faltando.append("GRUPO")

    if idx_cota is None:
        faltando.append("COTA")

    if idx_status_lance is None:
        faltando.append("LANCE")

    if idx_cliente is None:
        faltando.append("NOME DO CLIENTE")

    if idx_obs_lance is None:
        faltando.append("OBSERVAÇÃO LANCE")

    if idx_consultor is None:
        faltando.append("CONSULTOR / NOME DA PASTA")

    if faltando:
        raise ValueError(
            f"Faltando colunas obrigatórias: {', '.join(faltando)}. "
            f"Esperado: GRUPO, COTA, LANCE, NOME DO CLIENTE, OBSERVAÇÃO LANCE, "
            f"CONSULTOR/NOME DA PASTA. "
            f"Recebido: {cabecalho}"
        )

    return {
        "GRUPO": idx_grupo,
        "COTA": idx_cota,
        "LANCE": idx_status_lance,
        "NOME DO CLIENTE": idx_cliente,
        "OBSERVAÇÃO LANCE": idx_obs_lance,
        "CONSULTOR": idx_consultor,  # opcional
    }


def encontrar_cabecalho(
    linhas: List[List[str]],
    max_linhas_busca: int = 10
) -> Tuple[int, Dict[str, Optional[int]]]:
    limite = min(len(linhas), max_linhas_busca)

    for i in range(limite):
        linha = linhas[i]

        if not linha:
            continue

        if not any(str(c or "").strip() for c in linha):
            continue

        try:
            indices = mapear_indices_cabecalho(linha)
            return i, indices
        except ValueError:
            continue

    raise ValueError(
        f"Nenhum cabeçalho válido encontrado nas primeiras {limite} linhas."
    )