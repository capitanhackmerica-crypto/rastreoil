"""
Rastreoil - API de búsqueda de estaciones de servicio por cercanía y precio.

Fuente de datos: Ministerio para la Transición Ecológica y el Reto Demográfico.
https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/PreciosCarburantes/EstacionesTerrestres/

Datos abiertos, sin clave de API. Se refrescan cada ~30 minutos en origen,
por lo que la caché local usa el mismo TTL.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rastreoil")

# --- Configuración (nada hardcodeado que sea secreto; todo por entorno) -------

ORIGEN_URL = os.getenv(
    "RASTREOIL_ORIGEN_URL",
    "https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes"
    "/PreciosCarburantes/EstacionesTerrestres/",
)
CACHE_TTL_SEG = int(os.getenv("RASTREOIL_CACHE_TTL", "1800"))
TIMEOUT_SEG = float(os.getenv("RASTREOIL_TIMEOUT", "30"))
FRONTEND_DIR = Path(os.getenv("RASTREOIL_FRONTEND_DIR", "../frontend")).resolve()

RADIO_TIERRA_KM = 6371.0088

# Clave interna -> nombre del campo en el JSON del Ministerio.
PRODUCTOS: dict[str, str] = {
    "gasolina95": "Precio Gasolina 95 E5",
    "gasolina98": "Precio Gasolina 98 E5",
    "diesel": "Precio Gasoleo A",
    "diesel_premium": "Precio Gasoleo Premium",
    "glp": "Precio Gases licuados del petróleo",
    "gnc": "Precio Gas Natural Comprimido",
}

# El JSON ha cambiado de nombres de campo alguna vez; se aceptan variantes.
ALIAS_LATITUD = ("Latitud", "latitud")
ALIAS_LONGITUD = ("Longitud (WGS84)", "Longitud", "longitud")


# --- Modelo ------------------------------------------------------------------

@dataclass(slots=True)
class Estacion:
    id: str
    rotulo: str
    direccion: str
    municipio: str
    provincia: str
    cp: str
    horario: str
    lat: float
    lon: float
    precios: dict[str, float]


@dataclass(slots=True)
class Resultado:
    estacion: Estacion
    distancia_km: float
    precio: float
    coste_total: float


# --- Utilidades --------------------------------------------------------------

def a_float(valor: Any) -> float | None:
    """El Ministerio publica decimales con coma y cadenas vacías para 'no vende'."""
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


def primer_campo(registro: dict, claves: tuple[str, ...]) -> Any:
    for clave in claves:
        if clave in registro:
            return registro[clave]
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * RADIO_TIERRA_KM * math.asin(math.sqrt(a))


# --- Caché del dataset -------------------------------------------------------

class CacheEstaciones:
    """Mantiene en memoria el listado completo. Un único refresco concurrente."""

    def __init__(self) -> None:
        self._estaciones: list[Estacion] = []
        self._fecha_origen: str = ""
        self._cargado_en: float = 0.0
        self._lock = threading.Lock()

    @property
    def caducada(self) -> bool:
        return (time.monotonic() - self._cargado_en) > CACHE_TTL_SEG

    def obtener(self) -> tuple[list[Estacion], str]:
        if self._estaciones and not self.caducada:
            return self._estaciones, self._fecha_origen

        with self._lock:
            # Otro hilo pudo refrescar mientras esperábamos.
            if self._estaciones and not self.caducada:
                return self._estaciones, self._fecha_origen
            try:
                self._refrescar()
            except Exception as exc:  # noqa: BLE001 - se degrada a datos antiguos
                log.error("Fallo al refrescar el origen: %s", exc)
                if not self._estaciones:
                    raise HTTPException(
                        status_code=503,
                        detail="No se ha podido consultar el origen de datos del Ministerio.",
                    ) from exc
                log.warning("Se sirven datos cacheados del %s", self._fecha_origen)
        return self._estaciones, self._fecha_origen

    def _refrescar(self) -> None:
        log.info("Descargando dataset desde %s", ORIGEN_URL)
        with httpx.Client(timeout=TIMEOUT_SEG, follow_redirects=True) as cliente:
            respuesta = cliente.get(ORIGEN_URL, headers={"Accept": "application/json"})
            respuesta.raise_for_status()
            datos = respuesta.json()

        crudas = datos.get("ListaEESSPrecio") or []
        if not crudas:
            raise ValueError("El origen ha devuelto una lista vacía de estaciones.")

        estaciones: list[Estacion] = []
        for registro in crudas:
            lat = a_float(primer_campo(registro, ALIAS_LATITUD))
            lon = a_float(primer_campo(registro, ALIAS_LONGITUD))
            # a_float descarta los <= 0, y hay longitudes negativas legítimas en España.
            if lat is None:
                continue
            if lon is None:
                bruto = primer_campo(registro, ALIAS_LONGITUD)
                try:
                    lon = float(str(bruto).strip().replace(",", "."))
                except (TypeError, ValueError):
                    continue

            precios = {
                clave: valor
                for clave, campo in PRODUCTOS.items()
                if (valor := a_float(registro.get(campo))) is not None
            }
            if not precios:
                continue

            estaciones.append(
                Estacion(
                    id=str(registro.get("IDEESS", "")),
                    rotulo=(registro.get("Rótulo") or "Sin rótulo").strip(),
                    direccion=(registro.get("Dirección") or "").strip(),
                    municipio=(registro.get("Municipio") or "").strip(),
                    provincia=(registro.get("Provincia") or "").strip(),
                    cp=str(registro.get("C.P.", "")).strip(),
                    horario=(registro.get("Horario") or "").strip(),
                    lat=lat,
                    lon=lon,
                    precios=precios,
                )
            )

        self._estaciones = estaciones
        self._fecha_origen = str(datos.get("Fecha", "")).strip()
        self._cargado_en = time.monotonic()
        log.info("Cargadas %d estaciones (fecha origen: %s)", len(estaciones), self._fecha_origen)


cache = CacheEstaciones()


# --- API ---------------------------------------------------------------------

app = FastAPI(title="Rastreoil", version="0.1.0")


@app.get("/api/salud")
def salud() -> dict:
    return {"estado": "ok", "productos": list(PRODUCTOS)}


@app.get("/api/estaciones")
def buscar(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radio_km: float = Query(10, gt=0, le=200),
    producto: str = Query("gasolina95"),
    orden: str = Query("coste", pattern="^(distancia|precio|coste)$"),
    litros: float = Query(40, gt=0, le=200),
    consumo: float = Query(6.5, gt=0, le=40, description="Litros/100 km del vehículo"),
    limite: int = Query(25, gt=0, le=100),
) -> dict:
    """Devuelve las EESS del radio indicado que venden el producto, ordenadas.

    - distancia: la más cercana primero.
    - precio: la más barata por litro.
    - coste: precio del repostaje más el carburante gastado en el desvío (ida y vuelta).
    """
    if producto not in PRODUCTOS:
        raise HTTPException(status_code=400, detail=f"Producto no válido: {producto}")

    estaciones, fecha_origen = cache.obtener()

    # Prefiltro por caja envolvente: evita calcular haversine sobre ~12.000 registros.
    delta_lat = radio_km / 111.32
    coseno = max(math.cos(math.radians(lat)), 0.01)
    delta_lon = radio_km / (111.32 * coseno)

    resultados: list[Resultado] = []
    for estacion in estaciones:
        if producto not in estacion.precios:
            continue
        if abs(estacion.lat - lat) > delta_lat or abs(estacion.lon - lon) > delta_lon:
            continue
        distancia = haversine_km(lat, lon, estacion.lat, estacion.lon)
        if distancia > radio_km:
            continue
        precio = estacion.precios[producto]
        litros_desvio = (2 * distancia) * consumo / 100
        resultados.append(
            Resultado(
                estacion=estacion,
                distancia_km=round(distancia, 2),
                precio=precio,
                coste_total=round(precio * (litros + litros_desvio), 2),
            )
        )

    claves = {
        "distancia": lambda r: (r.distancia_km, r.precio),
        "precio": lambda r: (r.precio, r.distancia_km),
        "coste": lambda r: (r.coste_total, r.distancia_km),
    }
    resultados.sort(key=claves[orden])

    precios = [r.precio for r in resultados]
    return {
        "fecha_origen": fecha_origen,
        "total_encontradas": len(resultados),
        "precio_medio": round(sum(precios) / len(precios), 3) if precios else None,
        "parametros": {
            "producto": producto,
            "radio_km": radio_km,
            "orden": orden,
            "litros": litros,
            "consumo": consumo,
        },
        "resultados": [
            {
                **asdict(r.estacion),
                "distancia_km": r.distancia_km,
                "precio": r.precio,
                "coste_total": r.coste_total,
            }
            for r in resultados[:limite]
        ],
    }


# --- Frontend estático -------------------------------------------------------

if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def inicio() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("RASTREOIL_HOST", "127.0.0.1"),
        port=int(os.getenv("RASTREOIL_PORT", "8000")),
    )
