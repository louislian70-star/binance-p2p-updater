"""
=============================================================
ACTUALIZADOR DE TASA OFICIAL BCV (USD y EUR)
=============================================================
- Nivel 1: Scraping directo a bcv.org.ve
- Nivel 2: Respaldo automático vía DolarApi
- Nivel 3: Escudo anti-caídas (mantiene histórico si todo falla)
- Auto-recuperación: regresa a la fuente oficial al revivir
- Guarda exclusivamente en la clave: BCV_DATA
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

# Zona horaria de Venezuela (UTC-4)
VET = timezone(timedelta(hours=-4))

# Credenciales desde GitHub Secrets
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

BCV_URL = "https://www.bcv.org.ve/"
KEY_NAME = "BCV_DATA"


def hoy_vet_iso():
    """Retorna la fecha de hoy en Venezuela (YYYY-MM-DD)."""
    return datetime.now(VET).strftime("%Y-%m-%d")


def normalizar_numero(texto_o_numero):
    """Trunca el número a exactamente 4 decimales sin redondear."""
    try:
        texto = str(texto_o_numero).strip().replace(" ", "").replace(",", ".")
        if not texto:
            return None

        valor = float(texto)
        if valor <= 0:
            return None

        if "." in texto:
            entero, decimales = texto.split(".", 1)
            decimales_digitos = "".join(ch for ch in decimales if ch.isdigit())
            decimales_cortadas = (decimales_digitos + "0000")[:4]
        else:
            decimales_cortadas = "0000"

        return float(f"{entero}.{decimales_cortadas}")
    except Exception:
        return None


def parsear_fecha_bcv(soup):
    """Extrae la Fecha Valor del HTML del BCV."""
    fecha_span = soup.find("span", class_="date-display-single")
    if not fecha_span:
        return None, None

    content = fecha_span.get("content", "")
    if content and len(content) >= 10:
        return content[:10], fecha_span.get_text().strip()

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


def nivel1_scraping_bcv():
    """NIVEL 1: Consulta directa a la página oficial del BCV."""
    print("🔍 [Nivel 1] Scraping directo a bcv.org.ve...")
    try:
        res = curl_req.get(
            BCV_URL,
            impersonate="chrome120",
            timeout=25,
            verify=False,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )

        if res.status_code != 200:
            print(f"  ⚠️ BCV respondió código HTTP {res.status_code}")
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        tasas = {}

        div_usd = soup.find("div", id="dolar")
        if div_usd:
            tag = div_usd.find("strong", class_="strong-tb") or div_usd.find("strong")
            if tag:
                tasas["usd"] = normalizar_numero(tag.get_text())

        div_eur = soup.find("div", id="euro")
        if div_eur:
            tag = div_eur.find("strong", class_="strong-tb") or div_eur.find("strong")
            if tag:
                tasas["eur"] = normalizar_numero(tag.get_text())

        fecha_iso, fecha_texto = parsear_fecha_bcv(soup)
        if fecha_iso:
            tasas["fecha_valor"] = fecha_iso
            tasas["fecha_valor_texto"] = fecha_texto or fecha_iso

        if tasas.get("usd") and tasas.get("eur") and tasas.get("fecha_valor"):
            tasas["fuente"] = "BCV_Oficial_Directo"
            print(f"  ✅ Éxito BCV -> USD: {tasas['usd']} | EUR: {tasas['eur']} | Fecha: {tasas['fecha_valor_texto']}")
            return tasas

    except Exception as e:
        print(f"  ⚠️ Falló Nivel 1 (BCV directo): {e}")

    return None


def nivel2_dolarapi_respaldo():
    """NIVEL 2: Respaldo vía DolarApi Venezuela."""
    print("🔄 [Nivel 2] Activando respaldo DolarApi...")
    try:
        tasas = {}
        res_usd = requests.get("https://ve.dolarapi.com/v1/dolares/oficial", timeout=10)
        if res_usd.status_code == 200:
            usd_val = res_usd.json().get("promedio")
            if usd_val and float(usd_val) > 0:
                tasas["usd"] = normalizar_numero(usd_val)

        res_eur = requests.get("https://ve.dolarapi.com/v1/euros/oficial", timeout=10)
        if res_eur.status_code == 200:
            eur_val = res_eur.json().get("promedio")
            if eur_val and float(eur_val) > 0:
                tasas["eur"] = normalizar_numero(eur_val)

        if tasas.get("usd") and tasas.get("eur"):
            tasas["fuente"] = "DolarApi_Respaldo"
            tasas["fecha_valor"] = hoy_vet_iso()
            tasas["fecha_valor_texto"] = hoy_vet_iso()
            print(f"  ✅ Éxito Respaldo -> USD: {tasas['usd']} | EUR: {tasas['eur']}")
            return tasas
    except Exception as e:
        print(f"  🚨 Falló Nivel 2 (DolarApi): {e}")

    return None


def read_kv():
    """Lee exclusivamente la clave BCV_DATA de Cloudflare KV."""
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/{KEY_NAME}"
    )
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            return res.json()
        elif res.status_code == 404:
            return {}
        else:
            print(f"⚠️ Error al leer KV ({res.status_code}): {res.text}")
            return None
    except Exception as e:
        print(f"⚠️ Error de red leyendo KV: {e}")
        return None


def write_kv(data):
    """Guarda exclusivamente en la clave BCV_DATA de Cloudflare KV."""
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/{KEY_NAME}"
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
    except Exception as e:
        print(f"🚨 Error escribiendo en KV: {e}")
        return False


def main():
    print("=" * 55)
    print(f"🕐 BCV Updater: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"📅 Hoy en Venezuela: {hoy_vet_iso()}")
    print("=" * 55)

    if not all([CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN]):
        print("🚨 Faltan credenciales en GitHub Secrets.")
        return

    # 1. Leer estado actual del BCV
    bcv_data = read_kv()
    if bcv_data is None:
        print("🛡️ Abortando por seguridad: no se pudo leer el estado actual.")
        return

    usd_actual = bcv_data.get("usd", {})
    eur_actual = bcv_actual = bcv_data.get("eur", {})

    # 2. Promoción automática a medianoche
    hoy = hoy_vet_iso()
    fecha_proxima_usd = usd_actual.get("fecha_proxima")
    if fecha_proxima_usd and hoy >= fecha_proxima_usd:
        print(f"\n⏰ PROMOCIÓN AUTOMÁTICA: Tasa próxima ({usd_actual.get('proxima')}) ahora es vigente.")
        usd_actual["vigente"] = usd_actual.get("proxima")
        usd_actual["fecha_vigente"] = usd_actual.get("fecha_proxima")
        usd_actual["fecha_vigente_texto"] = usd_actual.get("fecha_proxima_texto", "")
        usd_actual["proxima"] = None
        usd_actual["fecha_proxima"] = None
        usd_actual["fecha_proxima_texto"] = None

        eur_actual["vigente"] = eur_actual.get("proxima")
        eur_actual["fecha_vigente"] = eur_actual.get("fecha_proxima")
        eur_actual["fecha_vigente_texto"] = eur_actual.get("fecha_proxima_texto", "")
        eur_actual["proxima"] = None
        eur_actual["fecha_proxima"] = None
        eur_actual["fecha_proxima_texto"] = None

    # 3. Consulta en cascada (Nivel 1 -> Nivel 2)
    tasas_frescas = nivel1_scraping_bcv()
    if not tasas_frescas:
        tasas_frescas = nivel2_dolarapi_respaldo()

    # 4. Procesar tasas frescas o aplicar Nivel 3 (Escudo)
    if tasas_frescas and tasas_frescas.get("usd", 0) > 0:
        fecha_scraped = tasas_frescas.get("fecha_valor", "")
        fecha_vigente_actual = usd_actual.get("fecha_vigente", "")

        if not fecha_vigente_actual:
            usd_actual = {
                "vigente": tasas_frescas["usd"],
                "fecha_vigente": fecha_scraped,
                "fecha_vigente_texto": tasas_frescas.get("fecha_valor_texto", fecha_scraped),
                "proxima": None, "fecha_proxima": None, "fecha_proxima_texto": None
            }
            eur_actual = {
                "vigente": tasas_frescas["eur"],
                "fecha_vigente": fecha_scraped,
                "fecha_vigente_texto": tasas_frescas.get("fecha_valor_texto", fecha_scraped),
                "proxima": None, "fecha_proxima": None, "fecha_proxima_texto": None
            }
        elif fecha_scraped > fecha_vigente_actual:
            print(f"📢 Nueva tasa próxima detectada: {tasas_frescas['usd']} ({fecha_scraped})")
            usd_actual["proxima"] = tasas_frescas["usd"]
            usd_actual["fecha_proxima"] = fecha_scraped
            usd_actual["fecha_proxima_texto"] = tasas_frescas.get("fecha_valor_texto", fecha_scraped)

            eur_actual["proxima"] = tasas_frescas["eur"]
            eur_actual["fecha_proxima"] = fecha_scraped
            eur_actual["fecha_proxima_texto"] = tasas_frescas.get("fecha_valor_texto", fecha_scraped)
        elif fecha_scraped == fecha_vigente_actual:
            usd_actual["vigente"] = tasas_frescas["usd"]
            eur_actual["vigente"] = tasas_frescas["eur"]
            print(f"🔄 Misma fecha ({fecha_scraped}): valores vigentes actualizados.")

        bcv_data = {
            "success": True,
            "fuente": tasas_frescas.get("fuente"),
            "usd": usd_actual,
            "eur": eur_actual,
            "updated_at": int(time.time()),
            "updated_at_human": time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        }

        if write_kv(bcv_data):
            print("🚀 Guardado exitoso en Cloudflare KV (Clave: BCV_DATA).")
        else:
            print("❌ Error escribiendo en KV.")
    else:
        # NIVEL 3: ESCUDO TOTAL
        print("\n🛡️ [Nivel 3 - Escudo Activo] Todas las fuentes fallaron.")
        print("🔒 No se modifica la base de datos para proteger los precios previos.")

    print("🏁 Fin del ciclo BCV.")


if __name__ == "__main__":
    main()
