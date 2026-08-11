"""
═══════════════════════════════════════════════════════════════════════════
  SCRAPER MERCADO LIBRE — versión final (Playwright + parser JSON validado)
═══════════════════════════════════════════════════════════════════════════

  Por qué esta versión:
    ML transmite los productos por streaming DESPUÉS del HTML inicial, así que
    'requests' no es confiable (a veces trae los datos, a veces no). Un navegador
    real espera a que termine el stream → siempre ve los productos.

    • Tu Chrome real + stealth + tus cookies  → pasa el anti-bot de ML
    • Espera a que rendericen los productos    → nunca llega vacío
    • Extrae el JSON embebido (parser probado)  → datos limpios y completos
    • Paginación por el botón "Siguiente" real  → avanza de verdad

  Instalación (una vez):
      pip install playwright pandas beautifulsoup4
      playwright install chromium

  Uso:
      python scraper_ml.py

  Cookies: pegas una vez, se guardan. Solo re-pegas cuando expiran (~3 semanas).
═══════════════════════════════════════════════════════════════════════════
"""

import os
import re
import sys
import json
import time
import random
from datetime import datetime

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─── CONFIGURACIÓN ──────────────────────────────────────────────────────────
_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CARPETA_CSV  = os.path.join(_BASE_DIR, "salidas")
COOKIES_FILE = os.path.join(_BASE_DIR, "_ml_cookies.json")
DIAS_CACHE   = 20
HEADLESS     = True     # True = invisible. Si algún día falla, ponlo en False para ver qué pasa.
MAX_REINTENTOS = 3     # reintentos por página ante errores temporales
PAUSA_MIN    = 1.5     # pausa mínima entre páginas (segundos)
PAUSA_MAX    = 3.5     # pausa máxima entre páginas (segundos)
# ─────────────────────────────────────────────────────────────────────────────

PAISES = {
    "1": ("México",    "com.mx"),
    "2": ("Argentina", "com.ar"),
    "3": ("Colombia",  "com.co"),
    "4": ("Chile",     "cl"),
    "5": ("Perú",      "com.pe"),
    "6": ("España",    "es"),
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['es-MX','es','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = { runtime: {} };
"""


# ─── COOKIES ─────────────────────────────────────────────────────────────────
def parsear_cookie_dict(raw):
    d = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def cookies_para_playwright(raw, dominio):
    out = []
    for k, v in parsear_cookie_dict(raw).items():
        out.append({"name": k, "value": v,
                    "domain": f".mercadolibre.{dominio}", "path": "/"})
    return out


def pedir_cookies():
    print("\n" + "═" * 55)
    print("  🔑 PEGA TUS COOKIES (una vez cada ~3 semanas)")
    print("═" * 55)
    print("""
  1. Abre Chrome → mercadolibre.com.mx (con sesión iniciada)
  2. F12 → Network → recarga con F5
  3. Filtro: mercadolibre.com → clic en el primer request
  4. Request Headers → línea 'cookie:' → clic derecho → Copy value
  5. Pégalo aquí y ENTER
""")
    raw = input("  Cookie: ").strip()
    if not raw or "=" not in raw:
        print("  ❌ Cookie inválido.")
        sys.exit(1)
    os.makedirs(os.path.dirname(COOKIES_FILE), exist_ok=True)
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump({"raw": raw, "guardadas": datetime.now().isoformat()}, f, indent=2)
    print("  ✓ Cookies guardadas\n")
    return raw


def cargar_cookies_raw():
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        dias = (datetime.now() - datetime.fromisoformat(data["guardadas"])).days
        if dias < DIAS_CACHE:
            print(f"🍪 Cookies del caché (~{DIAS_CACHE - dias} días restantes)\n")
            return data["raw"]
        print(f"⚠️  Cookies con {dias} días — renovando...")
    else:
        print("📋 Primera vez — configura cookies.")
    return pedir_cookies()


# ─── PARSER JSON (validado contra HTML real: 50/50 productos) ────────────────
def extraer_json_embebido(html):
    soup = BeautifulSoup(html, "html.parser")
    for s in soup.find_all("script"):
        txt = s.string or s.get_text() or ""
        if "product_list" in txt and "optimizedResponse" in txt:
            idx = txt.find('q("')
            if idx < 0:
                continue
            raw = txt[idx + 3: txt.rfind('")')]
            try:
                return json.loads('"' + raw + '"')
            except Exception:
                continue
    return None


def extraer_array(texto, clave):
    p = texto.find(clave)
    if p < 0:
        return None
    inicio = texto.find('[', p)
    depth, i = 0, inicio
    while i < len(texto):
        if texto[i] == '[':
            depth += 1
        elif texto[i] == ']':
            depth -= 1
            if depth == 0:
                return texto[inicio:i + 1]
        i += 1
    return None


def enriquecer(decoded, pid):
    d = {"precio_normal": None, "descuento": None,
         "disponibilidad": "Disponible", "ventas": None}
    p = decoded.find(f'"id":"{pid}"')
    if p < 0:
        return d
    b = decoded[p:p + 6000]
    m = re.search(r'"previous_price":\{"value":([\d.]+)', b)
    if m:
        d["precio_normal"] = float(m.group(1))
    m = re.search(r'"discount_label":\{"text":"(\d+)%', b)
    if m:
        d["descuento"] = int(m.group(1))
    m = re.search(r'"available_quantity":(\d+)', b)
    if m:
        d["disponibilidad"] = f"{m.group(1)} disponibles"
    m = re.search(r'"(\d+)\s*vendidos?"', b)
    if m:
        d["ventas"] = int(m.group(1))
    return d


def parsear_desde_json(html):
    decoded = extraer_json_embebido(html)
    if not decoded:
        return None
    pl_str = extraer_array(decoded, '"product_list":')
    if not pl_str:
        return []
    try:
        product_list = json.loads(pl_str)
    except Exception:
        return []
    productos = []
    for prod in product_list:
        pid = prod.get("id", "")
        extra = enriquecer(decoded, pid)
        oferta = prod.get("item_offered", {})
        rating = prod.get("aggregate_rating", {})
        po = oferta.get("price")
        productos.append({
            "Nombre":         prod.get("name", ""),
            "Precio oferta":  round(po, 2) if isinstance(po, (int, float)) else None,
            "Precio normal":  round(extra["precio_normal"], 2) if extra["precio_normal"] else (round(po, 2) if isinstance(po, (int, float)) else None),
            "Descuento %":    extra["descuento"],
            "URL imagen":     (prod.get("image", "") or "").replace("http://", "https://"),
            "Disponibilidad": extra["disponibilidad"],
            "Ventas":         extra["ventas"],
            "Calificación":   rating.get("rating_value"),
            "Reseñas":        rating.get("rating_count"),
            "Marca":          prod.get("brand_attribute", {}).get("name", ""),
            "URL producto":   oferta.get("url", ""),
        })
    return productos


# ─── FALLBACK: extraer del DOM ya renderizado ────────────────────────────────
JS_DOM = r"""
() => {
  const out = [];
  const vistos = new Set();
  const num = s => { const v=(s||'').replace(/[^\d]/g,''); return v?parseInt(v):null; };
  for (const pn of document.querySelectorAll('.andes-money-amount__fraction')) {
    let card = pn;
    for (let i=0;i<15&&card;i++){const t=(card.tagName||'').toLowerCase();const c=(card.className||'').toString();
      if(t==='li'||t==='article'||c.includes('layout__item')||c.includes('poly-card'))break;card=card.parentElement;}
    if(!card||vistos.has(card))continue;vistos.add(card);
    let a=null,mx=0;for(const x of card.querySelectorAll('a[href]')){const t=(x.innerText||'').trim();if(t.length>mx&&t.length>10){mx=t.length;a=x;}}
    if(!a)continue;
    let po=null,pnn=null;
    for(const fr of card.querySelectorAll('.andes-money-amount__fraction')){const n=num(fr.innerText);if(!n)continue;if(fr.closest('s')){if(pnn===null)pnn=n;}else{if(po===null)po=n;}}
    let desc=null;const de=card.querySelector('[class*="discount"]');if(de){const m=de.innerText.match(/(\d+)/);if(m)desc=parseInt(m[1]);}
    let img='';const ie=card.querySelector('img');if(ie){const s=ie.getAttribute('data-src')||ie.getAttribute('src')||'';if(s.startsWith('http'))img=s;}
    let rat=null;const re_=card.querySelector('[class*="rating"]');if(re_){const m=re_.innerText.match(/[\d.,]+/);if(m){const r=parseFloat(m[0].replace(',','.'));if(r>=0&&r<=5)rat=r;}}
    out.push({nombre:a.innerText.trim(),url:(a.getAttribute('href')||'').split('#')[0].split('?')[0],po,pnn,desc,img,rat});
  }
  return out;
}
"""


def parsear_desde_dom(page):
    try:
        crudos = page.evaluate(JS_DOM)
    except Exception:
        return []
    out = []
    for c in crudos:
        po = c.get("po")
        out.append({
            "Nombre": c.get("nombre", ""),
            "Precio oferta": round(po, 2) if po else None,
            "Precio normal": round(c.get("pnn"), 2) if c.get("pnn") else (round(po, 2) if po else None),
            "Descuento %": c.get("desc"),
            "URL imagen": c.get("img", ""),
            "Disponibilidad": "Disponible",
            "Ventas": None,
            "Calificación": c.get("rat"),
            "Reseñas": None,
            "Marca": "",
            "URL producto": c.get("url", ""),
        })
    return out


# ─── NAVEGACIÓN ──────────────────────────────────────────────────────────────
# ─── BLINDAJES ───────────────────────────────────────────────────────────────
def pausa_humana(minimo=None, maximo=None):
    a = minimo if minimo is not None else PAUSA_MIN
    b = maximo if maximo is not None else PAUSA_MAX
    time.sleep(random.uniform(a, b))


def goto_con_reintento(page, url, intentos=MAX_REINTENTOS):
    """Navega con reintentos ante errores de red/timeout. True si cargó."""
    for intento in range(1, intentos + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            return True
        except PWTimeout:
            print(f"  ⏳ Timeout (intento {intento}/{intentos}). Reintentando...")
            time.sleep(random.uniform(3, 6) * intento)
        except Exception as e:
            msg = str(e).lower()
            if any(x in msg for x in ["net::", "connection", "closed", "reset"]):
                print(f"  🔌 Error de red (intento {intento}/{intentos}). Reintentando...")
                time.sleep(random.uniform(3, 6) * intento)
            else:
                print(f"  ⚠️  Error inesperado: {e}")
                time.sleep(2)
    return False


def construir_url(termino, dominio):
    return f"https://listado.mercadolibre.{dominio}/{termino.strip().replace(' ', '-')}"


def hay_muro_login(page):
    t = page.content().lower()
    return "ingresa a tu cuenta" in t[:3000] and "soy nuevo" in t[:3000]


def esperar_productos(page):
    """Espera a que termine el streaming y haya productos."""
    try:
        page.wait_for_selector(".andes-money-amount__fraction", timeout=20000)
        page.wait_for_timeout(1500)
        return True
    except PWTimeout:
        return False


def siguiente_url(page):
    try:
        return page.evaluate("""() => {
            const sels = ['li.andes-pagination__button--next:not(.andes-pagination__button--disabled) a',
                          'a[title="Siguiente"]','a[aria-label="Siguiente"]','.andes-pagination__button--next a'];
            for (const s of sels){const a=document.querySelector(s);
                if(a&&a.href&&!a.closest('.andes-pagination__button--disabled'))return a.href;}
            return null;
        }""")
    except Exception:
        return None


def lanzar(p):
    args = ["--disable-blink-features=AutomationControlled", "--disable-infobars"]
    try:
        return p.chromium.launch(channel="chrome", headless=HEADLESS, args=args)
    except Exception:
        return p.chromium.launch(headless=HEADLESS, args=args)


# ─── FLUJO PRINCIPAL ─────────────────────────────────────────────────────────
def seleccionar_pais():
    print("📍 País:")
    for k, (nombre, _) in PAISES.items():
        print(f"   {k}. {nombre}")
    while True:
        op = input("\nOpción (Enter = México): ").strip() or "1"
        if op in PAISES:
            nombre, dom = PAISES[op]
            print(f"   ✓ {nombre}\n")
            return dom
        print("   Opción inválida.")


def main():
    print("═" * 55)
    print("   🛒  SCRAPER MERCADO LIBRE")
    print("═" * 55 + "\n")

    raw = cargar_cookies_raw()
    dominio = seleccionar_pais()
    cookies = cookies_para_playwright(raw, dominio)

    termino = input("🔍 ¿Qué producto o categoría buscas? ").strip()
    if not termino:
        sys.exit(1)
    try:
        max_prod = int(input("📦 ¿Cuántos productos? (Enter = 50): ").strip() or "50")
    except ValueError:
        max_prod = 50

    print(f"\n⏳ Buscando '{termino}' en mercadolibre.{dominio}...\n")

    todos = []
    urls_vistas = set()

    with sync_playwright() as p:
        browser = lanzar(p)
        context = browser.new_context(user_agent=UA,
                                      viewport={"width": 1366, "height": 900},
                                      locale="es-MX")
        context.add_init_script(STEALTH_JS)
        context.add_cookies(cookies)
        page = context.new_page()

        url = construir_url(termino, dominio)
        pagina = 1
        while len(todos) < max_prod and url:
            print(f"\n  📄 Página {pagina}")
            if not goto_con_reintento(page, url):
                print("  ✗ No se pudo cargar la página tras varios intentos. Guardando lo obtenido.")
                break

            if hay_muro_login(page):
                print("\n  ❌ Muro de login → cookies expiradas o inválidas.")
                try:
                    os.makedirs(CARPETA_CSV, exist_ok=True)
                    page.screenshot(path=os.path.join(CARPETA_CSV, "_debug_pagina.png"))
                except Exception:
                    pass
                print("     Borra _ml_cookies.json y corre de nuevo con cookies frescas.")
                break

            if not esperar_productos(page):
                # Un reintento por si el stream tardó de más
                print("  ⏳ El stream tardó, reintentando esta página una vez...")
                if goto_con_reintento(page, url, intentos=1) and esperar_productos(page):
                    pass
                else:
                    print("  ✗ No cargaron productos (timeout del stream).")
                    break

            # Extraer: primero JSON embebido, si no, del DOM renderizado
            productos = parsear_desde_json(page.content())
            if not productos:
                productos = parsear_desde_dom(page)

            if not productos:
                print("  ✗ Página sin productos.")
                break

            nuevos = 0
            for pr in productos:
                if len(todos) >= max_prod:
                    break
                clave = pr.get("URL producto") or pr.get("Nombre")
                if clave in urls_vistas:
                    continue
                urls_vistas.add(clave)
                todos.append(pr)
                nuevos += 1
                ps = f"${pr['Precio oferta']:,.0f}" if pr['Precio oferta'] else "N/A"
                print(f"  [{len(todos):>3}] {pr['Nombre'][:46]:<46} | {ps}")

            if len(todos) >= max_prod:
                break

            url = siguiente_url(page)
            if not url or nuevos == 0:
                print("  ⏹  No hay más páginas.")
                break
            pagina += 1
            pausa_humana()

        context.close()
        browser.close()

    if not todos:
        print("\n✗ No se obtuvieron resultados.")
        sys.exit(1)

    columnas = ["Nombre", "Precio oferta", "Precio normal", "Descuento %",
                "URL imagen", "Disponibilidad", "Ventas", "Calificación",
                "Reseñas", "Marca", "URL producto"]
    df = pd.DataFrame(todos, columns=columnas)
    for col in ["Precio oferta", "Precio normal"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    df["Descuento %"]  = pd.to_numeric(df["Descuento %"], errors="coerce")
    df["Calificación"] = pd.to_numeric(df["Calificación"], errors="coerce").round(1)

    fecha = datetime.now().strftime("%d%m%Y")
    slug  = re.sub(r"[^a-zA-Z0-9]", "", termino).lower()[:30]
    os.makedirs(CARPETA_CSV, exist_ok=True)
    ruta = os.path.join(CARPETA_CSV, f"{slug}{fecha}.csv")
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        f.write("sep=;\n")
        df.to_csv(f, index=False, sep=";")

    print(f"\n{'═' * 55}")
    print(f"✅  {len(df)} productos → {ruta}")
    print(f"{'═' * 55}")
    v = df['Precio oferta'].dropna()
    if not v.empty:
        print(f"  Precio promedio : ${v.mean():>10,.2f}")
        print(f"  Precio mínimo   : ${v.min():>10,.2f}")
        print(f"  Precio máximo   : ${v.max():>10,.2f}")
    cd = df['Descuento %'].notna().sum()
    print(f"  Con descuento   : {cd} ({cd/len(df)*100:.0f}%)")
    cr = df['Calificación'].dropna()
    if not cr.empty:
        print(f"  Rating promedio : {cr.mean():.2f} estrellas")
    print(f"{'═' * 55}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  ⏹  Cancelado por el usuario. Hasta luego.")
        sys.exit(0)
    except Exception as e:
        print("\n" + "═" * 55)
        print(f"  ⚠️  Ocurrió un problema inesperado:")
        print(f"     {type(e).__name__}: {e}")
        print("═" * 55)
        print("  Si persiste, corre con HEADLESS = False para ver la página,")
        print("  o comparte el mensaje de arriba.")
        sys.exit(1)