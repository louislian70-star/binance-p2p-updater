"""
=============================================================
ACTUALIZADOR DE TASA OFICIAL BCV (USD y EUR)
=============================================================
- Opción 1: Scraping directo a bcv.org.ve (HTML real)
- Opción 2: Respaldo vía DolarApi (si BCV está caído)
- Lógica de 2 tasas: VIGENTE (hoy) + PRÓXIMA (mañana/lunes)
- Promoción automática a medianoche de Venezuela (UTC-4)
- Precisión: valores TRUNCADOS a exactamente 4 decimales
  (sin redondeo, fieles al formato del BCV)
- Anti-ceros: si ambas fuentes fallan, NO toca la BD
- Protege precios P2P y maneja compatibilidad de datos viejos
=============================================================
"""

import os
import json
import time
import re
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_req
import requests

# ==========================================================
# ZONA HORARIA DE VENEZUELA (UTC-4)
# ==========================================================
VET = timezone(timedelta(hours=-4))

# ==========================================================
# CREDENCIALES (GitHub Secrets)
# ==========================================================
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

BCV_URL = "https://www.bcv.org.ve/"


# ==========================================================
# UTILIDADES DE FECHA Y NÚMEROS
# ==========================================================

def hoy_vet_iso():
    """Retorna la fecha de HOY en Venezuela en formato ISO (YYYY-MM-DD)."""
    return datetime.now(VET).strftime("%Y-%m-%d")


def normalizar_numero(texto_o_numero):
    """
    Devuelve el valor con EXACTAMENTE 4 decimales, TRUNCADO.

    NUNCA redondea: corta el texto en el 4to decimal.
    El corte se hace manipulando TEXTO (no multiplicaciones)
    para evitar errores de precisión de punto flotante.

    Ejemplos:
      '801,1752'     -> 801.1752  (idéntico al BCV)
      '794,99170000' -> 794.9917  (ceros sobrantes descartados)
      '922,69121677' -> 922.6912  (NO 922.6913: truncado, no redondeado)
      '801,17'       -> 801.17    (si trae menos decimales, se respeta)
    """
    try:
        # Normalizar: quitar espacios, coma -> punto
        texto = str(texto_o_numero).strip().replace(" ", "").replace(",", ".")

        if not texto:
            return None

        # Validar que sea un número positivo real
        valor = float(texto)
        if valor <= 0:
            return None

        # --------------------------------------------------
        # CORTE POR TEXTO en el 4to decimal (sin redondear)
        # --------------------------------------------------
        if "." in texto:
            entero, decimales = texto.split(".", 1)
            # Conservar solo dígitos en la parte decimal
            decimales_digitos = "".join(ch for ch in decimales if ch.isdigit())
            # Cortar en 4; rellenar con ceros si faltan
            decimales_cortadas = (decimales_digitos + "0000")[:4]
        else:
            # Sin parte decimal: completar con 4 ceros
            decimales_cortadas = "0000"

        return float(f"{entero}.{decimales_cortadas}")

    except Exception:
        return None


def parsear_fecha_bcv(soup):
    """
    Extrae la Fecha Valor del HTML del BCV.
    Retorna: (fecha_iso, fecha_texto)
    """
    fecha_span = soup.find("span", class_="date-display-single")
    if not fecha_span:
        return None, None

    # Prioridad 1: atributo content="2026-08-31T00:00:00-04:00"
    content = fecha_span.get("content", "")
    if content and len(content) >= 10:
        fecha_iso = content[:10]  # "2026-08-31"
        fecha_texto = fecha_span.get_text().strip()
        return fecha_iso, fecha_texto

    # Prioridad 2: parsear texto visible "Lunes, 31 Agosto 2026"
    texto = fecha_span.get_text().strip()
    meses = {
        "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
        "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
        "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
    }
    match = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", texto)
    if match:
        dia = match.group(1).zfill(2)
        mes = meses.get(match.group(2).lower(), "01")
        anio = match.group(3)
        return f"{anio}-{mes}-{dia}", texto

    return None, texto


# ==========================================================
# OPCIÓN 1: SCRAPING DIRECTO AL BCV
# ==========================================================

def scrape_bcv_oficial():
    """
    Scrapea la página oficial del BCV (bcv.org.ve).
    Extrae USD, EUR y Fecha Valor directamente del HTML.
    Usa curl_cffi para simular Chrome real y evitar bloqueos SSL.
    """
    print("🔍 [Opción 1] Scraping directo a bcv.org.ve...")
    try:
        res = curl_req.get(
            BCV_URL,
            impersonate="chrome120",
            timeout=25,
            verify=False,  # El BCV tiene problemas frecuentes de certificado SSL
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            }
        )

        if res.status_code != 200:
            print(f"  ⚠️ BCV respondió HTTP {res.status_code}")
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        tasas = {}

        # --- Extraer Dólar (div#dolar > strong.strong-tb) ---
        div_usd = soup.find("div", id="dolar")
        if div_usd:
            tag = div_usd.find("strong", class_="strong-tb") or div_usd.find("strong")
            if tag:
                tasas["usd"] = normalizar_numero(tag.get_text())

        # --- Extraer Euro (div#euro > strong.strong-tb) ---
        div_eur = soup.find("div", id="euro")
        if div_eur:
            tag = div_eur.find("strong", class_="strong-tb") or div_eur.find("strong")
            if tag:
                tasas["eur"] = normalizar_numero(tag.get_text())

        # --- Extraer Fecha Valor ---
        fecha_iso, fecha_texto = parsear_fecha_bcv(soup)
        if fecha_iso:
            tasas["fecha_valor"] = fecha_iso
            tasas["fecha_valor_texto"] = fecha_texto or fecha_iso

        # Validar que tenemos ambos valores y la fecha
        if tasas.get("usd") and tasas.get("eur") and tasas.get("fecha_valor"):
            tasas["fuente"] = "BCV_Scraping_Directo"
            print(f"  ✅ USD: {tasas['usd']} | EUR: {tasas['eur']} | Fecha: {tasas['fecha_valor_texto']}")
            return tasas

        print(f"  ⚠️ Datos incompletos del scraping: {list(tasas.keys())}")

    except Exception as e:
        print(f"  ⚠️ Error en scraping: {e}")

    return None


# ==========================================================
# OPCIÓN 2: RESPALDO VÍA DOLARAPI
# ==========================================================

def fetch_dolarapi_respaldo():
    """
    Fuente de respaldo cuando el BCV está caído.
    DolarApi Venezuela: https://ve.dolarapi.com

    Usa la MISMA función normalizar_numero que el scraping
    (truncado a 4 decimales) para mantener consistencia total
    de precisión entre ambas fuentes.
    """
    print("🔄 [Opción 2 - Respaldo] Consultando DolarApi...")
    try:
        tasas = {}

        # Dólar oficial
        res_usd = requests.get("https://ve.dolarapi.com/v1/dolares/oficial", timeout=10)
        if res_usd.status_code == 200:
            usd_val = res_usd.json().get("promedio")
            if usd_val and float(usd_val) > 0:
                # Truncado a 4 decimales (mismo criterio que el scraping)
                tasas["usd"] = normalizar_numero(usd_val)

        # Euro oficial
        res_eur = requests.get("https://ve.dolarapi.com/v1/euros/oficial", timeout=10)
        if res_eur.status_code == 200:
            eur_val = res_eur.json().get("promedio")
            if eur_val and float(eur_val) > 0:
                # Truncado a 4 decimales (mismo criterio que el scraping)
                tasas["eur"] = normalizar_numero(eur_val)

        # Validar que obtuvimos ambos valores válidos
        if tasas.get("usd") and tasas.get("eur"):
            tasas["fuente"] = "DolarApi_Respaldo"
            tasas["fecha_valor"] = hoy_vet_iso()
            tasas["fecha_valor_texto"] = hoy_vet_iso()
            print(f"  ✅ Respaldo OK -> USD: {tasas['usd']} | EUR: {tasas['eur']}")
            return tasas

    except Exception as e:
        print(f"  🚨 Respaldo también falló: {e}")

    return None


# ==========================================================
# CLOUDFLARE KV: LECTURA Y ESCRITURA
# ==========================================================

def read_kv():
    """Lee el JSON completo actual de Cloudflare KV."""
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/LATEST_PRICES"
    )
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return None


def write_kv(data):
    """Guarda el JSON completo en Cloudflare KV (1 sola escritura)."""
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/LATEST_PRICES"
    )
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        res = requests.put(
            url,
            data=json.dumps(data, ensure_ascii=False),
            headers=headers,
            timeout=15
        )
        return res.json().get("success", False)
    except Exception:
        return False


# ==========================================================
# FLUJO PRINCIPAL (Lógica de 2 tasas: VIGENTE + PRÓXIMA)
# ==========================================================

def main():
    print("=" * 55)
    print(f"🕐 BCV Updater: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"📅 Hoy en Venezuela: {hoy_vet_iso()}")
    print("=" * 55)

    if not all([CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN]):
        print("🚨 Faltan credenciales en GitHub Secrets.")
        return

    # -------------------------------------------------------
    # 1. Leer estado actual de KV y normalizar tipos
    #    (escudo de compatibilidad contra formatos viejos)
    # -------------------------------------------------------
    kv_data = read_kv()
    if not isinstance(kv_data, dict):
        kv_data = {"success": True, "prices": {}}

    bcv_actual = kv_data.get("bcv")
    if not isinstance(bcv_actual, dict):
        bcv_actual = {}

    usd_actual = bcv_actual.get("usd")
    if not isinstance(usd_actual, dict):
        usd_actual = {}

    eur_actual = bcv_actual.get("eur")
    if not isinstance(eur_actual, dict):
        eur_actual = {}

    # -------------------------------------------------------
    # 2. PROMOCIÓN AUTOMÁTICA
    #    Si hoy (VET) >= fecha_proxima, la próxima se convierte
    #    en la vigente. Ocurre a medianoche de Venezuela.
    # -------------------------------------------------------
    hoy = hoy_vet_iso()
    fecha_proxima_usd = usd_actual.get("fecha_proxima")
    if fecha_proxima_usd and hoy >= fecha_proxima_usd:
        print(f"\n⏰ PROMOCIÓN AUTOMÁTICA:")
        print(f"   La tasa próxima ({usd_actual.get('proxima')}) ahora es la vigente.")

        # Promover USD
        usd_actual["vigente"] = usd_actual.get("proxima")
        usd_actual["fecha_vigente"] = usd_actual.get("fecha_proxima")
        usd_actual["fecha_vigente_texto"] = usd_actual.get("fecha_proxima_texto", "")
        usd_actual["proxima"] = None
        usd_actual["fecha_proxima"] = None
        usd_actual["fecha_proxima_texto"] = None

        # Promover EUR (misma fecha)
        eur_actual["vigente"] = eur_actual.get("proxima")
        eur_actual["fecha_vigente"] = eur_actual.get("fecha_proxima")
        eur_actual["fecha_vigente_texto"] = eur_actual.get("fecha_proxima_texto", "")
        eur_actual["proxima"] = None
        eur_actual["fecha_proxima"] = None
        eur_actual["fecha_proxima_texto"] = None

    # -------------------------------------------------------
    # 3. Obtener tasas frescas (Scraping -> Respaldo)
    # -------------------------------------------------------
    tasas_frescas = scrape_bcv_oficial()
    if not tasas_frescas:
        tasas_frescas = fetch_dolarapi_respaldo()

    # -------------------------------------------------------
    # 4. Procesar las tasas frescas
    # -------------------------------------------------------
    if tasas_frescas and tasas_frescas.get("usd", 0) > 0 and tasas_frescas.get("eur", 0) > 0:

        fecha_scraped = tasas_frescas.get("fecha_valor", "")
        fecha_vigente_actual = usd_actual.get("fecha_vigente", "")

        if not fecha_vigente_actual:
            # Primera ejecución: todo es vigente
            print("\n📌 Estableciendo tasas iniciales:")
            usd_actual["vigente"] = tasas_frescas["usd"]
            usd_actual["fecha_vigente"] = fecha_scraped
            usd_actual["fecha_vigente_texto"] = tasas_frescas.get("fecha_valor_texto", fecha_scraped)
            usd_actual["proxima"] = None
            usd_actual["fecha_proxima"] = None
            usd_actual["fecha_proxima_texto"] = None

            eur_actual["vigente"] = tasas_frescas["eur"]
            eur_actual["fecha_vigente"] = fecha_scraped
            eur_actual["fecha_vigente_texto"] = tasas_frescas.get("fecha_valor_texto", fecha_scraped)
            eur_actual["proxima"] = None
            eur_actual["fecha_proxima"] = None
            eur_actual["fecha_proxima_texto"] = None

        elif fecha_scraped > fecha_vigente_actual:
            # La fecha del scraping es FUTURA respecto a la vigente
            # -> El BCV ya publicó la tasa de mañana/lunes
            print(f"\n📢 NUEVA TASA DETECTADA (Próxima):")
            print(f"   Vigente: {usd_actual.get('vigente')} ({fecha_vigente_actual})")
            print(f"   Próxima: {tasas_frescas['usd']} ({fecha_scraped})")

            usd_actual["proxima"] = tasas_frescas["usd"]
            usd_actual["fecha_proxima"] = fecha_scraped
            usd_actual["fecha_proxima_texto"] = tasas_frescas.get("fecha_valor_texto", fecha_scraped)

            eur_actual["proxima"] = tasas_frescas["eur"]
            eur_actual["fecha_proxima"] = fecha_scraped
            eur_actual["fecha_proxima_texto"] = tasas_frescas.get("fecha_valor_texto", fecha_scraped)

        elif fecha_scraped == fecha_vigente_actual:
            # Misma fecha: actualizar valores numéricos de la vigente
            # (el BCV puede ajustar decimales durante el día)
            usd_actual["vigente"] = tasas_frescas["usd"]
            eur_actual["vigente"] = tasas_frescas["eur"]
            print(f"\n🔄 Misma fecha ({fecha_scraped}): valores vigentes actualizados.")

        # Registrar la fuente utilizada
        bcv_actual["fuente"] = tasas_frescas.get("fuente", "desconocida")

    else:
        # -------------------------------------------------------
        # ESCUDO ANTI-CEROS: Ambas fuentes fallaron
        # No tocar la base de datos. Mantener lo que había.
        # -------------------------------------------------------
        print("\n⚠️ No se pudieron obtener tasas frescas.")
        print("🛡️ PROTECCIÓN ACTIVA: Se conservan las tasas previas.")

    # -------------------------------------------------------
    # 5. Guardar en Cloudflare KV (merge con P2P)
    # -------------------------------------------------------
    bcv_actual["usd"] = usd_actual
    bcv_actual["eur"] = eur_actual
    kv_data["bcv"] = bcv_actual
    kv_data["updated_at"] = int(time.time())
    kv_data["updated_at_human"] = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())

    if write_kv(kv_data):
        print("\n🚀 Guardado exitoso en Cloudflare KV.")
        print(f"   USD Vigente: {usd_actual.get('vigente')} | Próxima: {usd_actual.get('proxima')}")
        print(f"   EUR Vigente: {eur_actual.get('vigente')} | Próxima: {eur_actual.get('proxima')}")
    else:
        print("\n❌ Error al guardar en Cloudflare KV.")

    print("🏁 Fin del ciclo BCV.")


if __name__ == "__main__":
    main()
