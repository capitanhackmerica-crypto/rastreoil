# Rastreoil

Buscador de estaciones de servicio por cercanía y precio, sobre los datos abiertos
de precios de carburantes del Ministerio para la Transición Ecológica y el Reto Demográfico.

## Puesta en marcha

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Abrir http://127.0.0.1:8000

## Cómo funciona

- El backend descarga el listado completo de EESS de España (≈12.000 registros) y lo
  mantiene en memoria durante 30 minutos, que es la cadencia de actualización del origen.
  Si el origen falla, se siguen sirviendo los últimos datos válidos.
- La búsqueda prefiltra por caja envolvente y calcula la distancia real con Haversine.
- Tres criterios de orden:
  - `distancia`: la más cercana.
  - `precio`: la más barata por litro del radio.
  - `coste`: precio del repostaje más el carburante consumido en el desvío (ida y vuelta).
    Es el criterio por defecto, porque desviarse 12 km para ahorrar 2 céntimos sale caro.

## Endpoint

`GET /api/estaciones?lat=&lon=&radio_km=&producto=&orden=&litros=&consumo=`

Productos: `gasolina95`, `gasolina98`, `diesel`, `diesel_premium`, `glp`, `gnc`.

## Configuración por entorno

| Variable | Por defecto |
|---|---|
| `RASTREOIL_ORIGEN_URL` | endpoint del Ministerio |
| `RASTREOIL_CACHE_TTL` | 1800 (segundos) |
| `RASTREOIL_TIMEOUT` | 30 (segundos) |
| `RASTREOIL_HOST` / `RASTREOIL_PORT` | 127.0.0.1 / 8000 |

No hay credenciales: el origen es público y sin clave.

## Datos que salen de nuestros sistemas

- Hacia `sedeaplicaciones.minetur.gob.es`: ninguno. Se descarga el dataset completo,
  sin enviar la posición del usuario.
- Hacia `google.com/maps`: solo cuando el usuario pulsa "Cómo llegar".
- La posición del usuario no se envía a ningún tercero ni se persiste: viaja del navegador
  a nuestro backend, se usa para calcular distancias y no se guarda. Conviene dejarlo escrito
  en la política de privacidad antes de publicar.

## Generador de páginas estáticas

```bash
cd generador
pip install -r requirements.txt
python build.py --salida ../dist --base https://tu-dominio.es
```

Produce los datos del buscador (`datos/indice.json` y un JSON por provincia), una página por municipio con estaciones (`/gasolineras/<provincia>/<municipio>/`),
una por provincia, portada, `sitemap.xml`, `robots.txt` y datos estructurados `ItemList`
de schema.org. Copia también el buscador como `app.html`.

Cada página incluye datos que varían de un municipio a otro (precio mínimo por carburante,
media local frente a la provincial, diferencia entre la más barata y la más cara, número de
EESS abiertas 24 h) para no publicar miles de páginas prácticamente idénticas, que es lo que
penaliza Google en este tipo de directorios.

El workflow `.github/workflows/publicar.yml` regenera y despliega en GitHub Pages cada tres
horas, y aborta el despliegue si la generación sale sospechosamente pequeña, para que un
fallo del origen no tumbe el sitio. Define la variable de repositorio `URL_BASE` con el
dominio real antes del primer despliegue.

## Tipografías

Barlow y Barlow Condensed van autoalojadas en `frontend/fuentes/`, en woff2 con subconjunto
latino: 90 KB para las siete caras que usa el sitio. El build las copia a `dist/fuentes/`.
Así el sitio no hace ninguna petición a terceros durante la navegación, lo que simplifica
bastante la política de privacidad. La licencia SIL OFL va junto a los ficheros; no la quites.

Para regenerarlas con otros pesos o subconjuntos hace falta `fonttools[woff]` y `pyftsubset`.

## Alojamiento

El sitio se publica en **GitHub Pages**. Nominalia interviene solo como registrador del
dominio `rastreoil.com`: los DNS apuntan a GitHub y ningún dato de los visitantes pasa por
Nominalia. Esto es lo que declara la política de privacidad, así que si algún día cambias de
alojamiento hay que actualizar `legales/datos.json`.

## Páginas legales

Los textos viven en `generador/legales/` como markdown, y los datos identificativos en
`generador/legales/datos.json`. El build los sustituye y produce `/aviso-legal/`,
`/privacidad/` y `/cookies/`, enlazadas desde el pie de todas las páginas.

Los textos de `generador/legales/` son la única versión válida: no mantengas copias sueltas
en Word o markdown por ahí, porque se desincronizan y acabas publicando una y archivando otra.

**Si falta algún dato del titular, el build falla.** Es deliberado: un aviso legal sin
titular incumple el artículo 10 de la LSSI-CE, y es preferible que no se despliegue nada a
que se publique un documento con huecos. Para trabajar en local antes de tenerlos, usa
`--permitir-huecos`.

Ten en cuenta que esos datos serán públicos en cualquier caso, porque la ley obliga a
mostrarlos en el sitio. Si el repositorio es público, no añades exposición.

## Dos formas de desplegarlo

**Estático, sin servidor (recomendado para publicar).** El build vuelca `datos/indice.json`
y un JSON compacto por provincia. El buscador descarga solo las provincias cuyo recuadro
toca el radio de búsqueda —normalmente una o dos, decenas de KB— y calcula distancias y
coste en el propio navegador. Todo el sitio cabe en GitHub Pages, Cloudflare Pages o
Netlify: cero coste, cero mantenimiento y la posición del usuario no sale del dispositivo.

**Con backend.** `backend/main.py` sigue siendo útil si en algún momento quieres precios al
minuto, filtros que no caben en el cliente o una API para terceros. El frontend prueba
primero el backend y cae al modo estático si no responde, así que el mismo HTML sirve para
las dos formas sin tocar nada.

## Pendiente antes de publicar

- Decidir si añadir un fichero LICENSE. Sin él, en un repositorio público el código queda
  con todos los derechos reservados: se puede leer, pero nadie puede reutilizarlo.
- Registrar el dominio y darlo de alta en Search Console para enviar el sitemap.
- Vista de mapa, favoritos y avisos de bajada de precio.
