"""
Rastreoil - Generador de páginas estáticas por municipio.

Descarga el dataset de precios del Ministerio y produce en `dist/`:

  index.html                              portada con el listado de provincias
  gasolineras/<provincia>/index.html      todas las EESS de la provincia por municipio
  gasolineras/<provincia>/<municipio>/    página de municipio (el objetivo SEO)
  sitemap.xml                             índice + tramos de 45.000 URLs
  robots.txt
  estilos.css

Una única llamada al origen: el propio listado de estaciones ya trae municipio y
provincia, así que no hace falta el endpoint de listados.

Uso:
    python build.py [--salida dist] [--base https://rastreoil.es]
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import re
import shutil
import time
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rastreoil.build")

ORIGEN_URL = (
    "https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes"
    "/PreciosCarburantes/EstacionesTerrestres/"
)
URLS_POR_SITEMAP = 45_000

# Carburantes que se muestran en las tablas, en orden de columna.
CARBURANTES: list[tuple[str, str, str]] = [
    ("gasolina95", "Precio Gasolina 95 E5", "Gasolina 95"),
    ("gasolina98", "Precio Gasolina 98 E5", "Gasolina 98"),
    ("diesel", "Precio Gasoleo A", "Diésel"),
    ("diesel_premium", "Precio Gasoleo Premium", "Diésel premium"),
    ("glp", "Precio Gases licuados del petróleo", "GLP"),
]
PRINCIPAL = "gasolina95"  # el que ordena las tablas y encabeza los titulares


# --- Normalización -----------------------------------------------------------

def a_float(valor: Any) -> float | None:
    if valor is None:
        return None
    texto = str(valor).strip().replace(",", ".")
    if not texto:
        return None
    try:
        numero = float(texto)
    except ValueError:
        return None
    return numero if numero > 0 else None


def a_coord(valor: Any) -> float | None:
    """Como a_float pero admitiendo negativos (longitudes al oeste de Greenwich)."""
    if valor is None:
        return None
    try:
        return float(str(valor).strip().replace(",", "."))
    except ValueError:
        return None


def slug(texto: str) -> str:
    base = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base or "sin-nombre"


def nombre_natural(texto: str) -> str:
    """'Palmas de Gran Canaria (Las)' -> 'Las Palmas de Gran Canaria'."""
    coincidencia = re.match(r"^(.*?)\s*\((El|La|Los|Las|A|O|As|Os|Es|Sa|L')\)$", texto.strip())
    if coincidencia:
        cuerpo, articulo = coincidencia.groups()
        union = "" if articulo.endswith("'") else " "
        return f"{articulo}{union}{cuerpo}"
    return texto.strip()


def eur(valor: float, decimales: int = 3) -> str:
    return f"{valor:,.{decimales}f}".replace(",", "\u00a0").replace(".", ",")


def esc(texto: Any) -> str:
    return html.escape(str(texto or ""), quote=True)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


# --- Modelo ------------------------------------------------------------------

@dataclass(slots=True)
class Estacion:
    rotulo: str
    direccion: str
    cp: str
    horario: str
    lat: float
    lon: float
    precios: dict[str, float]

    @property
    def abierta_siempre(self) -> bool:
        return "24H" in self.horario.upper().replace(" ", "")


@dataclass(slots=True)
class Municipio:
    nombre: str
    slug: str
    provincia: str
    provincia_slug: str
    estaciones: list[Estacion] = field(default_factory=list)

    @property
    def ruta(self) -> str:
        return f"gasolineras/{self.provincia_slug}/{self.slug}/"

    @property
    def centro(self) -> tuple[float, float]:
        n = len(self.estaciones)
        return (sum(e.lat for e in self.estaciones) / n, sum(e.lon for e in self.estaciones) / n)

    def baratas(self, carburante: str) -> list[Estacion]:
        con_precio = [e for e in self.estaciones if carburante in e.precios]
        return sorted(con_precio, key=lambda e: e.precios[carburante])

    def media(self, carburante: str) -> float | None:
        valores = [e.precios[carburante] for e in self.estaciones if carburante in e.precios]
        return sum(valores) / len(valores) if valores else None


# --- Descarga ----------------------------------------------------------------

def descargar(intentos: int = 4) -> tuple[list[dict], str]:
    """Descarga el dataset, reintentando ante fallos transitorios del origen.

    El servicio del Ministerio responde a veces con un 200 y una lista vacía, o corta la
    conexión. Son huecos que duran poco, así que conviene insistir antes de rendirse: con
    un despliegue automático cada pocas horas, rendirse a la primera llena el buzón de
    avisos de fallo por algo que se arregla solo.
    """
    espera = 10
    ultimo_error: Exception | None = None

    for intento in range(1, intentos + 1):
        try:
            log.info("Descargando dataset del Ministerio (intento %d de %d)…", intento, intentos)
            with httpx.Client(timeout=120, follow_redirects=True) as cliente:
                respuesta = cliente.get(ORIGEN_URL, headers={"Accept": "application/json"})
                respuesta.raise_for_status()
                datos = respuesta.json()

            registros = datos.get("ListaEESSPrecio") or []
            if not registros:
                raise ValueError("el origen ha devuelto una lista vacía")

            log.info("Recibidos %d registros (fecha origen: %s)", len(registros), datos.get("Fecha"))
            return registros, str(datos.get("Fecha", "")).strip()

        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            ultimo_error = exc
            if intento == intentos:
                break
            log.warning("Intento %d fallido (%s). Se reintenta en %d s.", intento, exc, espera)
            time.sleep(espera)
            espera *= 3

    raise ValueError(
        f"El origen no ha dado datos válidos tras {intentos} intentos: {ultimo_error}"
    )


def agrupar(registros: Iterable[dict]) -> dict[str, Municipio]:
    municipios: dict[str, Municipio] = {}
    descartadas = 0

    for registro in registros:
        lat = a_coord(registro.get("Latitud"))
        lon = a_coord(registro.get("Longitud (WGS84)") or registro.get("Longitud"))
        nombre_mun = (registro.get("Municipio") or "").strip()
        nombre_prov = (registro.get("Provincia") or "").strip()
        if lat is None or lon is None or not nombre_mun or not nombre_prov:
            descartadas += 1
            continue

        precios = {
            clave: valor
            for clave, campo, _ in CARBURANTES
            if (valor := a_float(registro.get(campo))) is not None
        }
        if not precios:
            descartadas += 1
            continue

        mun_limpio = nombre_natural(nombre_mun)
        prov_limpia = nombre_natural(nombre_prov.title() if nombre_prov.isupper() else nombre_prov)
        clave = f"{slug(prov_limpia)}/{slug(mun_limpio)}"

        municipio = municipios.get(clave)
        if municipio is None:
            municipio = Municipio(
                nombre=mun_limpio,
                slug=slug(mun_limpio),
                provincia=prov_limpia,
                provincia_slug=slug(prov_limpia),
            )
            municipios[clave] = municipio

        municipio.estaciones.append(
            Estacion(
                rotulo=(registro.get("Rótulo") or "Sin rótulo").strip(),
                direccion=(registro.get("Dirección") or "").strip(),
                cp=str(registro.get("C.P.", "")).strip(),
                horario=(registro.get("Horario") or "").strip(),
                lat=lat,
                lon=lon,
                precios=precios,
            )
        )

    log.info("Agrupados %d municipios con estaciones (%d registros descartados)",
             len(municipios), descartadas)
    return municipios


# --- Plantillas --------------------------------------------------------------

def envoltura(*, titulo: str, descripcion: str, canonica: str, cuerpo: str,
              raiz: str, fecha: str, jsonld: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(titulo)}</title>
<meta name="description" content="{esc(descripcion)}">
<link rel="canonical" href="{esc(canonica)}">
<link rel="icon" href="{raiz}favicon.svg" type="image/svg+xml">
<meta property="og:title" content="{esc(titulo)}">
<meta property="og:description" content="{esc(descripcion)}">
<meta property="og:type" content="website">
<link rel="preload" href="{raiz}fuentes/Barlow-Regular.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="{raiz}fuentes/BarlowCondensed-Bold.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{raiz}estilos.css">
{jsonld}
</head>
<body>
<div class="envoltura">
<header class="cabecera">
  <a class="marca" href="{raiz}"><svg class="gota" viewBox="0 0 120 120" aria-hidden="true"><path d="M60 18c0 0 32 34 32 52a32 32 0 0 1-64 0c0-18 32-52 32-52z" fill="currentColor"/><text x="60" y="86" text-anchor="middle" font-family="var(--cond)" font-weight="700" font-size="42" fill="var(--papel)">€</text></svg><span class="txt">Rastre<span>oil</span></span></a>
  <div class="sello">Precios del {esc(fecha) or "Ministerio"}</div>
</header>
{cuerpo}
<footer class="pie">
  <p>Precios publicados por el Ministerio para la Transición Ecológica y el Reto Demográfico
  como datos abiertos. Se actualizan varias veces al día; confirma siempre el precio en el
  surtidor antes de repostar.</p>
  <p class="legales"><a href="{raiz}aviso-legal/">Aviso legal</a>
  · <a href="{raiz}privacidad/">Privacidad</a>
  · <a href="{raiz}cookies/">Cookies</a></p>
</footer>
</div>
</body>
</html>"""


def tabla_estaciones(estaciones: list[Estacion]) -> str:
    columnas = "".join(f"<th>{esc(etiqueta)}</th>" for _, _, etiqueta in CARBURANTES)
    filas = []
    for e in estaciones:
        celdas = "".join(
            f'<td class="p">{eur(e.precios[clave])}</td>' if clave in e.precios else '<td class="p vacia">—</td>'
            for clave, _, _ in CARBURANTES
        )
        mapa = f"https://www.google.com/maps/dir/?api=1&destination={e.lat},{e.lon}"
        horario = f'<span class="h">{esc(e.horario)}</span>' if e.horario else ""
        filas.append(
            f"<tr><td><b>{esc(e.rotulo)}</b>"
            f'<span class="d">{esc(e.direccion)}</span>{horario}'
            f'<a class="mapa" href="{mapa}" target="_blank" rel="noopener nofollow">Cómo llegar</a></td>'
            f"{celdas}</tr>"
        )
    return (f'<div class="tabla-marco"><table class="tabla">'
            f"<thead><tr><th>Estación</th>{columnas}</tr></thead>"
            f"<tbody>{''.join(filas)}</tbody></table></div>")


def jsonld_municipio(municipio: Municipio, url: str) -> str:
    elementos = [
        {
            "@type": "ListItem",
            "position": i,
            "item": {
                "@type": "GasStation",
                "name": e.rotulo,
                "address": {
                    "@type": "PostalAddress",
                    "streetAddress": e.direccion,
                    "addressLocality": municipio.nombre,
                    "postalCode": e.cp,
                    "addressCountry": "ES",
                },
                "geo": {"@type": "GeoCoordinates", "latitude": e.lat, "longitude": e.lon},
            },
        }
        for i, e in enumerate(municipio.baratas(PRINCIPAL)[:10], start=1)
    ]
    datos = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": f"Gasolineras en {municipio.nombre}",
        "url": url,
        "numberOfItems": len(municipio.estaciones),
        "itemListElement": elementos,
    }
    return f'<script type="application/ld+json">{json.dumps(datos, ensure_ascii=False)}</script>'


def pagina_municipio(municipio: Municipio, *, base: str, fecha: str,
                     media_provincial: dict[str, float], cercanos: list[Municipio]) -> str:
    url = f"{base}/{municipio.ruta}"
    total = len(municipio.estaciones)
    orden = municipio.baratas(PRINCIPAL)
    mejor = orden[0] if orden else None
    media_local = municipio.media(PRINCIPAL)

    # Titular: el dato concreto que diferencia esta página de las otras 5.000.
    if mejor:
        titulo = (f"Gasolineras baratas en {municipio.nombre}: "
                  f"gasolina 95 desde {eur(mejor.precios[PRINCIPAL])} €/l")
        descripcion = (f"Precios actualizados de las {total} gasolineras de {municipio.nombre} "
                       f"({municipio.provincia}). La más barata: {mejor.rotulo}, "
                       f"{eur(mejor.precios[PRINCIPAL])} €/l de gasolina 95.")
    else:
        titulo = f"Gasolineras en {municipio.nombre} ({municipio.provincia})"
        descripcion = f"Precios actualizados de las {total} gasolineras de {municipio.nombre}."

    destacados = []
    for clave, _, etiqueta in CARBURANTES:
        lista = municipio.baratas(clave)
        if not lista:
            continue
        e = lista[0]
        destacados.append(
            f'<li><span class="k">{esc(etiqueta)}</span>'
            f'<span class="v">{eur(e.precios[clave])}<small> €/l</small></span>'
            f'<span class="q">{esc(e.rotulo)}</span></li>'
        )

    contexto = ""
    prov_media = media_provincial.get(PRINCIPAL)
    if media_local and prov_media:
        diferencia = media_local - prov_media
        if abs(diferencia) >= 0.005:
            sentido = "por encima de" if diferencia > 0 else "por debajo de"
            contexto = (f" El precio medio de la gasolina 95 en el municipio es "
                        f"{eur(media_local)} €/l, {eur(abs(diferencia))} € {sentido} "
                        f"la media de {esc(municipio.provincia)}.")
        else:
            contexto = (f" El precio medio de la gasolina 95 en el municipio, "
                        f"{eur(media_local)} €/l, está en línea con la media provincial.")

    ahorro = ""
    if len(orden) > 1:
        brecha = orden[-1].precios[PRINCIPAL] - orden[0].precios[PRINCIPAL]
        if brecha >= 0.01:
            ahorro = (f" Entre la más barata y la más cara hay {eur(brecha)} € por litro, "
                      f"unos {eur(brecha * 50, 2)} € en un depósito de 50 litros.")

    veinticuatro = sum(1 for e in municipio.estaciones if e.abierta_siempre)
    if veinticuatro == 1:
        nota_horario = " Una de ellas abre 24 horas."
    elif veinticuatro > 1:
        nota_horario = f" {veinticuatro} de ellas abren 24 horas."
    else:
        nota_horario = ""

    enlaces_cerca = "".join(
        f'<li><a href="../../{m.provincia_slug}/{m.slug}/">{esc(m.nombre)}</a></li>'
        for m in cercanos
    )

    cuerpo = f"""
<nav class="miga">
  <a href="../../../">Inicio</a> · <a href="../">{esc(municipio.provincia)}</a>
</nav>

<h1>Gasolineras en {esc(municipio.nombre)}</h1>
<p class="entradilla">{esc(municipio.nombre)} tiene {total}
  {"estaciones de servicio" if total != 1 else "estación de servicio"} con precios
  publicados.{contexto}{ahorro}{nota_horario}</p>

{f'<ul class="destacados">{"".join(destacados)}</ul>' if destacados else ""}

<h2>Todas las gasolineras de {esc(municipio.nombre)}</h2>
<p class="nota">Ordenadas de más barata a más cara por el precio de la gasolina 95.
Un guion indica que la estación no vende ese carburante.</p>
{tabla_estaciones(orden or municipio.estaciones)}

{f'<h2>Municipios cercanos</h2><ul class="cerca">{enlaces_cerca}</ul>' if enlaces_cerca else ""}

<p class="cta"><a href="../../../">Buscar la gasolinera más barata cerca de mí</a></p>
"""
    return envoltura(
        titulo=titulo, descripcion=descripcion, canonica=url, cuerpo=cuerpo,
        raiz="../../../", fecha=fecha, jsonld=jsonld_municipio(municipio, url),
    )


def pagina_provincia(provincia: str, municipios: list[Municipio], *, base: str, fecha: str) -> str:
    total_eess = sum(len(m.estaciones) for m in municipios)
    precios = [p for m in municipios for e in m.estaciones if (p := e.precios.get(PRINCIPAL))]
    media = sum(precios) / len(precios) if precios else None

    mejor_mun, mejor_precio = None, None
    for m in municipios:
        lista = m.baratas(PRINCIPAL)
        if lista and (mejor_precio is None or lista[0].precios[PRINCIPAL] < mejor_precio):
            mejor_mun, mejor_precio = m, lista[0].precios[PRINCIPAL]

    filas = []
    for m in sorted(municipios, key=lambda m: m.nombre):
        lista = m.baratas(PRINCIPAL)
        desde = f"{eur(lista[0].precios[PRINCIPAL])} €/l" if lista else "—"
        filas.append(
            f'<li><a href="{m.slug}/">{esc(m.nombre)}</a>'
            f'<span class="n">{len(m.estaciones)}</span>'
            f'<span class="p">{desde}</span></li>'
        )
    filas = "".join(filas)

    resumen = f"{total_eess} gasolineras repartidas en {len(municipios)} municipios."
    if media:
        resumen += f" El precio medio de la gasolina 95 en la provincia es {eur(media)} €/l."
    if mejor_mun and mejor_precio:
        resumen += (f" El litro más barato está en {mejor_mun.nombre}, "
                    f"a {eur(mejor_precio)} €/l.")

    cuerpo = f"""
<nav class="miga"><a href="../../">Inicio</a></nav>
<h1>Gasolineras en la provincia de {esc(provincia)}</h1>
<p class="entradilla">{esc(resumen)}</p>
<h2>Municipios</h2>
<ul class="municipios"><li class="cab"><span>Municipio</span><span class="n">EESS</span><span class="p">Desde</span></li>{filas}</ul>
<p class="cta"><a href="../../">Buscar la gasolinera más barata cerca de mí</a></p>
"""
    return envoltura(
        titulo=f"Gasolineras en {provincia}: precios actualizados por municipio",
        descripcion=resumen,
        canonica=f"{base}/gasolineras/{slug(provincia)}/",
        cuerpo=cuerpo, raiz="../../", fecha=fecha,
    )


def pagina_portada(provincias: dict[str, list[Municipio]], *, base: str, fecha: str,
                   total_eess: int) -> str:
    filas = "".join(
        f'<li><a href="gasolineras/{slug(p)}/">{esc(p)}</a>'
        f'<span class="n">{sum(len(m.estaciones) for m in ms)}</span></li>'
        for p, ms in sorted(provincias.items())
    )
    cuerpo = f"""
<h1>Precios de las gasolineras de España</h1>
<p class="entradilla">{total_eess} estaciones de servicio con precios oficiales, ordenadas
por lo que cuesta llenar el depósito. Elige tu provincia, o usa el buscador para encontrar
la más barata dentro del radio que te compense.</p>
<p class="cta"><a href="app.html">Buscar cerca de mí</a></p>
<h2>Provincias</h2>
<ul class="municipios">{filas}</ul>
"""
    return envoltura(
        titulo="Rastreoil · Gasolineras baratas en España, precio actualizado",
        descripcion=(f"Precios oficiales de {total_eess} gasolineras de España. "
                     "Busca la más barata cerca de ti y calcula si compensa el desvío."),
        canonica=f"{base}/", cuerpo=cuerpo, raiz="", fecha=fecha,
    )


ESTILOS = """
@font-face{font-family:"Barlow";src:url("fuentes/Barlow-Regular.woff2") format("woff2");font-weight:400;font-style:normal;font-display:swap}
@font-face{font-family:"Barlow";src:url("fuentes/Barlow-Medium.woff2") format("woff2");font-weight:500;font-style:normal;font-display:swap}
@font-face{font-family:"Barlow";src:url("fuentes/Barlow-SemiBold.woff2") format("woff2");font-weight:600;font-style:normal;font-display:swap}
@font-face{font-family:"Barlow";src:url("fuentes/Barlow-Bold.woff2") format("woff2");font-weight:700;font-style:normal;font-display:swap}
@font-face{font-family:"Barlow Condensed";src:url("fuentes/BarlowCondensed-Medium.woff2") format("woff2");font-weight:500;font-style:normal;font-display:swap}
@font-face{font-family:"Barlow Condensed";src:url("fuentes/BarlowCondensed-SemiBold.woff2") format("woff2");font-weight:600;font-style:normal;font-display:swap}
@font-face{font-family:"Barlow Condensed";src:url("fuentes/BarlowCondensed-Bold.woff2") format("woff2");font-weight:700;font-style:normal;font-display:swap}
:root{--papel:#E7EBEE;--superficie:#fff;--tinta:#16232F;--tinta-suave:#5B6B7A;--linea:#CFD7DE;
--panel:#15202B;--verde:#0B7A4B;--ambar:#B9750A;--senal:#0B4FA8;
--sans:"Barlow",system-ui,sans-serif;--cond:"Barlow Condensed","Barlow",system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--papel:#0E1720;--superficie:#18242F;
--tinta:#E6EDF3;--tinta-suave:#93A4B3;--linea:#2B3A48;--verde:#3FBD84;--senal:#7FB0F0}}
*{box-sizing:border-box}
body{margin:0;background:var(--papel);color:var(--tinta);font-family:var(--sans);font-size:16px;line-height:1.5}
.envoltura{max-width:760px;margin:0 auto;padding:1rem 1rem 3rem}
.cabecera{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;
padding-bottom:.8rem;border-bottom:2px solid var(--tinta);margin-bottom:1.2rem}
.marca{font-family:var(--cond);font-weight:700;font-size:1.9rem;line-height:1;text-decoration:none;color:inherit}
.marca{display:flex;align-items:center;gap:.4rem}
.marca .gota{width:1.15em;height:1.15em;color:var(--verde);flex-shrink:0}
.marca .txt span{color:var(--verde)}
.sello{font-size:.76rem;color:var(--tinta-suave);text-align:right}
.miga{font-size:.82rem;color:var(--tinta-suave);margin-bottom:.8rem}
.miga a{color:var(--senal)}
h1{font-family:var(--cond);font-weight:700;font-size:2.4rem;line-height:1.05;margin:0 0 .6rem}
h2{font-family:var(--cond);font-weight:600;font-size:1.5rem;margin:2rem 0 .5rem}
.entradilla{font-size:1.05rem;max-width:62ch;margin:0 0 1.2rem}
.nota{font-size:.85rem;color:var(--tinta-suave);margin:.2rem 0 .8rem}
.destacados{list-style:none;margin:0;padding:0;display:grid;
grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.6rem}
.destacados li{background:var(--panel);color:#F6E7C8;border-radius:8px;padding:.7rem .8rem}
.destacados .k{display:block;font-size:.78rem;opacity:.7}
.destacados .v{display:block;font-family:var(--cond);font-weight:700;font-size:2.1rem;
line-height:1;font-variant-numeric:tabular-nums}
.destacados .v small{font-family:var(--sans);font-size:.8rem;font-weight:500;opacity:.7}
.destacados .q{display:block;font-size:.8rem;opacity:.75;margin-top:.2rem}
.tabla-marco{overflow-x:auto;border:1px solid var(--linea);border-radius:8px;background:var(--superficie)}
.tabla{border-collapse:collapse;width:100%;min-width:560px;font-size:.9rem}
.tabla th{text-align:left;font-weight:600;font-size:.78rem;color:var(--tinta-suave);
padding:.6rem .7rem;border-bottom:1px solid var(--linea);white-space:nowrap}
.tabla th:not(:first-child){text-align:right}
.tabla td{padding:.65rem .7rem;border-bottom:1px solid var(--linea);vertical-align:top}
.tabla tr:last-child td{border-bottom:0}
.tabla td.p{text-align:right;font-family:var(--cond);font-weight:600;font-size:1.25rem;
font-variant-numeric:tabular-nums;white-space:nowrap}
.tabla td.vacia{color:var(--tinta-suave);font-weight:400}
.tabla .d,.tabla .h{display:block;font-size:.78rem;color:var(--tinta-suave)}
.tabla .mapa{display:inline-block;margin-top:.25rem;font-size:.78rem;color:var(--senal)}
.tabla tbody tr:first-child td.p:nth-child(2){color:var(--verde)}
.municipios{list-style:none;margin:0;padding:0}
.municipios li{display:flex;align-items:baseline;gap:.75rem;padding:.5rem .2rem;
border-bottom:1px solid var(--linea)}
.municipios li a{flex:1;color:var(--senal);text-decoration:none}
.municipios li a:hover{text-decoration:underline}
.municipios .n,.municipios .p{font-variant-numeric:tabular-nums;color:var(--tinta-suave);font-size:.85rem}
.municipios .p{min-width:5.5rem;text-align:right}
.municipios .cab{font-size:.76rem;color:var(--tinta-suave);border-bottom:1px solid var(--tinta)}
.municipios .cab span:first-child{flex:1}
.cerca{list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:.4rem}
.cerca a{display:inline-block;border:1px solid var(--linea);border-radius:999px;
padding:.3rem .7rem;font-size:.88rem;color:var(--senal);text-decoration:none}
.cta{margin:2rem 0 0}
.cta a{display:inline-block;background:var(--senal);color:#fff;text-decoration:none;
font-weight:600;padding:.7rem 1.1rem;border-radius:8px}
.pie{margin-top:2.5rem;padding-top:1rem;border-top:1px solid var(--linea);
font-size:.78rem;color:var(--tinta-suave);max-width:62ch}
.pie .legales a{color:var(--senal)}
.legal{max-width:68ch}
.legal h2{font-size:1.3rem;margin:1.8rem 0 .4rem}
.legal p,.legal li{font-size:.95rem}
.legal ul{padding-left:1.1rem}
.legal table{border-collapse:collapse;width:100%;font-size:.85rem;margin:1rem 0;display:block;overflow-x:auto}
.legal th,.legal td{border:1px solid var(--linea);padding:.5rem .6rem;text-align:left;vertical-align:top}
.legal th{background:var(--superficie);font-weight:600}
.legal em{color:var(--tinta-suave)}
.legal a{color:var(--senal)}
a:focus-visible{outline:3px solid var(--ambar);outline-offset:2px}
"""


# --- Generación --------------------------------------------------------------

def escribir(ruta: Path, contenido: str) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")


def generar_datos(por_provincia: dict[str, list[Municipio]], salida: Path, fecha: str) -> None:
    """Vuelca un JSON compacto por provincia para que el buscador funcione sin servidor.

    Formato posicional (no objetos) para reducir el peso: con ~12.000 estaciones en toda
    España, cada provincia queda en decenas de KB y el navegador solo descarga las que
    toca el radio de búsqueda.
    """
    claves = [clave for clave, _, _ in CARBURANTES]
    indice = []

    for provincia, municipios in por_provincia.items():
        filas = []
        lats, lons = [], []
        for municipio in municipios:
            for e in municipio.estaciones:
                lats.append(e.lat)
                lons.append(e.lon)
                filas.append([
                    e.rotulo,
                    e.direccion,
                    municipio.nombre,
                    e.horario,
                    round(e.lat, 5),
                    round(e.lon, 5),
                    [e.precios.get(c) for c in claves],
                ])

        escribir(
            salida / "datos" / f"{slug(provincia)}.json",
            json.dumps({"fecha": fecha, "campos": claves, "e": filas},
                       ensure_ascii=False, separators=(",", ":")),
        )
        indice.append({
            "s": slug(provincia),
            "n": provincia,
            "bb": [round(min(lats), 4), round(max(lats), 4),
                   round(min(lons), 4), round(max(lons), 4)],
        })

    escribir(
        salida / "datos" / "indice.json",
        json.dumps({"fecha": fecha, "campos": claves, "provincias": indice},
                   ensure_ascii=False, separators=(",", ":")),
    )
    log.info("Volcados %d ficheros de datos para el buscador", len(indice) + 1)


def municipios_cercanos(objetivo: Municipio, candidatos: list[Municipio], n: int = 8) -> list[Municipio]:
    lat, lon = objetivo.centro
    distancias = []
    for m in candidatos:
        if m is objetivo:
            continue
        mlat, mlon = m.centro
        distancias.append((haversine_km(lat, lon, mlat, mlon), m))
    distancias.sort(key=lambda par: par[0])
    return [m for _, m in distancias[:n]]


def generar_sitemaps(rutas: list[str], base: str, salida: Path, ahora: str) -> None:
    tramos = [rutas[i:i + URLS_POR_SITEMAP] for i in range(0, len(rutas), URLS_POR_SITEMAP)]
    nombres = []
    for indice, tramo in enumerate(tramos, start=1):
        nombre = "sitemap.xml" if len(tramos) == 1 else f"sitemap-{indice}.xml"
        urls = "".join(
            f"<url><loc>{esc(base)}/{esc(r)}</loc><lastmod>{ahora}</lastmod></url>" for r in tramo
        )
        escribir(salida / nombre,
                 '<?xml version="1.0" encoding="UTF-8"?>'
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 f"{urls}</urlset>")
        nombres.append(nombre)

    if len(tramos) > 1:
        entradas = "".join(
            f"<sitemap><loc>{esc(base)}/{n}</loc><lastmod>{ahora}</lastmod></sitemap>"
            for n in nombres
        )
        escribir(salida / "sitemap.xml",
                 '<?xml version="1.0" encoding="UTF-8"?>'
                 '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 f"{entradas}</sitemapindex>")


def generar_legales(salida: Path, base: str, fecha: str, permitir_huecos: bool) -> list[str]:
    """Convierte los markdown de `legales/` en páginas del sitio.

    Los datos identificativos viven en `legales/datos.json` y se sustituyen aquí, para no
    repetir el NIF en tres documentos. Si alguno está vacío, la generación se detiene: un
    aviso legal sin titular incumple el artículo 10 de la LSSI-CE.
    """
    carpeta = Path(__file__).resolve().parent / "legales"
    if not carpeta.is_dir():
        log.warning("No existe %s; el sitio se publicará sin páginas legales.", carpeta)
        return []

    try:
        datos = json.loads((carpeta / "datos.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se ha podido leer legales/datos.json: {exc}") from exc

    vacios = [clave for clave, valor in datos.items() if not str(valor).strip()]
    if vacios:
        mensaje = ("Faltan datos en legales/datos.json: " + ", ".join(sorted(vacios)) +
                   ". Sin ellos el aviso legal no cumple el artículo 10 de la LSSI-CE.")
        if not permitir_huecos:
            raise ValueError(mensaje + " Usa --permitir-huecos para generar igualmente en local.")
        log.warning("%s Se generará con marcadores visibles.", mensaje)

    try:
        import markdown as md
    except ImportError as exc:
        raise ValueError("Falta la dependencia 'markdown' (pip install markdown).") from exc

    rutas = []
    for fichero in sorted(carpeta.glob("*.md")):
        texto = fichero.read_text(encoding="utf-8")
        for clave, valor in datos.items():
            texto = texto.replace("{{" + clave + "}}", str(valor).strip() or f"[{clave} pendiente]")

        titulo = texto.lstrip().split("\n", 1)[0].lstrip("# ").strip()
        cuerpo = md.markdown(texto, extensions=["tables"])
        ruta = f"{fichero.stem}/"
        escribir(
            salida / fichero.stem / "index.html",
            envoltura(
                titulo=f"{titulo} · Rastreoil",
                descripcion=f"{titulo} de Rastreoil, buscador de gasolineras baratas en España.",
                canonica=f"{base}/{ruta}",
                cuerpo=f'<article class="legal">{cuerpo}</article>',
                raiz="../", fecha=fecha,
            ),
        )
        rutas.append(ruta)

    log.info("Generadas %d páginas legales", len(rutas))
    return rutas


def construir(salida: Path, base: str, permitir_huecos: bool = False) -> None:
    base = base.rstrip("/")
    registros, fecha = descargar()
    municipios = agrupar(registros)
    if not municipios:
        raise ValueError("No se ha podido agrupar ningún municipio.")

    por_provincia: dict[str, list[Municipio]] = defaultdict(list)
    for municipio in municipios.values():
        por_provincia[municipio.provincia].append(municipio)

    if salida.exists():
        shutil.rmtree(salida)
    salida.mkdir(parents=True)
    escribir(salida / "estilos.css", ESTILOS)

    # Tipografías autoalojadas: el sitio no hace ninguna petición a terceros.
    fuentes = Path(__file__).resolve().parent.parent / "frontend" / "fuentes"
    if fuentes.is_dir():
        shutil.copytree(fuentes, salida / "fuentes")
    icono = fuentes.parent / "favicon.svg"
    if icono.is_file():
        shutil.copy(icono, salida / "favicon.svg")
    else:
        log.warning("No se ha encontrado %s; las páginas caerán a la tipografía del sistema.", fuentes)

    ahora = datetime.now(timezone.utc).date().isoformat()
    rutas = [""]
    total_eess = sum(len(m.estaciones) for m in municipios.values())

    for provincia, lista in por_provincia.items():
        precios_prov = defaultdict(list)
        for m in lista:
            for e in m.estaciones:
                for clave, valor in e.precios.items():
                    precios_prov[clave].append(valor)
        media_provincial = {k: sum(v) / len(v) for k, v in precios_prov.items() if v}

        escribir(salida / "gasolineras" / slug(provincia) / "index.html",
                 pagina_provincia(provincia, lista, base=base, fecha=fecha))
        rutas.append(f"gasolineras/{slug(provincia)}/")

        for municipio in lista:
            cercanos = municipios_cercanos(municipio, lista)
            escribir(salida / municipio.ruta / "index.html",
                     pagina_municipio(municipio, base=base, fecha=fecha,
                                      media_provincial=media_provincial, cercanos=cercanos))
            rutas.append(municipio.ruta)

    escribir(salida / "index.html",
             pagina_portada(por_provincia, base=base, fecha=fecha, total_eess=total_eess))

    generar_datos(por_provincia, salida, fecha)

    # El buscador por radio se publica junto a las páginas estáticas.
    buscador = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
    if buscador.is_file():
        shutil.copy(buscador, salida / "app.html")
        rutas.append("app.html")
    else:
        log.warning("No se ha encontrado %s; la portada enlazará a una página inexistente.", buscador)

    rutas.extend(generar_legales(salida, base, fecha, permitir_huecos))

    generar_sitemaps(rutas, base, salida, ahora)
    escribir(salida / "robots.txt",
             f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n")

    # GitHub Pages sirve este fichero ante cualquier ruta inexistente.
    escribir(
        salida / "404.html",
        envoltura(
            titulo="Página no encontrada · Rastreoil",
            descripcion="La página que buscas no existe o ha cambiado de dirección.",
            canonica=f"{base}/404.html", raiz="/", fecha=fecha,
            cuerpo="""
<h1>Aquí no hay nada</h1>
<p class="entradilla">La dirección que has abierto no existe, o el municipio que buscabas
ya no tiene estaciones con precios publicados.</p>
<p class="cta"><a href="/">Buscar gasolineras cerca de mí</a></p>
""",
        ),
    )

    # GitHub Pages sirve el sitio en el dominio que indique este fichero.
    dominio = base.split("//", 1)[-1].split("/", 1)[0]
    if dominio and not dominio.endswith("github.io"):
        escribir(salida / "CNAME", dominio + "\n")

    log.info("Generadas %d páginas en %s", len(rutas), salida)


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera las páginas estáticas de Rastreoil.")
    parser.add_argument("--salida", default="dist", type=Path)
    parser.add_argument("--base", default="https://rastreoil.es",
                        help="URL base del sitio, sin barra final.")
    parser.add_argument("--permitir-huecos", action="store_true",
                        help="Genera aunque falten datos del titular en legales/datos.json. "
                             "Solo para pruebas en local; nunca en el despliegue real.")
    args = parser.parse_args()

    try:
        construir(args.salida, args.base, args.permitir_huecos)
    except httpx.HTTPError as exc:
        log.error("No se ha podido descargar el dataset: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        log.error("Generación abortada: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
