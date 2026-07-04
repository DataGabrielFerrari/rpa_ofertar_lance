import os
import time
from contextlib import contextmanager

import psycopg2


def get_conn():
    # statement_timeout: mata qualquer query que demore mais de 90s
    # lock_timeout: mata qualquer espera de lock apos 30s
    # Sem esses parametros, uma query travada bloqueia o Python para sempre,
    # deixando o PowerShell e o PAD presos em loop silencioso infinito.
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        sslmode="require",
        connect_timeout=15,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
        # timezone: todo NOW()/CURRENT_TIMESTAMP desta conexao resolve em
        # horario do Brasil, independente do fuso do servidor ou da maquina
        # (VM/local) que executa o RPA.
        options="-c statement_timeout=90000 -c lock_timeout=30000 "
                "-c timezone=America/Sao_Paulo",
    )


@contextmanager
def get_cursor():
    conn = None
    cur = None

    try:
        conn = get_conn()
        cur = conn.cursor()
        yield conn, cur
        conn.commit()

    except Exception:
        if conn is not None and conn.closed == 0:
            try:
                conn.rollback()
            except Exception:
                pass
        raise

    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass

        if conn is not None and conn.closed == 0:
            try:
                conn.close()
            except Exception:
                pass


def execute(sql, params=None, tentativas=5, espera_s=2):
    ultimo_erro = None

    for tentativa in range(1, tentativas + 1):
        try:
            with get_cursor() as (_, cur):
                cur.execute(sql, params or ())
            return
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            ultimo_erro = e
            if tentativa == tentativas:
                raise
            time.sleep(espera_s)

    raise ultimo_erro


def fetchone(sql, params=None, tentativas=5, espera_s=2):
    ultimo_erro = None

    for tentativa in range(1, tentativas + 1):
        try:
            with get_cursor() as (_, cur):
                cur.execute(sql, params or ())
                return cur.fetchone()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            ultimo_erro = e
            if tentativa == tentativas:
                raise
            time.sleep(espera_s)

    raise ultimo_erro


def fetchall(sql, params=None, tentativas=5, espera_s=2):
    ultimo_erro = None

    for tentativa in range(1, tentativas + 1):
        try:
            with get_cursor() as (_, cur):
                cur.execute(sql, params or ())
                return cur.fetchall()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            ultimo_erro = e
            if tentativa == tentativas:
                raise
            time.sleep(espera_s)

    raise ultimo_erro