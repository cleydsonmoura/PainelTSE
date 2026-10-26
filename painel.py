#!/usr/bin/env python3
"""
Painel TSE - apuracao em tempo real (presidente 2026, 2o turno)
===============================================================

Servidor local (biblioteca padrao apenas) que:

  * serve o painel web (pasta ./web);
  * faz proxy da API publica de resultados do TSE
    (https://resultados.tse.jus.br/oficial), evitando o bloqueio de CORS;
  * normaliza o JSON oficial para um formato enxuto usado pelo front-end;
  * monta em segundo plano o mapa municipal (candidato mais votado em cada
    cidade), consultando os arquivos -u.json de cada municipio;
  * cacheia respostas curtas (poucos segundos) e mantem a ultima resposta
    valida caso o TSE oscile.

Uso:
    python painel.py                # http://localhost:8787
    python painel.py --port 9000    # outra porta
    python painel.py --host 0.0.0.0 # aceitar acesso pela rede local
    python painel.py --no-browser   # nao abre o navegador
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# --------------------------------------------------------------------------- #
# Configuracao
# --------------------------------------------------------------------------- #

TSE_BASE = "https://resultados.tse.jus.br/oficial"
RAIZ = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(RAIZ, "web")
PORTA_PADRAO = int(os.environ.get("PORT", "8787"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 PainelTSE/1.0"
)

# Tempo (segundos) que cada tipo de resposta fica em cache no servidor.
TTL_CONFIG = 600.0      # catalogo de eleicoes
TTL_RESULTADO = 3.0     # dados do cargo (votos)  -> consulta quase em tempo real
TTL_ACOMPANHAMENTO = 5.0
TTL_MUNICIPIOS = 1800.0  # lista de municipios
TTL_FOTO = 86400.0      # fotos dos candidatos

# Mapa municipal: cada prefeitura/cidade precisa do seu proprio arquivo -u.json.
# A varredura roda em segundo plano, com conexoes keep-alive e em ondas.
TTL_MAPA = 60.0         # validade de um municipio dentro da mesma apuracao
MAPA_WORKERS = 20       # requisicoes simultaneas ao TSE
MAPA_PAUSA = 30.0       # pausa entre ondas enquanto a apuracao corre
MAPA_PAUSA_FINAL = 600.0

UF_NOMES = {
    "ac": "Acre", "al": "Alagoas", "am": "Amazonas", "ap": "Amapá",
    "ba": "Bahia", "ce": "Ceará", "df": "Distrito Federal",
    "es": "Espírito Santo", "go": "Goiás", "ma": "Maranhão",
    "mg": "Minas Gerais", "ms": "Mato Grosso do Sul", "mt": "Mato Grosso",
    "pa": "Pará", "pb": "Paraíba", "pe": "Pernambuco", "pi": "Piauí",
    "pr": "Paraná", "rj": "Rio de Janeiro", "rn": "Rio Grande do Norte",
    "ro": "Rondônia", "rr": "Roraima", "rs": "Rio Grande do Sul",
    "sc": "Santa Catarina", "se": "Sergipe", "sp": "São Paulo", "to": "Tocantins",
}
UF_NOMES["br"] = "Brasil"

TIPOS_ELEICAO = {1: "estadual", 3: "municipal", 4: "suplementar", 8: "federal"}

# Este painel acompanha exclusivamente a eleicao presidencial de 2026 (2o turno).
PLEITO_ALVO = {"tipo": "federal", "ano": "2026", "turno": 2}


# --------------------------------------------------------------------------- #
# Utilitarios
# --------------------------------------------------------------------------- #

def num(valor, padrao: float = 0.0) -> float:
    """Converte '47,03' / '47.03' / 47 para float."""
    if valor is None:
        return padrao
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if not texto:
        return padrao
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return padrao


def inteiro(valor, padrao: int = 0) -> int:
    try:
        return int(round(num(valor)))
    except (TypeError, ValueError):
        return padrao


def apenas_digitos(valor: str) -> str:
    return "".join(c for c in (valor or "") if c.isdigit())


class Cache:
    """Cache em memoria com validade e possibilidade de servir valor antigo."""

    def __init__(self) -> None:
        self._dados: dict[str, tuple[float, object]] = {}
        self._chaves: dict[str, threading.Lock] = {}
        self._trava = threading.Lock()

    def guardar(self, chave: str, valor: object) -> None:
        with self._trava:
            self._dados[chave] = (time.time(), valor)

    def obter(self, chave: str, ttl: float | None = None):
        with self._trava:
            item = self._dados.get(chave)
        if not item:
            return None
        idade = time.time() - item[0]
        if ttl is not None and idade > ttl:
            return None
        return item[1]

    def idade(self, chave: str) -> float | None:
        with self._trava:
            item = self._dados.get(chave)
        return None if not item else time.time() - item[0]

    def trava(self, chave: str) -> threading.Lock:
        with self._trava:
            return self._chaves.setdefault(chave, threading.Lock())


CACHE = Cache()


class ErroTSE(Exception):
    def __init__(self, mensagem: str, status: int = 502) -> None:
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = status


def _requisitar(caminho: str, tentativas: int = 3) -> bytes:
    """GET no TSE com retentativas e cabecalhos de navegador."""
    url = TSE_BASE + caminho
    ultimo_erro: Exception | None = None

    for tentativa in range(tentativas):
        requisicao = urlrequest.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "pt-BR,pt;q=0.9",
                "Referer": "https://resultados.tse.jus.br/oficial/app/index.html",
            },
        )
        try:
            with urlrequest.urlopen(requisicao, timeout=20) as resposta:
                return resposta.read()
        except urlerror.HTTPError as erro:
            ultimo_erro = erro
            if erro.code == 404:
                raise ErroTSE(
                    "O TSE nao tem dados publicados para esta combinacao "
                    "(eleicao/cargo/UF/municipio).",
                    status=404,
                ) from None
            if erro.code in (403, 429, 500, 502, 503, 504):
                time.sleep(0.4 * (tentativa + 1))
                continue
            raise ErroTSE(f"O TSE respondeu HTTP {erro.code}.", status=502) from None
        except Exception as erro:  # timeouts, DNS, TLS...
            ultimo_erro = erro
            time.sleep(0.4 * (tentativa + 1))

    raise ErroTSE(f"Falha ao consultar o TSE ({ultimo_erro}).", status=504)


_CONEXAO_LOCAL = threading.local()


def _fechar_conexao_mapa() -> None:
    conexao = getattr(_CONEXAO_LOCAL, "conexao", None)
    if conexao is not None:
        try:
            conexao.close()
        except OSError:
            pass
    _CONEXAO_LOCAL.conexao = None


def _requisitar_mapa(caminho: str, tentativas: int = 2) -> bytes:
    """GET no TSE reaproveitando a conexao TLS de cada thread do mapa.

    O mapa faz milhares de consultas pequenas; abrir uma conexao nova para
    cada uma deixaria a varredura varias vezes mais lenta.
    """
    partes = urlparse.urlsplit(TSE_BASE + caminho)
    alvo = partes.path + (f"?{partes.query}" if partes.query else "")
    ultimo_erro: Exception | None = None

    for tentativa in range(tentativas):
        conexao = getattr(_CONEXAO_LOCAL, "conexao", None)
        try:
            if conexao is None:
                conexao = http.client.HTTPSConnection(
                    partes.hostname or "resultados.tse.jus.br",
                    partes.port or 443,
                    timeout=25,
                )
                _CONEXAO_LOCAL.conexao = conexao
            conexao.request(
                "GET",
                alvo,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "pt-BR,pt;q=0.9",
                    "Referer": "https://resultados.tse.jus.br/oficial/app/index.html",
                    "Connection": "keep-alive",
                },
            )
            resposta = conexao.getresponse()
            corpo = resposta.read()
            if resposta.status == 200:
                return corpo
            if resposta.status == 404:
                raise ErroTSE("Sem dados para o municipio.", status=404)
            ultimo_erro = f"HTTP {resposta.status}"
            if resposta.status not in (403, 429, 500, 502, 503, 504):
                raise ErroTSE(f"O TSE respondeu HTTP {resposta.status}.", status=502)
        except ErroTSE:
            raise
        except Exception as erro:  # timeout, conexao fechada pelo servidor...
            ultimo_erro = erro
            _fechar_conexao_mapa()

        if tentativa + 1 < tentativas:
            time.sleep(0.3 * (tentativa + 1))

    _fechar_conexao_mapa()
    raise ErroTSE(f"Falha ao consultar o municipio no TSE ({ultimo_erro}).", status=504)


def buscar_json(caminho: str, ttl: float, chave: str | None = None):
    """Busca JSON no TSE com cache curto e fallback para a ultima versao boa."""
    chave = chave or caminho
    em_cache = CACHE.obter(chave, ttl)
    if em_cache is not None:
        return em_cache

    with CACHE.trava(chave):
        em_cache = CACHE.obter(chave, ttl)
        if em_cache is not None:
            return em_cache

        # nocache em janelas de 5s: evita CDN antigo sem multiplicar requisicoes
        marca = int(time.time() // 5)
        separador = "&" if "?" in caminho else "?"
        try:
            bruto = _requisitar(f"{caminho}{separador}nocache={marca}")
            dados = json.loads(bruto.decode("utf-8"))
        except ErroTSE:
            antigo = CACHE.obter(chave)
            if antigo is not None:
                return _com_aviso(antigo, CACHE.idade(chave) or 0)
            raise
        except json.JSONDecodeError:
            antigo = CACHE.obter(chave)
            if antigo is not None:
                return _com_aviso(antigo, CACHE.idade(chave) or 0)
            raise ErroTSE("O TSE devolveu uma resposta invalida.", status=502) from None

        CACHE.guardar(chave, dados)
        return dados


def _com_aviso(dados, idade: float):
    """Marca um payload antigo (dict) como desatualizado sem alterar o original."""
    if not isinstance(dados, dict):
        return dados
    copia = dict(dados)
    copia["_cache"] = {"stale": True, "idadeSegundos": round(idade, 1)}
    return copia


def buscar_foto(caminho: str) -> bytes:
    em_cache = CACHE.obter(caminho, TTL_FOTO)
    if em_cache is not None:
        return em_cache
    with CACHE.trava(caminho):
        em_cache = CACHE.obter(caminho, TTL_FOTO)
        if em_cache is not None:
            return em_cache
        bruto = _requisitar(caminho, tentativas=2)
        CACHE.guardar(caminho, bruto)
        return bruto


# --------------------------------------------------------------------------- #
# Configuracao de eleicoes (catalogo ele-c.json)
# --------------------------------------------------------------------------- #

def carregar_config():
    bruto = buscar_json("/comum/config/ele-c.json", TTL_CONFIG, chave="config:ele-c")
    return normalizar_config(bruto)


def normalizar_config(bruto: dict) -> dict:
    eleicoes = []

    for pleito in bruto.get("pl", []):
        ciclo = str(pleito.get("c") or "")
        data_pleito = pleito.get("dt") or ""
        ano = data_pleito[-4:] if data_pleito else ciclo[-4:]
        for eleicao in pleito.get("e", []):
            tipo_cd = inteiro(eleicao.get("tp"))
            if tipo_cd not in TIPOS_ELEICAO:
                continue  # ignora consultas populares

            abrangencias = []
            for abrangencia in eleicao.get("abr", []):
                cargos = [
                    {
                        "cd": str(cargo.get("cd")),
                        "nome": cargo.get("ds"),
                        "tipo": str(cargo.get("tp") or ""),
                    }
                    for cargo in abrangencia.get("cp", [])
                    if str(cargo.get("tp") or "") in ("1", "2")
                ]
                if not cargos:
                    continue
                abrangencias.append(
                    {
                        "uf": str(abrangencia.get("cd") or "br").lower(),
                        "cargos": cargos,
                        "municipios": [str(m.get("cd")) for m in abrangencia.get("mu") or []],
                    }
                )

            if not abrangencias:
                continue

            turno = inteiro(eleicao.get("t"), 1)
            tipo = TIPOS_ELEICAO[tipo_cd]
            nome = eleicao.get("nm") or ""
            data_iso = ""
            pedacos = data_pleito.split("/")
            if len(pedacos) == 3:
                data_iso = f"{pedacos[2]}{pedacos[1]}{pedacos[0]}"
            eleicoes.append(
                {
                    "cd": str(eleicao.get("cd")),
                    "ciclo": ciclo,
                    "ano": ano,
                    "turno": turno,
                    "data": data_pleito,
                    "nome": nome,
                    "tipo": tipo,
                    # tp 3 = municipal e tp 4 = suplementar (sempre disputa municipal)
                    "municipal": tipo_cd in (3, 4),
                    "pleito": str(pleito.get("cd")),
                    "abrangencias": abrangencias,
                    "_ordem": data_iso,
                }
            )

    # mais recentes primeiro
    eleicoes.sort(key=lambda e: (e["ano"], e["_ordem"], e["turno"], e["cd"]), reverse=True)
    for eleicao in eleicoes:
        eleicao.pop("_ordem", None)

    # Painel dedicado: apenas presidente 2026, 2o turno.
    alvo = [
        eleicao
        for eleicao in eleicoes
        if eleicao["tipo"] == PLEITO_ALVO["tipo"]
        and eleicao["ano"] == PLEITO_ALVO["ano"]
        and eleicao["turno"] == PLEITO_ALVO["turno"]
    ]
    if alvo:
        eleicoes = alvo[:1]

    return {
        "geradoEm": f'{bruto.get("dg", "")} {bruto.get("hg", "")}'.strip(),
        "fase": bruto.get("f"),
        "eleicoes": eleicoes,
        "ufs": UF_NOMES,
    }


# --------------------------------------------------------------------------- #
# Resultados
# --------------------------------------------------------------------------- #

def montar_caminho_resultado(ciclo, eleicao, cargo, uf, municipio) -> tuple[str, str]:
    ciclo = (ciclo or "").strip().lower()
    if not ciclo.startswith("ele") or not apenas_digitos(ciclo[3:]):
        raise ErroTSE("Ciclo eleitoral invalido.", status=400)

    eleicao = apenas_digitos(eleicao)
    cargo = apenas_digitos(cargo)
    uf = (uf or "br").strip().lower()
    if uf != "br":
        uf = "".join(c for c in uf if c.isalpha())[:2]
    if not eleicao or not cargo:
        raise ErroTSE("Parametros eleicao/cargo sao obrigatorios.", status=400)
    if uf != "br" and uf not in UF_NOMES:
        raise ErroTSE("UF invalida.", status=400)

    segmento_uf = "br" if uf == "br" else uf
    prefixo = segmento_uf
    municipio = apenas_digitos(municipio)
    if municipio:
        prefixo = f"{segmento_uf}{municipio.zfill(5)}"

    arquivo = f"{prefixo}-c{cargo.zfill(4)}-e{eleicao.zfill(6)}-u.json"
    caminho = f"/{ciclo}/{eleicao}/dados/{segmento_uf}/{arquivo}"
    return caminho, arquivo


def normalizar_resultado(bruto: dict, contexto: dict) -> dict:
    cargos = bruto.get("carg") or []
    cargo = cargos[0] if cargos else {}

    candidatos = []
    for agregado in cargo.get("agr", []):
        for partido in agregado.get("par", []):
            for candidato in partido.get("cand", []):
                vices = [
                    {"nome": v.get("nm"), "partido": v.get("sgp")}
                    for v in candidato.get("vs") or []
                ]
                candidatos.append(
                    {
                        "numero": candidato.get("n"),
                        "nome": candidato.get("nm"),
                        "nomeUrna": candidato.get("nmu") or candidato.get("nm"),
                        "partido": partido.get("sg") or "",
                        "partidoNome": partido.get("nm") or "",
                        "coligacao": agregado.get("nm") if agregado.get("tp") != "p" else None,
                        "votos": inteiro(candidato.get("vap")),
                        "pct": num(candidato.get("pvapn")) or num(candidato.get("pvap")),
                        "pctTexto": candidato.get("pvap") or "0,00",
                        "situacao": candidato.get("st") or "",
                        "eleito": candidato.get("e") == "s",
                        "sqcand": str(candidato.get("sqcand") or ""),
                        "vices": vices,
                    }
                )

    candidatos.sort(key=lambda c: (-c["votos"], c["numero"] or ""))

    secoes = bruto.get("s") or {}
    eleitorado = bruto.get("e") or {}
    votos = bruto.get("v") or {}
    andamento = bruto.get("and")
    if andamento == "f":
        situacao_apuracao = "finalizada"
    elif andamento == "n":
        situacao_apuracao = "aguardando"
    else:
        situacao_apuracao = "em_andamento"

    return {
        "contexto": contexto,
        "eleicao": bruto.get("ele"),
        "turno": inteiro(bruto.get("t"), 1),
        "fase": bruto.get("f"),
        "uf": bruto.get("cdabr"),
        "cargo": {
            "cd": cargo.get("cd"),
            "nome": cargo.get("nmn") or cargo.get("nm"),
            "nomeMasculino": cargo.get("nmm"),
            "nomeFeminino": cargo.get("nmf"),
        },
        "geradoEm": f'{bruto.get("dg", "")} {bruto.get("hg", "")}'.strip(),
        "totalizado": situacao_apuracao == "finalizada",
        "situacaoApuracao": situacao_apuracao,
        "secoes": {
            "total": inteiro(secoes.get("ts")),
            "totalizadas": inteiro(secoes.get("st")),
            "pct": num(secoes.get("pstn")) or num(secoes.get("pst")),
            "pctTexto": secoes.get("pst") or "0,00",
            "pendentes": inteiro(secoes.get("snt")),
            "instaladas": inteiro(secoes.get("si")),
            "interrompidas": inteiro(secoes.get("sni")),
        },
        "eleitorado": {
            "total": inteiro(eleitorado.get("te")),
            "aptos": inteiro(eleitorado.get("est")) or inteiro(eleitorado.get("esi")),
            "comparecimento": inteiro(eleitorado.get("c")),
            "pctComparecimento": num(eleitorado.get("pcn")) or num(eleitorado.get("pc")),
            "pctComparecimentoTexto": eleitorado.get("pc") or "0,00",
            "abstencao": inteiro(eleitorado.get("a")),
            "pctAbstencao": num(eleitorado.get("pan")) or num(eleitorado.get("pa")),
            "pctAbstencaoTexto": eleitorado.get("pa") or "0,00",
        },
        "votos": {
            "total": inteiro(votos.get("tv")),
            "validos": inteiro(votos.get("vvc")) or inteiro(votos.get("vv")),
            "pctValidos": num(votos.get("pvvcn")) or num(votos.get("pvvc")),
            "pctValidosTexto": votos.get("pvvc") or "0,00",
            "brancos": inteiro(votos.get("vb")),
            "pctBrancos": num(votos.get("pvbn")) or num(votos.get("pvb")),
            "pctBrancosTexto": votos.get("pvb") or "0,00",
            "nulos": inteiro(votos.get("vn")),
            "pctNulos": num(votos.get("pvnn")) or num(votos.get("pvn")),
            "pctNulosTexto": votos.get("pvn") or "0,00",
            "anulados": inteiro(votos.get("van")),
        },
        "candidatos": candidatos,
        "recebidoEm": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cache": bruto.get("_cache"),
    }


def obter_resultado(ciclo, eleicao, cargo, uf, municipio):
    caminho, arquivo = montar_caminho_resultado(ciclo, eleicao, cargo, uf, municipio)
    contexto = {
        "ciclo": ciclo,
        "eleicao": apenas_digitos(eleicao),
        "cargo": apenas_digitos(cargo),
        "uf": (uf or "br").lower(),
        "municipio": apenas_digitos(municipio),
        "arquivo": arquivo,
        "fonte": TSE_BASE + caminho,
    }
    chave = "resultado:" + caminho
    bruto = buscar_json(caminho, TTL_RESULTADO, chave=chave)
    payload = normalizar_resultado(bruto, contexto)
    idade = CACHE.idade(chave)
    payload["idadeCacheSegundos"] = round(idade, 1) if idade is not None else 0
    return payload


# --------------------------------------------------------------------------- #
# Acompanhamento (por UF / municipio)
# --------------------------------------------------------------------------- #

def obter_acompanhamento(ciclo, eleicao, uf="br"):
    eleicao = apenas_digitos(eleicao)
    uf = (uf or "br").lower()
    if uf != "br" and uf not in UF_NOMES:
        raise ErroTSE("UF invalida.", status=400)

    segmento = "br" if uf == "br" else uf
    arquivo = f"{segmento}-e{eleicao.zfill(6)}-ab.json"
    caminho = f"/{ciclo}/{eleicao}/dados/{segmento}/{arquivo}"

    bruto = buscar_json(caminho, TTL_ACOMPANHAMENTO, chave="ab:" + caminho)
    linhas = []

    nomes_municipios = {}
    if uf != "br":
        nomes_municipios = mapa_municipios(ciclo, eleicao, uf)

    for item in bruto.get("abr", []):
        codigo = str(item.get("cdabr") or "").lower()
        if codigo == "br":
            continue  # linha agregada do Brasil: os totais já aparecem nos KPIs
        tipo = item.get("tpabr") or ("uf" if codigo in UF_NOMES else "mun")
        if codigo == "zz":
            nome = "Exterior"
        else:
            nome = UF_NOMES.get(codigo) or nomes_municipios.get(codigo) or codigo.upper()
        secoes = item.get("s") or {}
        eleitorado = item.get("e") or {}
        linhas.append(
            {
                "codigo": codigo,
                "nome": nome,
                "tipo": tipo,
                "atualizadoEm": f'{item.get("dt", "")} {item.get("ht", "")}'.strip(),
                "secoes": {
                    "total": inteiro(secoes.get("ts")),
                    "totalizadas": inteiro(secoes.get("st")),
                    "pct": num(secoes.get("pstn")) or num(secoes.get("pst")),
                    "pctTexto": secoes.get("pst") or "0,00",
                },
                "comparecimento": {
                    "votos": inteiro(eleitorado.get("c")),
                    "pct": num(eleitorado.get("pcn")) or num(eleitorado.get("pc")),
                    "pctTexto": eleitorado.get("pc") or "0,00",
                },
                "abstencao": {
                    "votos": inteiro(eleitorado.get("a")),
                    "pct": num(eleitorado.get("pan")) or num(eleitorado.get("pa")),
                    "pctTexto": eleitorado.get("pa") or "0,00",
                },
                "eleitorado": inteiro(eleitorado.get("te")),
            }
        )

    linhas.sort(key=lambda l: l["nome"])
    return {
        "eleicao": bruto.get("ele"),
        "turno": inteiro(bruto.get("t"), 1),
        "geradoEm": f'{bruto.get("dg", "")} {bruto.get("hg", "")}'.strip(),
        "escopo": "uf" if uf == "br" else "municipio",
        "uf": uf,
        "linhas": linhas,
    }


# --------------------------------------------------------------------------- #
# Municipios
# --------------------------------------------------------------------------- #

def _carregar_municipios(ciclo, eleicao):
    eleicao = apenas_digitos(eleicao)
    arquivo = f"mun-e{eleicao.zfill(6)}-cm.json"
    caminho = f"/{ciclo}/{eleicao}/config/{arquivo}"
    return buscar_json(caminho, TTL_MUNICIPIOS, chave="mun:" + caminho)


def mapa_municipios(ciclo, eleicao, uf):
    try:
        bruto = _carregar_municipios(ciclo, eleicao)
    except ErroTSE:
        return {}
    for abrangencia in bruto.get("abr", []):
        if str(abrangencia.get("cd")).lower() == uf.lower():
            return {str(m.get("cd")): m.get("nm") for m in abrangencia.get("mu", [])}
    return {}


def obter_municipios(ciclo, eleicao, uf=None):
    bruto = _carregar_municipios(ciclo, eleicao)
    ufs = []
    municipios = []
    for abrangencia in bruto.get("abr", []):
        codigo_uf = str(abrangencia.get("cd") or "").lower()
        lista = abrangencia.get("mu") or []
        ufs.append(
            {
                "uf": codigo_uf,
                "nome": UF_NOMES.get(codigo_uf, codigo_uf.upper()),
                "total": len(lista),
            }
        )
        if uf and codigo_uf == uf.lower():
            municipios = sorted(
                [
                    {"cd": str(m.get("cd")), "nome": m.get("nm")}
                    for m in lista
                ],
                key=lambda m: m["nome"] or "",
            )
    ufs.sort(key=lambda u: u["nome"])
    return {
        "geradoEm": f'{bruto.get("dg", "")} {bruto.get("hg", "")}'.strip(),
        "ufs": ufs,
        "municipios": municipios,
    }


# --------------------------------------------------------------------------- #
# Mapa municipal (varredura em segundo plano)
# --------------------------------------------------------------------------- #

class MapaMunicipal:
    """Monta, cidade por cidade, o candidato mais votado em cada municipio.

    O TSE nao publica um arquivo agregado de votos por municipio: e preciso
    consultar o -u.json de cada cidade. A varredura e progressiva, prioriza as
    cidades com maior eleitorado e reaproveita os resultados ja finalizados.
    """

    def __init__(self) -> None:
        self._trava = threading.RLock()
        self._chave: tuple[str, str, str] | None = None
        self._thread: threading.Thread | None = None
        self._parar = threading.Event()
        self._ciclo = ""
        self._eleicao = ""
        self._cargo = ""
        self._lista: list[dict] = []
        self._municipios: dict[str, dict] = {}
        self._candidatos: list[dict] = []
        self._progresso = self._progresso_inicial()

    @staticmethod
    def _progresso_inicial() -> dict:
        return {
            "total": 0,
            "prontos": 0,
            "falhas": 0,
            "varrendo": False,
            "finalizado": False,
            "onda": 0,
            "iniciadoEm": None,
            "atualizadoEm": None,
            "concluidoEm": None,
            "erro": None,
            "nacional": None,
        }

    # ------------------------------------------------------------- controle
    def iniciar(self, ciclo: str, eleicao: str, cargo: str) -> None:
        chave = (ciclo, eleicao, cargo)
        with self._trava:
            if self._chave == chave and self._thread and self._thread.is_alive():
                return
            if self._thread and self._thread.is_alive():
                self._parar.set()
            self._parar = threading.Event()
            self._chave = chave
            self._ciclo, self._eleicao, self._cargo = chave
            self._lista = []
            self._municipios = {}
            self._candidatos = []
            self._progresso = self._progresso_inicial()
            self._progresso["iniciadoEm"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._thread = threading.Thread(
                target=self._laco,
                args=(chave, self._parar),
                daemon=True,
                name="mapa-tse",
            )
            self._thread.start()

    def _laco(self, chave: tuple[str, str, str], parar: threading.Event) -> None:
        ciclo, eleicao, cargo = chave
        try:
            self._preparar_lista(ciclo, eleicao)
            while not parar.is_set():
                try:
                    nacional = obter_resultado(ciclo, eleicao, cargo, "br", "")
                except ErroTSE as erro:
                    self._definir_erro(f"Nao foi possivel consultar o total nacional: {erro.mensagem}")
                    if parar.wait(15):
                        break
                    continue

                self._definir_candidatos(nacional)
                self._guardar_nacional(nacional)
                with self._trava:
                    self._progresso["erro"] = None  # o TSE voltou a responder

                if nacional["votos"]["total"] <= 0:
                    if parar.wait(15):
                        break
                    continue

                with self._trava:
                    agora = time.time()
                    fila = [
                        municipio
                        for municipio in self._lista
                        if not municipio["final"] and municipio["proximaTentativa"] <= agora
                    ]
                    self._progresso["varrendo"] = bool(fila)
                    self._progresso["falhas"] = 0
                    if fila:
                        self._progresso["onda"] += 1

                if not fila:
                    with self._trava:
                        finalizado = bool(self._lista) and all(m["final"] for m in self._lista)
                        self._progresso["varrendo"] = False
                        self._progresso["finalizado"] = finalizado
                        if finalizado:
                            self._progresso["concluidoEm"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    if parar.wait(MAPA_PAUSA_FINAL if finalizado else MAPA_PAUSA):
                        break
                    continue

                self._varredura(fila, parar)
                with self._trava:
                    self._progresso["varrendo"] = False
                    self._progresso["atualizadoEm"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                if parar.wait(10):
                    break
        except Exception as erro:  # pragma: no cover - protege a thread
            self._definir_erro(f"Falha ao montar o mapa: {erro}")

    def _definir_erro(self, mensagem: str) -> None:
        with self._trava:
            self._progresso["erro"] = mensagem
            self._progresso["varrendo"] = False

    def _guardar_nacional(self, nacional: dict) -> None:
        with self._trava:
            self._progresso["nacional"] = {
                "situacao": nacional.get("situacaoApuracao"),
                "secoesPct": nacional.get("secoes", {}).get("pct", 0),
                "votosTotal": nacional.get("votos", {}).get("total", 0),
                "geradoEm": nacional.get("geradoEm") or "",
            }

    def _definir_candidatos(self, nacional: dict) -> None:
        with self._trava:
            if self._candidatos:
                return
            candidatos = []
            for candidato in nacional.get("candidatos") or []:
                chave = str(candidato.get("sqcand") or "")
                if not chave:
                    continue
                candidatos.append(
                    {
                        "sqcand": chave,
                        "nome": candidato.get("nomeUrna") or candidato.get("nome") or chave,
                        "partido": candidato.get("partido") or "",
                        "numero": candidato.get("numero") or "",
                    }
                )
            candidatos.sort(key=lambda c: (inteiro(c["numero"]), c["nome"]))
            self._candidatos = candidatos

    # ------------------------------------------------------------- listas
    def _preparar_lista(self, ciclo: str, eleicao: str) -> None:
        bruto = _carregar_municipios(ciclo, eleicao)
        por_ibge: dict[str, dict] = {}
        for abrangencia in bruto.get("abr", []):
            uf = str(abrangencia.get("cd") or "").lower()
            if uf not in UF_NOMES:
                continue  # exterior (zz) nao tem malha municipal
            for municipio in abrangencia.get("mu") or []:
                cdi = str(municipio.get("cdi") or "").strip()
                cd = str(municipio.get("cd") or "").strip()
                if not cdi or not cd or cdi in por_ibge:
                    continue
                por_ibge[cdi] = {
                    "cd": cd,
                    "cdi": cdi,
                    "uf": uf,
                    "nome": municipio.get("nm") or cdi,
                    "final": False,
                    "proximaTentativa": 0.0,
                    "tentativas": 0,
                }

        lista = list(por_ibge.values())
        eleitorado = self._coletar_eleitorado(ciclo, eleicao, sorted({m["uf"] for m in lista}))
        lista.sort(key=lambda m: (-eleitorado.get(m["cd"], 0), m["nome"]))

        with self._trava:
            self._lista = lista
            self._progresso["total"] = len(lista)

    def _coletar_eleitorado(self, ciclo: str, eleicao: str, ufs: list[str]) -> dict[str, int]:
        """Le o eleitorado de cada municipio nos arquivos -ab.json por UF."""
        resultado: dict[str, int] = {}
        fila = list(ufs)
        trava = threading.Lock()

        def trabalhador() -> None:
            while True:
                with trava:
                    if not fila:
                        return
                    uf = fila.pop()
                caminho = f"/{ciclo}/{eleicao}/dados/{uf}/{uf}-e{eleicao.zfill(6)}-ab.json"
                try:
                    bruto = buscar_json(caminho, TTL_ACOMPANHAMENTO, chave="ab:" + caminho)
                except ErroTSE:
                    continue
                locais = {}
                for item in bruto.get("abr", []):
                    codigo = str(item.get("cdabr") or "")
                    if codigo:
                        locais[codigo] = inteiro((item.get("e") or {}).get("te"))
                with trava:
                    resultado.update(locais)

        threads = [
            threading.Thread(target=trabalhador, daemon=True, name=f"mapa-eleitorado-{i}")
            for i in range(min(8, len(ufs)))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return resultado

    # ------------------------------------------------------------ varredura
    def _varredura(self, fila: list[dict], parar: threading.Event) -> None:
        indice = {"atual": 0}
        trava = threading.Lock()

        def trabalhador() -> None:
            while not parar.is_set():
                with trava:
                    posicao = indice["atual"]
                    indice["atual"] += 1
                if posicao >= len(fila):
                    return
                self._buscar_municipio(fila[posicao])

        threads = [
            threading.Thread(target=trabalhador, daemon=True, name=f"mapa-{i}")
            for i in range(min(MAPA_WORKERS, len(fila)))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def _buscar_municipio(self, municipio: dict) -> None:
        caminho, _ = montar_caminho_resultado(
            self._ciclo, self._eleicao, self._cargo, municipio["uf"], municipio["cd"]
        )
        separador = "&" if "?" in caminho else "?"
        caminho = f"{caminho}{separador}nocache={int(time.time() // 5)}"

        try:
            bruto = json.loads(_requisitar_mapa(caminho).decode("utf-8"))
        except (ErroTSE, json.JSONDecodeError) as erro:
            with self._trava:
                municipio["tentativas"] += 1
                status = getattr(erro, "status", 0)
                municipio["proximaTentativa"] = time.time() + (180 if status == 404 else 60)
                self._progresso["falhas"] += 1
            return

        votos_por_candidato: dict[str, int] = {}
        for agregado in (bruto.get("carg") or [{}])[0].get("agr", []):
            for partido in agregado.get("par", []):
                for candidato in partido.get("cand", []):
                    chave = str(candidato.get("sqcand") or "")
                    if chave:
                        votos_por_candidato[chave] = votos_por_candidato.get(chave, 0) + inteiro(
                            candidato.get("vap")
                        )

        with self._trava:
            candidatos = list(self._candidatos)

        votos = [votos_por_candidato.get(c["sqcand"], 0) for c in candidatos]
        total = sum(votos)
        vencedor = -1
        percentual = 0.0
        if total > 0:
            vencedor = max(range(len(votos)), key=lambda i: votos[i])
            if votos.count(votos[vencedor]) > 1:
                vencedor = -1  # empate: sem maioria
            elif vencedor >= 0:
                percentual = round(votos[vencedor] / total * 100, 1)

        secoes = bruto.get("s") or {}
        final = bruto.get("and") == "f"
        registro = {
            "c": municipio["cdi"],
            "u": municipio["uf"],
            "n": municipio["nome"],
            "v": votos,
            "w": vencedor,
            "p": percentual,
            "f": final,
            "s": round(num(secoes.get("pstn")) or num(secoes.get("pst")), 1),
            "tse": f'{bruto.get("dg", "")} {bruto.get("hg", "")}'.strip(),
        }

        with self._trava:
            self._municipios[municipio["cdi"]] = registro
            municipio["final"] = final
            municipio["tentativas"] = 0
            municipio["proximaTentativa"] = time.time() + (3600 if final else TTL_MAPA)
            self._progresso["prontos"] = len(self._municipios)
            self._progresso["atualizadoEm"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    # ------------------------------------------------------------- resposta
    def snapshot(self) -> dict:
        with self._trava:
            candidatos = [dict(c) for c in self._candidatos]
            municipios = [
                [
                    r["c"],
                    r["u"],
                    r["n"],
                    list(r["v"]),
                    r["w"],
                    r["p"],
                    1 if r["f"] else 0,
                    r.get("s", 0),
                ]
                for r in self._municipios.values()
            ]
            progresso = dict(self._progresso)
            if progresso.get("nacional"):
                progresso["nacional"] = dict(progresso["nacional"])

        municipios.sort(key=lambda item: item[0])
        por_candidato = [
            {"sqcand": c["sqcand"], "municipios": 0, "votos": 0} for c in candidatos
        ]
        sem_dados = 0
        for municipio in municipios:
            votos = municipio[3]
            for indice, candidato in enumerate(por_candidato):
                if indice < len(votos):
                    candidato["votos"] += votos[indice]
            vencedor = municipio[4]
            if vencedor < 0:
                sem_dados += 1
                continue
            por_candidato[vencedor]["municipios"] += 1

        return {
            "ok": True,
            "candidatos": candidatos,
            "municipios": municipios,
            "resumo": {
                "porCandidato": por_candidato,
                "comDados": len(municipios) - sem_dados,
                "semDados": sem_dados,
            },
            "progresso": progresso,
        }


MAPA = MapaMunicipal()


def obter_mapa(ciclo: str, eleicao: str, cargo: str) -> dict:
    MAPA.iniciar(ciclo, eleicao, cargo)
    return MAPA.snapshot()


# --------------------------------------------------------------------------- #
# Servidor HTTP
# --------------------------------------------------------------------------- #

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
}


class PainelHandler(BaseHTTPRequestHandler):
    server_version = "PainelTSE/1.0"
    protocol_version = "HTTP/1.1"

    # ----------------------------------------------------------------- utils
    def _cabecalhos(self, status, content_type, tamanho, cache="no-store", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(tamanho))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        for chave, valor in (extra or {}).items():
            self.send_header(chave, valor)
        self.end_headers()

    def _json(self, dados, status=200):
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self._cabecalhos(status, "application/json; charset=utf-8", len(corpo))
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _erro(self, mensagem, status=500):
        self._json({"erro": mensagem, "status": status}, status=status)

    def _bytes(self, dados, content_type, cache="public, max-age=86400"):
        self._cabecalhos(status=200, content_type=content_type, tamanho=len(dados), cache=cache)
        if self.command != "HEAD":
            self.wfile.write(dados)

    # ----------------------------------------------------------------- rotas
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        partes = urlparse.urlsplit(self.path)
        rota = partes.path
        params = {k: v[0] for k, v in urlparse.parse_qs(partes.query).items()}

        try:
            if rota.startswith("/api/"):
                self._rota_api(rota, params)
            else:
                self._estatico(rota)
        except ErroTSE as erro:
            self._erro(erro.mensagem, erro.status)
        except BrokenPipeError:
            pass
        except Exception as erro:  # pragma: no cover
            self._erro(f"Erro interno: {erro}", 500)

    def _rota_api(self, rota, params):
        if rota == "/api/health":
            self._json({"ok": True, "hora": time.strftime("%H:%M:%S")})
            return

        if rota == "/api/config":
            self._json(carregar_config())
            return

        if rota == "/api/resultado":
            ciclo = params.get("ciclo") or ""
            eleicao = params.get("eleicao") or ""
            cargo = params.get("cargo") or ""
            uf = params.get("uf") or "br"
            municipio = params.get("municipio") or ""
            self._json(obter_resultado(ciclo, eleicao, cargo, uf, municipio))
            return

        if rota == "/api/acompanhamento":
            ciclo = params.get("ciclo") or ""
            eleicao = apenas_digitos(params.get("eleicao") or "")
            if not ciclo.startswith("ele") or not eleicao:
                raise ErroTSE("Parametros invalidos.", status=400)
            self._json(obter_acompanhamento(ciclo, eleicao, params.get("uf") or "br"))
            return

        if rota == "/api/municipios":
            ciclo = params.get("ciclo") or ""
            eleicao = apenas_digitos(params.get("eleicao") or "")
            if not ciclo.startswith("ele") or not eleicao:
                raise ErroTSE("Parametros invalidos.", status=400)
            self._json(obter_municipios(ciclo, eleicao, params.get("uf")))
            return

        if rota == "/api/mapa":
            ciclo = (params.get("ciclo") or "").strip().lower()
            eleicao = apenas_digitos(params.get("eleicao") or "")
            cargo = apenas_digitos(params.get("cargo") or "1")
            if not ciclo.startswith("ele") or not eleicao or not cargo:
                raise ErroTSE("Parametros do mapa invalidos.", status=400)
            self._json(obter_mapa(ciclo, eleicao, cargo))
            return

        if rota == "/api/foto":
            self._foto(params)
            return

        self._erro("Rota nao encontrada.", 404)

    def _foto(self, params):
        ciclo = (params.get("ciclo") or "").strip().lower()
        eleicao = apenas_digitos(params.get("eleicao") or "")
        uf = (params.get("uf") or "br").strip().lower()
        candidato = apenas_digitos(params.get("id") or "")

        if not ciclo.startswith("ele") or not eleicao or not candidato:
            raise ErroTSE("Parametros da foto invalidos.", status=400)
        if uf != "br" and uf not in UF_NOMES:
            uf = "br"

        caminho = f"/{ciclo}/{eleicao}/fotos/{uf}/{candidato}.jpeg"
        try:
            dados = buscar_foto(caminho)
        except ErroTSE:
            # alguns cargos usam "br" mesmo em disputa estadual
            if uf != "br":
                dados = buscar_foto(f"/{ciclo}/{eleicao}/fotos/br/{candidato}.jpeg")
            else:
                raise
        self._bytes(dados, "image/jpeg")

    def _estatico(self, rota):
        if rota in ("/", ""):
            rota = "/index.html"
        caminho = os.path.realpath(os.path.join(WEB_DIR, rota.lstrip("/")))
        if not caminho.startswith(os.path.realpath(WEB_DIR)) or not os.path.isfile(caminho):
            self._erro("Arquivo nao encontrado.", 404)
            return
        extensao = os.path.splitext(caminho)[1].lower()
        with open(caminho, "rb") as arquivo:
            dados = arquivo.read()
        # A malha municipal e grande e muda raramente: o navegador pode guardar.
        cache = "public, max-age=86400" if rota.startswith("/dados/") else "no-cache"
        self._bytes(dados, MIME.get(extensao, "application/octet-stream"), cache=cache)

    def log_message(self, formato, *args):  # noqa: A003
        if os.environ.get("PAINEL_LOG") == "1":
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), formato % args))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

class ServidorPainel(ThreadingHTTPServer):
    """Evita duas instancias do painel na mesma porta (Windows permite por padrao)."""

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        erro = sys.exc_info()[1]
        if isinstance(erro, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return  # navegador fechou a conexao: nao e um erro do painel
        super().handle_error(request, client_address)


def ips_locais() -> list[str]:
    """Enderecos IPv4 nao-loopback desta maquina (para acesso pela rede)."""
    ips: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))  # nao envia pacotes
            ips.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def principal():
    parser = argparse.ArgumentParser(description="Painel TSE - presidente 2026, 2o turno")
    parser.add_argument("--port", type=int, default=PORTA_PADRAO, help="porta HTTP (padrao 8787)")
    parser.add_argument("--host", default="127.0.0.1", help="interface de escuta")
    parser.add_argument("--no-browser", action="store_true", help="nao abrir o navegador")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        servidor = ServidorPainel((args.host, args.port), PainelHandler)
    except OSError as exc:
        print("=" * 62)
        print(f"  Nao foi possivel abrir a porta {args.port}.")
        print("  O painel ja pode estar rodando; feche a outra janela ou use --port 9000.")
        print(f"  Detalhe: {exc}")
        print("=" * 62)
        raise SystemExit(1)

    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}/"

    print("=" * 62)
    print("  PAINEL TSE - presidente 2026 (2o turno)")
    print("=" * 62)
    print(f"  Painel:  {url}")
    if args.host in ("0.0.0.0", ""):
        for ip in ips_locais():
            print(f"  Rede:    http://{ip}:{args.port}/")
    print("  Fonte:   https://resultados.tse.jus.br/oficial")
    print("  Encerrar: CTRL+C")
    print("=" * 62)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando painel...")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    principal()
