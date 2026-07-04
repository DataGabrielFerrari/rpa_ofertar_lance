"""
ENTRADA / Orquestrador chamado pelo PAD 1x por execucao.

Recebe MODALIDADE em argv[1] (MOTORS|IMOVEL).

Saida (stdout): JSON unica linha
{
  "sucesso": bool,
  "id_fila_adm": int|null,
  "status": "PROCESSANDO|SEM_FILA|FALHA|...",
  "observacao": str,
  "caminho_log": str|null
}
"""

import os
import sys
import io
import argparse
import json
import traceback
from contextlib import redirect_stdout

# Forca stdout/stderr em UTF-8 para evitar mojibake quando o PAD
# captura a saida via PowerShell (default do Windows e cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))   # ...\entrada
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)                # ...\rpa_ofertar_lance

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)
except Exception:
    pass

from config.modalidades import obter_config_modalidade

from db.db import fetchone as _db_fetchone, execute as _db_execute
from db.funcoes import (
    reservar_lote_interrompido,
    reservar_proximo_adm_e_criar_fila,
    marcar_lotes_parados_como_falha,
    obter_dados_adm_por_fila,
    atualizar_caminhos_fila_adm,
    atualizar_total_cotas_fila_adm,
    finalizar_fila_adm,
    obter_pasta_base,
)
from entrada.leitor_planilha import ler_planilhas

from shared.log import Logger
from shared.notificador import notificar_falha
from shared.pastas import criar_estrutura_lote


# =========================================================
# HELPERS
# =========================================================

def _contar_pendentes(id_fila_adm: int) -> int:
    """Retorna quantas cotas ainda estao com status PENDENTE no lote."""
    try:
        row = _db_fetchone(
            "SELECT COUNT(*) FROM tbl_fila_cotas WHERE id_fila_adm = %s AND status = 'PENDENTE'",
            (id_fila_adm,),
        )
        return int(row[0]) if row else 0
    except Exception:
        return -1  # -1 indica que nao foi possivel contar


def _campo(row, idx: int, chave: str, default=None):
    if row is None:
        return default

    if hasattr(row, "keys"):
        return row.get(chave, default)

    try:
        return row[idx]
    except Exception:
        return default


def _payload(
    sucesso: bool,
    id_fila_adm,
    status: str,
    observacao: str,
    caminho_log=None,
) -> dict:
    return {
        "sucesso": sucesso,
        "id_fila_adm": id_fila_adm,
        "status": status,
        "observacao": observacao,
        "caminho_log": caminho_log,
    }


def _payload_falha(observacao: str, id_fila_adm=None, caminho_log=None) -> dict:
    return _payload(
        sucesso=False,
        id_fila_adm=id_fila_adm,
        status="FALHA",
        observacao=observacao,
        caminho_log=caminho_log,
    )


def _emitir_json(stdout_original, payload: dict) -> None:
    stdout_original.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    stdout_original.flush()


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _desativar_flag_reexecucao(id_adm, modalidade: str, logger: Logger) -> None:
    """
    Desativa reexecucao_motors/reexecucao_imovel no tbl_adm.

    Chamada quando um lote de REEXECUCAO termina SEM_COTAS (nenhuma linha
    'REEXECUTAR' restante na planilha). Sem isso, a flag fica TRUE para
    sempre e a proxima entrada reserva o MESMO adm em modo reexecucao de
    novo, gerando lotes vazios (SEM_COTAS) em loop.
    """
    try:
        if not id_adm:
            return
        coluna = {
            "MOTORS": "reexecucao_motors",
            "IMOVEL": "reexecucao_imovel",
        }.get((modalidade or "").strip().upper())
        if not coluna:
            return
        _db_execute(
            f"UPDATE tbl_adm SET {coluna} = FALSE WHERE id_adm = %s",
            (int(id_adm),),
        )
        logger.info(
            f"[ENTRADA] Flag {coluna} desativada | id_adm={id_adm} "
            f"(reexecucao sem cotas REEXECUTAR na planilha)"
        )
    except Exception as e:
        logger.warn(f"[ENTRADA] Falha ao desativar flag reexecucao: {e}")


def _obter_dados_lote(id_fila_adm: int) -> dict:
    dados = obter_dados_adm_por_fila(id_fila_adm)

    if not dados:
        raise ValueError(f"Lote nao encontrado: id_fila_adm={id_fila_adm}")

    return {
        "id_fila_adm": _campo(dados, 0, "id_fila_adm"),
        "id_adm": _campo(dados, 1, "id_adm"),
        "nome": _campo(dados, 2, "nome"),
        "link_planilha": _campo(dados, 3, "link_planilha"),
        "nome_aba": _campo(dados, 4, "nome_aba"),
        "reexecucao": _campo(dados, 5, "reexecucao"),
        "modalidade_lote": _campo(dados, 6, "modalidade"),
        "ultima_execucao": _campo(dados, 7, "ultima_execucao"),
    }


def _preparar_estrutura_lote(
    id_fila_adm: int,
    modalidade: str,
    base_dir: str,
    logger: Logger,
):
    dados_lote = _obter_dados_lote(id_fila_adm)

    caminhos = criar_estrutura_lote(
        base_dir=base_dir,
        nome_adm=str(dados_lote["nome"] or "").strip(),
        id_adm=int(dados_lote["id_adm"]),
        id_fila_adm=int(id_fila_adm),
        modalidade=modalidade,
    )

    atualizar_caminhos_fila_adm(
        id_fila_adm=id_fila_adm,
        caminho_base=caminhos["raiz"],
        caminho_log=caminhos["log"],
    )

    logger.configurar_arquivo(
        caminhos["log"],
        cabecalho=(
            f"ETAPA=ENTRADA "
            f"id_fila_adm={id_fila_adm} "
            f"id_adm={dados_lote['id_adm']} "
            f"nome='{dados_lote['nome']}' "
            f"modalidade={modalidade}"
        ),
    )

    return dados_lote, caminhos


# =========================================================
# EXECUCAO
# =========================================================

def _detectar_drive_base() -> str | None:
    """
    Detecta a pasta do Google Drive onde fica o container 'lotes_lance'.
    Retorna o diretorio PAI (o container 'lotes_lance' e criado dentro dele
    por criar_estrutura_lote). Retorna None se nao encontrar nenhum Drive.

    O Drive monta como:
      - espelho:   C:\\Users\\<user>\\My Drive  (ou "Meu Drive" em PT-BR)
      - streaming: G:\\My Drive  /  G:\\Meu Drive  (a letra pode variar)
    """
    nomes_drive = ("My Drive", "Meu Drive")
    usuario = os.environ.get("USERNAME") or "adminrpa"

    candidatos = []
    for nome in nomes_drive:
        candidatos.append(os.path.join("C:\\", "Users", usuario, nome))
    for letra in ("G", "H", "I", "J", "K", "L", "M"):
        for nome in nomes_drive:
            candidatos.append(f"{letra}:\\{nome}")

    for base_drive in candidatos:
        if os.path.isdir(base_drive):
            return os.path.abspath(base_drive)
    return None


def _get_base_dir() -> str:
    """
    Raiz (PAI) onde o container 'lotes_lance' e criado. Precedencia:
      1) parametro 'pasta_base' no banco (se definido)
      2) variavel de ambiente LOTES_BASE (.env)
      3) Google Drive detectado automaticamente
      4) fallback: raiz do projeto
    Estrutura final: {base_dir}\\lotes_lance\\{motors|imovel}\\{ADM}_{id}\\fila_{id}
    """
    db_base = (obter_pasta_base() or "").strip()
    if db_base:
        return db_base

    env_base = (os.environ.get("LOTES_BASE") or "").strip()
    if env_base:
        return os.path.abspath(env_base)

    drive_base = _detectar_drive_base()
    if drive_base:
        return drive_base

    return PROJECT_ROOT


def _executar(modalidade: str, worker_nome: str, logger: Logger) -> dict:
    base_dir = _get_base_dir()

    logger.info(
        f"[ENTRADA] Iniciando | modalidade={modalidade} worker={worker_nome}"
    )

    # 1) Marca lotes parados como FALHA (pode haver mais de um)
    lotes_parados = marcar_lotes_parados_como_falha(minutos=10) or []

    if lotes_parados:
        for lote_parado in lotes_parados:
            id_parado = int(_campo(lote_parado, 0, "id_fila_adm"))
            obs_parado = str(
                _campo(lote_parado, 5, "observacao")
                or "LOTE PARADO MAIS DE 10 MINUTOS"
            ).strip()
            logger.warn(
                f"[ENTRADA] Lote parado marcado como FALHA | "
                f"id_fila_adm={id_parado} obs={obs_parado}"
            )

        # Mantem comportamento original: aborta com FALHA usando o primeiro
        primeiro = lotes_parados[0]
        return _payload(
            sucesso=False,
            id_fila_adm=int(_campo(primeiro, 0, "id_fila_adm")),
            status="FALHA",
            observacao=str(
                _campo(primeiro, 5, "observacao")
                or "LOTE PARADO MAIS DE 10 MINUTOS"
            ).strip(),
        )

    # 2) Tenta retomar lote interrompido
    lote_interrompido = reservar_lote_interrompido(modalidade, worker_nome)

    if lote_interrompido:
        id_fila_adm = int(_campo(lote_interrompido, 0, "id_fila_adm"))
        status_lote = str(_campo(lote_interrompido, 1, "status") or "").strip()

        dados_lote, caminhos = _preparar_estrutura_lote(
            id_fila_adm=id_fila_adm,
            modalidade=modalidade,
            base_dir=base_dir,
            logger=logger,
        )
        n_pendentes = _contar_pendentes(id_fila_adm)
        obs_pendentes = (
            f"{n_pendentes} cotas pendentes" if n_pendentes >= 0
            else "pendentes: nao foi possivel contar"
        )
        logger.info(
            f"[ENTRADA] Lote PENDENTE reservado para retomada | "
            f"id_fila_adm={id_fila_adm} | {obs_pendentes}"
        )
        logger.info(
            f"[PENDENTES] Cotas pendentes ao RETOMAR o lote | "
            f"id_fila_adm={id_fila_adm} | quantidade={n_pendentes if n_pendentes >= 0 else 'nao foi possivel contar'}"
        )

        return _payload(
            sucesso=True,
            id_fila_adm=id_fila_adm,
            status=status_lote,
            observacao=f"Lote retomado apos interrupcao. {obs_pendentes}.",
            caminho_log=caminhos["log"],
        )

    # 3) Cria novo lote
    lote = reservar_proximo_adm_e_criar_fila(modalidade, worker_nome)

    if not lote:
        logger.info(
            f"[ENTRADA] Nenhum ADM elegivel encontrado para modalidade={modalidade}"
        )
        return _payload(
            sucesso=True,
            id_fila_adm=None,
            status="SEM_FILA",
            observacao="Nenhum ADM elegivel encontrado.",
        )

    id_fila_adm = int(_campo(lote, 0, "id_fila_adm"))
    nome_adm = str(_campo(lote, 2, "nome") or "").strip()

    logger.info(
        f"[ENTRADA] Lote reservado/criado | "
        f"id_fila_adm={id_fila_adm} nome={nome_adm}"
    )

    dados_lote, caminhos = _preparar_estrutura_lote(
        id_fila_adm=id_fila_adm,
        modalidade=modalidade,
        base_dir=base_dir,
        logger=logger,
    )

    total_lidas = int(
        ler_planilhas(
            id_fila_adm=id_fila_adm,
            modalidade_execucao=modalidade,
            logger=logger,
        ) or 0
    )

    atualizar_total_cotas_fila_adm(id_fila_adm, total_lidas)

    logger.info(
        f"[ENTRADA] Leitura concluida | "
        f"id_fila_adm={id_fila_adm} total_cotas={total_lidas}"
    )

    if total_lidas == 0:
        obs_sem_cotas = "Nenhuma cota pendente encontrada para este administrador."
        logger.warn(f"[ENTRADA] SEM_COTAS | id_fila_adm={id_fila_adm} | {obs_sem_cotas}")
        # Lote de reexecucao sem nenhuma cota REEXECUTAR restante:
        # desativa a flag para nao reservar o mesmo ADM em loop.
        if dados_lote.get("reexecucao"):
            _desativar_flag_reexecucao(
                dados_lote.get("id_adm"), modalidade, logger
            )
        # Banco aceita apenas SUCESSO|FALHA via finalizar_fila_adm (CHECK
        # constraint na tabela). SEM_COTAS e' um status logico que vai apenas
        # no payload pro PAD - no banco fica como SUCESSO ja que nao houve
        # erro real, so' nao havia trabalho a fazer.
        try:
            finalizar_fila_adm(id_fila_adm, "SUCESSO", obs_sem_cotas)
        except Exception as e_fin:
            logger.warn(f"[ENTRADA] Nao foi possivel finalizar lote com SUCESSO/SEM_COTAS: {e_fin}")
        return _payload(
            sucesso=True,
            id_fila_adm=id_fila_adm,
            status="SEM_COTAS",
            observacao=obs_sem_cotas,
            caminho_log=caminhos["log"],
        )

    n_pendentes = _contar_pendentes(id_fila_adm)
    obs_pendentes = (
        f"{n_pendentes} pendentes" if n_pendentes >= 0
        else "pendentes: nao foi possivel contar"
    )
    logger.info(
        f"[ENTRADA] Lote criado | id_fila_adm={id_fila_adm} "
        f"total={total_lidas} {obs_pendentes}"
    )
    logger.info(
        f"[PENDENTES] Cotas pendentes ao INICIAR o lote | "
        f"id_fila_adm={id_fila_adm} | quantidade={n_pendentes if n_pendentes >= 0 else 'nao foi possivel contar'} "
        f"| total_lidas_planilha={total_lidas}"
    )

    return _payload(
        sucesso=True,
        id_fila_adm=id_fila_adm,
        status="PROCESSANDO",
        observacao=f"Lote iniciado para {nome_adm} com {total_lidas} cotas ({obs_pendentes}).",
        caminho_log=caminhos["log"],
    )


# =========================================================
# MAIN
# =========================================================

def main() -> int:
    stdout_original = sys.stdout

    # Timeout global de socket: cobre chamadas ao Google Sheets API (httplib2)
    # e qualquer outro IO de rede que nao tenha timeout proprio configurado.
    # Sem isso, ler_range() pode travar para sempre em caso de falha de rede,
    # deixando o PowerShell e o PAD em loop silencioso infinito.
    import socket as _socket
    _socket.setdefaulttimeout(120)  # 2 minutos maximo por operacao de rede

    parser = argparse.ArgumentParser(description="Entrada do RPA Ofertar Lance")
    parser.add_argument(
        "modalidade",
        choices=["MOTORS", "IMOVEL"],
        help="Modalidade de execucao",
    )
    parser.add_argument(
        "--worker",
        default=os.environ.get("COMPUTERNAME", "worker-local"),
        help="Nome da maquina/worker",
    )

    try:
        args = parser.parse_args()
    except SystemExit as se:
        # argparse ja escreveu mensagem de erro - emitir JSON e sair
        _emitir_json(
            stdout_original,
            _payload_falha(
                f"Argumentos invalidos (codigo argparse={se.code})"
            ),
        )
        return int(se.code) if isinstance(se.code, int) else 1

    modalidade = args.modalidade.strip().upper()
    worker_nome = str(args.worker or "").strip()

    try:
        _ = obter_config_modalidade(modalidade)
    except Exception as e:
        _emitir_json(
            stdout_original,
            _payload_falha(f"Modalidade invalida: {e}"),
        )
        return 1

    logger = Logger()
    saida_final = None
    codigo_saida = 0

    # Captura tudo que tentar imprimir em stdout durante o execute
    # para nao poluir o JSON final que vai pro PAD.
    with redirect_stdout(io.StringIO()):
        try:
            saida_final = _executar(modalidade, worker_nome, logger)
        except Exception as e:
            _stderr(traceback.format_exc())
            logger.error(f"[ENTRADA] Falha geral | erro={e}")
            try:
                logger.error(traceback.format_exc())
            except Exception:
                pass

            try:
                notificar_falha(
                    etapa="ENTRADA",
                    erro=e,
                    id_fila_adm=None,
                    caminho_log=None,
                    script_path=__file__,
                    contexto_extra=f"modalidade={modalidade} worker={worker_nome}",
                )
            except Exception:
                pass

            saida_final = _payload_falha(f"{type(e).__name__}: {e}")
            codigo_saida = 1

    payload_final = saida_final or _payload_falha("saida vazia")
    _emitir_json(stdout_original, payload_final)

    # CRITICO: se sucesso=False, sair com exit code 1 para que o PAD
    # capture o erro via "Bloco de erro" (ON BLOCK ERROR).
    # Sem isso, status=FALHA sai com codigo 0 — PAD nao lanca excecao,
    # ErroEntrada fica vazio, e o loop continua silenciosamente para sempre.
    if not payload_final.get("sucesso", True):
        codigo_saida = 1

    return codigo_saida


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        _stderr(traceback.format_exc())
        try:
            sys.stdout.write(json.dumps(
                _payload_falha(f"Excecao toplevel: {type(e).__name__}: {e}"),
                ensure_ascii=False,
            ) + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        sys.exit(1)
