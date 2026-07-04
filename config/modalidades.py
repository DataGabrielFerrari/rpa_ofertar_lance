from dataclasses import dataclass


@dataclass(frozen=True)
class ModalidadeConfig:
    modalidade: str
    subpasta_lotes: str
    botao_lance: str


CONFIG_MODALIDADES = {
    "IMOVEL": ModalidadeConfig(
        modalidade="IMOVEL",
        subpasta_lotes="imovel",
        botao_lance="Lance Fixo",
    ),
    "MOTORS": ModalidadeConfig(
        modalidade="MOTORS",
        subpasta_lotes="motors",
        botao_lance="Segundo Lance Fixo",
    ),
}


def obter_config_modalidade(modalidade: str) -> ModalidadeConfig:
    modalidade = (modalidade or "").strip().upper()

    if modalidade not in CONFIG_MODALIDADES:
        raise ValueError(f"Modalidade inválida: {modalidade}. Use IMOVEL ou MOTORS.")

    return CONFIG_MODALIDADES[modalidade]