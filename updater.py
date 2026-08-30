import os
import json
import time
import re
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_req
import requests

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

BCV_URL = "https://www.bcv.org.ve/"

def normalize_bcv_number(text):
    """Convierte '794,99170000' en 794.99"""
    try:
        clean = text.strip().replace(" ", "").replace(",", ".")
        val = float(clean)
        return round(val, 2) if val > 0 else None
    except Exception:
        return None

# ==========================================================
# OPCIÓN 1: SCRAPING DIRECTO AL BCV (HTML REAL)
# ==========================================================
def scrape_bcv_official():
    print("🔍 [Opción 1] Consultando página oficial del BCV...")
    try:
        res = curl_req.get(
            BCV_URL,
            impersonate="chrome120",
            timeout=25,
            verify=False,  # Evita caídas por errores de certificado SSL del BCV
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            }
        )
        if res.status_code != 200:
            print(f"  ⚠️ BCV respondió con HTTP {res.status_code}")
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        rates = {}

        # 1. Extraer USD (#dolar strong.strong-tb)
        div_usd = soup.find("div", id="dolar")
        if div_usd:
            tag = div_usd.find("strong", class_="strong-tb") or div_usd.find("strong")
            if tag:
                rates["usd"] = normalize_bcv_number(tag.get_text())

        # 2. Extraer EUR (#euro strong.strong-tb)
        div_eur = soup.find("div", id="euro")
        if div_eur:
            tag = div_eur.find("strong", class_="strong-tb") or div_eur.find("strong")
            if tag:
                rates["eur"] = normalize_bcv_number(tag.get_text())

        # 3. Extraer Fecha Valor (span.date-display-single)
        fecha_span = soup.find("span", class_="date-display-single")
        if fecha_span:
            fecha_txt = fecha_span.get_text().strip()  # Ej: "Lunes, 31 Agosto 2026"
            rates["fecha_valor"] = fecha_txt
            rates["es_tasa_lunes"] = "lunes" in fecha_txt.lower()
        else:
            rates["fecha_valor"] = time.strftime('%Y-%m-%d')
            rates["es_tasa_lunes"] = False

        if rates.get("usd") and rates.get("eur"):
            rates["fuente"] = "BCV_Oficial_Directo"
            print(f"  ✅ Scraping exitoso -> USD: {rates['usd']} | EUR: {rates['eur']} | Fecha: {rates['fecha_valor']}")
            return rates

    except Exception as e:
        print(f"  ⚠️ Error en Scraping BCV: {e}")
    return None

# ==========================================================
# OPCIÓN 2: RESPALDO VÍA API (DolarApi)
# ==========================================================
def fetch_bcv_fallback_api():
    print("🔄 [Opción 2] Activando respaldo vía DolarApi...")
    try:
        res_usd = requests.get("https://ve.dolarapi.com/v1/dolares/oficial", timeout=10)
        res_eur = requests.get("https://ve.dolarapi.com/v1/euros/oficial", timeout=10)
        
        rates = {}
        if res_usd.status_code == 200:
            usd_val = res_usd.json().get("promedio")
            if usd_val:
                rates["usd"] = round(float(usd_val), 2)
                rates["fecha_valor"] = res_usd.json().get("fechaActualizacion", "")

        if res_eur.status_code == 200:
            eur_val = res_eur.json().get("promedio")
            if eur_val:
                rates["eur"] = round(float(eur_val), 2)

        if rates.get("usd") and rates.get("eur"):
            rates["es_tasa_lunes"] = False
            rates["fuente"] = "DolarApi_Respaldo"
            print(f"  ✅ Respaldo exitoso -> USD: {rates['usd']} | EUR: {rates['eur']}")
            return rates
    except Exception as e:
        print(f"  🚨 Respaldo también falló: {e}")
    return None

# ==========================================================
# GESTIÓN EN CLOUDFLARE KV (ANTI-CEROS & MERGE)
# ==========================================================
def get_current_kv_data():
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/LATEST_PRICES"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return None

def upload_to_cloudflare_kv(data):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/LATEST_PRICES"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
    try:
        res = requests.put(url, data=json.dumps(data, ensure_ascii=False), headers=headers, timeout=15)
        return res.json().get("success", False)
    except Exception:
        return False

def main():
    print("=" * 50)
    print(f"🕐 Actualizador BCV: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 50)

    if not all([CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN]):
        print("🚨 Faltan credenciales en GitHub Secrets")
        return

    # Leer JSON actual
    kv_data = get_current_kv_data() or {
        "success": True,
        "prices": {}
    }

    # Intentar Opción 1: Scraping BCV
    bcv_rates = scrape_bcv_official()

    # Si falla, intentar Opción 2: Respaldo
    if not bcv_rates:
        bcv_rates = fetch_bcv_fallback_api()

    # Si ambas opciones obtuvieron datos válidos:
    if bcv_rates and bcv_rates.get("usd", 0) > 0 and bcv_rates.get("eur", 0) > 0:
        kv_data["bcv"] = bcv_rates
        kv_data["updated_at"] = int(time.time())
        kv_data["updated_at_human"] = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        
        if upload_to_cloudflare_kv(kv_data):
            print("🚀 Tasa oficial BCV actualizada y guardada en Cloudflare KV.")
        else:
            print("❌ Error al subir a Cloudflare KV.")
    else:
        # REGLA DE ORO: Si todo falló, NO poner 0.0. Mantener lo que ya había.
        print("⚠️ No se pudieron obtener tasas nuevas del BCV.")
        print("🛡️ PROTECCIÓN ACTIVA: Se conservan las tasas previas en memoria sin tocar la BD.")

if __name__ == "__main__":
    main()
