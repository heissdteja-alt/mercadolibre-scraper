# Scraper Mercado Libre

Scraper de resultados de búsqueda de Mercado Libre (Playwright + Chrome real +
cookies de sesion), con parser sobre el JSON que Mercado Libre embebe en la
pagina (no HTML fragil) y un fallback sobre el DOM ya renderizado si ese JSON
no aparece. Extrae nombre, precio de oferta, precio normal, descuento,
disponibilidad, ventas, calificacion, reseñas, marca, imagen y URL — y lo
guarda en un CSV.

## Por que esta arquitectura

Mercado Libre transmite los productos por streaming *despues* del HTML inicial,
asi que un `requests.get()` normal es poco confiable (a veces trae los datos
completos, a veces no). Un navegador real esperando a que termine el stream
siempre ve los productos ya cargados. El parser principal busca directamente el
JSON `product_list` que ML embebe en un `<script>` (mismos datos que usa el
propio sitio, mas limpios y completos que parsear tarjetas HTML); si por algun
motivo no aparece, cae a un segundo extractor que lee el DOM ya renderizado.

## Como funciona

1. Carga tus cookies de sesion (ver [Cookies](#cookies) abajo) — sin sesion
   iniciada, Mercado Libre muestra un muro de login en los resultados.
2. Abre Chrome (headless por defecto) en modo stealth, con esas cookies inyectadas.
3. Navega a `listado.mercadolibre.<dominio>/<termino>` y espera a que termine
   el streaming de productos (`.andes-money-amount__fraction` visible).
4. Extrae el JSON embebido `product_list`; si no esta, usa el fallback de DOM.
5. Avanza pagina por pagina con el boton "Siguiente" real del sitio, pausas
   aleatorias entre paginas, y reintentos ante timeouts/errores de red.
6. Si detecta el muro de login (cookies vencidas o invalidas), se detiene y te
   dice que borres `_ml_cookies.json` para volver a pegarlas.
7. Guarda todo en un CSV con separador `;` en `salidas/`.

## Instalacion

```bash
pip install -r requirements.txt
playwright install chromium
```

## Uso

```bash
python scraper_ml_pro_1.py
```

Primer uso: pide pegar tus cookies (ver abajo), luego el pais, el termino de
busqueda y cuantos productos (Enter = 50). El CSV queda en
`salidas/<termino><fecha>.csv`.

## Cookies

Mercado Libre requiere sesion iniciada para no mostrar el muro de login en los
resultados de busqueda.

1. Abre Chrome → `mercadolibre.com.mx` (o el dominio de tu pais) con tu sesion
   iniciada.
2. F12 → pestaña Network → recarga con F5.
3. Filtra por `mercadolibre.com` → clic en el primer request.
4. En Request Headers, busca la linea `cookie:` → clic derecho → **Copy value**.
5. Pegala cuando el script te la pida.

Se guardan en `_ml_cookies.json` (en la carpeta del proyecto — **nunca se sube a
git**, ver `.gitignore`) y se reusan automaticamente por `DIAS_CACHE` dias
(20 por default). El script avisa cuando estan por vencer y te las vuelve a
pedir. Si ves el muro de login antes de esos 20 dias, borra el archivo y
vuelve a correr el script para pegarlas de nuevo.

**Importante:** ese archivo contiene tu cookie de sesion real — es equivalente
a tener tu sesion iniciada. No lo compartas ni lo subas a ningun repositorio.

## Configuracion

Editables al inicio de `scraper_ml_pro_1.py`:

| Variable | Default | Que hace |
|---|---|---|
| `CARPETA_CSV` | `./salidas` | Donde se guardan los CSV |
| `COOKIES_FILE` | `./_ml_cookies.json` | Donde se guarda la cookie pegada |
| `DIAS_CACHE` | `20` | Cuantos dias se reusa la cookie antes de pedir una nueva |
| `HEADLESS` | `True` | Poner en `False` para ver el navegador si algo falla |
| `MAX_REINTENTOS` | `3` | Reintentos por pagina ante error transitorio |
| `PAUSA_MIN` / `PAUSA_MAX` | `1.5` / `3.5` | Rango de pausa aleatoria entre paginas (segundos) |

Paises soportados (se elige al correr el script): Mexico, Argentina, Colombia,
Chile, Peru, España.
Autor: Pablo Teja
## Notas sobre el parser

`extraer_json_embebido` busca el `<script>` que contiene `product_list` +
`optimizedResponse` y lo decodifica (viene como JSON escapado dentro de un
`q("...")`). Fue validado contra HTML real (50/50 productos correctos). Si
Mercado Libre cambia la forma de embeber ese JSON, el fallback de DOM
(`parsear_desde_dom`) sigue funcionando con menos campos (sin ventas, sin
reseñas, sin marca) porque lee directamente los elementos visibles renderizados.
