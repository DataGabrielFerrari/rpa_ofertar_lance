import re
from typing import Any, Iterable


def extrair_id_planilha(link: str) -> str:
    if not link:
        raise ValueError("link_planilha está vazio.")

    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", link)
    if not match:
        raise ValueError(f"Não foi possível extrair o ID da planilha do link: {link}")

    return match.group(1)


def ler_range(service, spreadsheet_id: str, range_a1: str) -> list[list[str]]:
    response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_a1,
        majorDimension="ROWS"
    ).execute()

    return response.get("values", [])


def escapar_nome_aba(nome_aba: str) -> str:
    nome_aba = str(nome_aba or "").strip()
    if not nome_aba:
        raise ValueError("Nome da aba está vazio.")

    nome_aba = nome_aba.replace("'", "''")
    return f"'{nome_aba}'"


def coluna_para_letra(idx_zero_based: int) -> str:
    if idx_zero_based < 0:
        raise ValueError("O índice da coluna não pode ser negativo.")

    idx = idx_zero_based + 1
    letras = ""

    while idx > 0:
        idx, resto = divmod(idx - 1, 26)
        letras = chr(65 + resto) + letras

    return letras


def atualizar_coluna_em_lote(
    service,
    spreadsheet_id: str,
    aba: str,
    letra_coluna: str,
    linhas: Iterable[int],
    valor: Any
) -> None:
    linhas = list(linhas)
    if not linhas:
        return

    aba_segura = escapar_nome_aba(aba)

    data = []
    for row_num in linhas:
        rng = f"{aba_segura}!{letra_coluna}{row_num}"
        data.append({
            "range": rng,
            "values": [[valor]]
        })

    body = {
        "valueInputOption": "RAW",
        "data": data
    }

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body=body
    ).execute()


def atualizar_multiplas_celulas(
    service,
    spreadsheet_id: str,
    atualizacoes: list[dict]
) -> None:
    if not atualizacoes:
        return

    data = []
    for item in atualizacoes:
        aba = escapar_nome_aba(item["aba"])
        coluna = item["coluna"]
        linha = item["linha"]
        valor = item["valor"]

        data.append({
            "range": f"{aba}!{coluna}{linha}",
            "values": [[valor]]
        })

    body = {
        "valueInputOption": "RAW",
        "data": data
    }

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body=body
    ).execute()