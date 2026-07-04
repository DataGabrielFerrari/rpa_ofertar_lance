import os
import sys
import inspect
from datetime import datetime
from typing import Optional


def _obter_origem() -> str:
    """
    Descobre o arquivo:linha que chamou o logger (pulando o proprio log.py).
    Ex: 'processamento/worker_avapro.py:512'
    """
    try:
        stack = inspect.stack()
        frame = None
        for item in stack:
            caminho = item.filename.replace("\\", "/")
            if not caminho.endswith("shared/log.py"):
                frame = item
                break
        if frame is None:
            return "?"

        caminho_completo = frame.filename.replace("\\", "/")
        if "/rpa_ofertar_lance/" in caminho_completo:
            caminho_rel = caminho_completo.split("/rpa_ofertar_lance/", 1)[1]
        else:
            caminho_rel = os.path.basename(caminho_completo)

        return f"{caminho_rel}:{frame.lineno}"
    except Exception:
        return "?"


class Logger:
    """
    Logger de arquivo texto do RPA Ofertar Lance.

    Formato de cada linha:
        AAAA-MM-DD HH:MM:SS.mmm | NIVEL | origem(arquivo:linha) | mensagem

    - Toda linha e espelhada no stderr (acompanhamento em tempo real pelo
      PAD/PowerShell) e gravada no arquivo .txt configurado.
    - Falha ao escrever no arquivo NUNCA derruba o fluxo do robo.
    """

    def __init__(self, caminho_arquivo: Optional[str] = None):
        self.caminho_arquivo = caminho_arquivo

    def configurar_arquivo(self, caminho_arquivo: str, cabecalho: Optional[str] = None) -> None:
        self.caminho_arquivo = caminho_arquivo

        pasta = os.path.dirname(caminho_arquivo)
        if pasta:
            os.makedirs(pasta, exist_ok=True)

        if not os.path.exists(caminho_arquivo):
            with open(caminho_arquivo, "w", encoding="utf-8") as f:
                if cabecalho:
                    ts = self._agora()
                    f.write(f"{ts} | INFO  | {_obter_origem()} | {cabecalho}\n")

        # Registra explicitamente ONDE o log esta sendo gravado, para que o
        # proprio arquivo documente seu caminho completo.
        self.info(f"[LOG] Arquivo de log configurado | caminho_completo={os.path.abspath(caminho_arquivo)}")

    def _agora(self) -> str:
        # Milissegundos para ordenar eventos proximos (cliques rapidos, toasts)
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def _escrever(self, nivel: str, mensagem: str) -> None:
        linha = f"{self._agora()} | {nivel:<5} | {_obter_origem()} | {mensagem}"

        # Espelho em stderr: acompanhamento em tempo real (PAD captura stdout,
        # entao stderr e seguro e nao polui o JSON de saida).
        try:
            print(linha, file=sys.stderr, flush=True)
        except Exception:
            pass

        if self.caminho_arquivo:
            try:
                pasta = os.path.dirname(self.caminho_arquivo)
                if pasta:
                    os.makedirs(pasta, exist_ok=True)

                with open(self.caminho_arquivo, "a", encoding="utf-8") as f:
                    f.write(linha + "\n")
            except Exception as e:
                # Log nao pode quebrar o robo: apenas avisa no stderr.
                try:
                    print(f"[ERRO LOG] Falha ao escrever em '{self.caminho_arquivo}': {e}",
                          file=sys.stderr, flush=True)
                except Exception:
                    pass

    def info(self, mensagem: str) -> None:
        self._escrever("INFO", mensagem)

    def warn(self, mensagem: str) -> None:
        self._escrever("WARN", mensagem)

    def warning(self, mensagem: str) -> None:
        self.warn(mensagem)

    def error(self, mensagem: str) -> None:
        self._escrever("ERROR", mensagem)

    def click(self, mensagem: str) -> None:
        self._escrever("CLICK", mensagem)

    def debug(self, mensagem: str) -> None:
        self._escrever("DEBUG", mensagem)
