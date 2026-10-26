#!/usr/bin/env python3
"""
Gera a malha municipal usada pelo mapa do painel.

Baixa a malha municipal do IBGE (qualidade minima, GeoJSON), projeta em
Mercator e grava caminhos SVG compactos em web/dados/municipios-br.json.

Uso:
    python ferramentas/gerar_malha.py
    python ferramentas/gerar_malha.py --origem C:\\caminho\\malha.geojson

O arquivo gerado tem o formato:
    {"largura": 1000, "altura": 992, "municipios": [["1100015", "M..."], ...]}
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request

MALHA_URL = (
    "https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
    "?formato=application/vnd.geo+json&qualidade=minima&intrarregiao=municipio"
)
LARGURA = 8000
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAIDA_PADRAO = os.path.join(RAIZ, "web", "dados", "municipios-br.json")


def baixar(origem: str | None) -> dict:
    if origem:
        with open(origem, encoding="utf-8") as arquivo:
            return json.load(arquivo)

    print("Baixando malha municipal do IBGE...")
    inicio = time.time()
    requisicao = urllib.request.Request(
        MALHA_URL,
        headers={"User-Agent": "PainelTSE/1.0 (malha municipal)"},
    )
    with urllib.request.urlopen(requisicao, timeout=180) as resposta:
        dados = json.loads(resposta.read().decode("utf-8"))
    print(f"  {len(dados.get('features', []))} municipios em {time.time() - inicio:.1f}s")
    return dados


def aneis(geometria: dict):
    """Percorre todos os aneis (externos e internos) de Polygon/MultiPolygon."""
    tipo = geometria.get("type")
    coordenadas = geometria.get("coordinates") or []
    if tipo == "Polygon":
        for anel in coordenadas:
            yield anel
    elif tipo == "MultiPolygon":
        for poligono in coordenadas:
            for anel in poligono:
                yield anel


def mercator_y(latitude: float) -> float:
    """Latitude -> Y de Mercator, em graus equivalentes (mesma escala de X)."""
    latitude = max(-85.0, min(85.0, latitude))
    return math.degrees(math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2)))


def gerar_caminho(anel, projetar) -> str:
    pontos = []
    anterior = None
    for ponto in anel:
        if not isinstance(ponto, (list, tuple)) or len(ponto) < 2:
            continue
        x, y = projetar(float(ponto[0]), float(ponto[1]))
        if anterior == (x, y):
            continue
        pontos.append((x, y))
        anterior = (x, y)

    # Remove o ultimo ponto se ele apenas fecha o anel (o "z" ja fecha).
    if len(pontos) > 1 and pontos[0] == pontos[-1]:
        pontos.pop()
    if len(pontos) < 3:
        return ""

    # Caminho relativo: economiza varios KB no arquivo final.
    saida = [f"M{pontos[0][0]} {pontos[0][1]}"]
    ultimo_x, ultimo_y = pontos[0]
    for x, y in pontos[1:]:
        dx, dy = x - ultimo_x, y - ultimo_y
        if dx or dy:
            saida.append(f"l{dx} {dy}")
            ultimo_x, ultimo_y = x, y
    saida.append("z")
    return "".join(saida)


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera a malha municipal do mapa")
    parser.add_argument("--origem", help="GeoJSON local (padrao: baixa do IBGE)")
    parser.add_argument("--saida", default=SAIDA_PADRAO, help="arquivo de saida")
    args = parser.parse_args()

    bruto = baixar(args.origem)
    features = bruto.get("features") or []
    if not features:
        print("A malha nao tem municipios.", file=sys.stderr)
        return 1

    # Limites geograficos de todos os pontos.
    lon_min = lat_min = math.inf
    lon_max = lat_max = -math.inf
    for feature in features:
        for anel in aneis(feature.get("geometry") or {}):
            for ponto in anel:
                if not isinstance(ponto, (list, tuple)) or len(ponto) < 2:
                    continue
                lon, lat = float(ponto[0]), float(ponto[1])
                lon_min, lon_max = min(lon_min, lon), max(lon_max, lon)
                lat_min, lat_max = min(lat_min, lat), max(lat_max, lat)

    y_min, y_max = mercator_y(lat_min), mercator_y(lat_max)
    escala = LARGURA / (lon_max - lon_min)
    altura = round((y_max - y_min) * escala)

    def projetar(lon: float, lat: float) -> tuple[int, int]:
        x = (lon - lon_min) * escala
        y = (y_max - mercator_y(lat)) * escala
        return round(x), round(y)

    municipios = []
    pontos_total = 0
    for feature in features:
        codigo = str((feature.get("properties") or {}).get("codarea") or "").strip()
        if not codigo:
            continue
        caminho = "".join(
            parte
            for parte in (gerar_caminho(anel, projetar) for anel in aneis(feature.get("geometry") or {}))
            if parte
        )
        if not caminho:
            continue
        municipios.append([codigo, caminho])
        pontos_total += caminho.count("l") + caminho.count("M")

    municipios.sort(key=lambda item: item[0])
    destino = os.path.abspath(args.saida)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "w", encoding="utf-8") as arquivo:
        json.dump(
            {"largura": LARGURA, "altura": altura, "municipios": municipios},
            arquivo,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    tamanho = os.path.getsize(destino) / 1024 / 1024
    print(f"  {len(municipios)} municipios, {pontos_total} pontos")
    print(f"  {destino} ({tamanho:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
