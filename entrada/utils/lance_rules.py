"""
Regras de classificacao do status de LANCE lido na planilha do ADM.

Toda comparacao e feita sobre uma versao NORMALIZADA do status:
  - upper case
  - sem acentos      (NÃO  -> NAO,  OFERTADÍSSIMO -> OFERTADISSIMO)
  - sem espacos extras (colapsa multiplos espacos em 1)
  - sem espacos no inicio/fim

Isso permite que o usuario digite na planilha em qualquer variacao:
  'Não Ofertado', 'NAO OFERTADO', 'nao  ofertado', '  NÃO OFERTADO  '
e tudo seja reconhecido como o mesmo status.
"""

import re

from entrada.utils.texto import remover_acentos


# As constantes ja estao em formato NORMALIZADO (upper + sem acento +
# espacos colapsados). Nao adicione variantes com acento aqui — a
# normalizacao do input cuida disso.
BLOQUEADOS = {
    "CONTEMPLADO",
    "CONTEMPLADA",
    "NAO PROCESSAR",
}

STATUS_NAO_OFERTADO = {
    "NAO OFERTADO",
}

STATUS_REEXECUTAR = {
    "REEXECUTAR",
}


def normalizar_status(texto: str) -> str:
    """
    Aplica a normalizacao padrao para comparacao de status.
    """
    if not texto:
        return ""
    t = str(texto).upper().strip()
    t = remover_acentos(t)
    t = re.sub(r"\s+", " ", t)
    return t


def deve_bloquear(status: str) -> bool:
    return normalizar_status(status) in BLOQUEADOS


def esta_nao_ofertado(status: str) -> bool:
    return normalizar_status(status) in STATUS_NAO_OFERTADO


def esta_reexecucao(valor: str) -> bool:
    """
    Usado quando o ADM esta em modo reexecucao.
    So processa cotas marcadas explicitamente como REEXECUTAR.
    """
    return normalizar_status(valor) in STATUS_REEXECUTAR
