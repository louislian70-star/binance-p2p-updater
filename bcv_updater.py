import os
import json
import time
import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_req

# ==========================================================
# CREDENCIALES (GitHub Secrets)
# ==========================================================
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

BCV_URL = "https://www.bcv.org.ve/"

def normalize_bcv_number(text):
    """Limpia y convierte '794,99170000' en 794.99"""
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
    print("🔍 [Opción 1] Intentando Scraping directo a bcv.org.ve...")
    try:
        res = curl_req.get(
            BCV_URL,
            impersonate="chrome120",
            timeout=25,
            verify=False,  # Evita errores si el certificado SSL del BCV falla
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            }
        )
        
        if res.status_code != 200:
            print(f"  ⚠️ BCV respondió HTTP {res.status_code}")
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        rates = {}

        # 1. Extraer Dólar (div#dolar)
        div_usd = soup.find("div", id="dolar")
        if div_usd:
            tag = div_usd.find("strong", class_="strong-tb") or div_usd.find("strong")
            if tag:
                rates["usd"] = normalize_bcv_number(tag.get_text())

        # 2. Extraer Euro (div#euro)
        div_eur = soup.find("div", id="euro")
        if div_eur:
            tag = div_eur.find("strong", class_="strong-tb") or div_eur.find("strong")
            if tag:
                rates["eur"] = normalize_bcv_number(tag.get_text())

        # 3. Extraer Fecha Valor (span.date-display-single)
        fecha_span = soup.find("span", class_="date-display-single")
        if fecha_span:
            fecha_txt = fecha_span.get_text().strip()
            rates["fecha_valor"] = fecha_txt
            # Detecta si explícitamente dice 'Lunes'
            rates["es_tasa_lunes"] = "lunes" in fecha_txt.lower()
        else:
            rates["fecha_valor"] = time.strftime('%Y-%m-%d')
            rates["es_tasa_lunes"] = False

        # Si tenemos ambos valores válidos, el scraping fue un éxito
        if rates.get("usd") and rates.get("eur"):
            rates["fuente"] = "BCV_Scraping_Directo"
            print(f"  ✅ Scraping exitoso -> USD: {rates['usd']} | EUR: {rates['eur']} | Fecha: {rates['fecha_valor']}")
            return rates

    except Exception as e:
        print(f"  ⚠️ Scraping falló: {e}")
    
    return None

# ==========================================================
# OPCIÓN 2: RESPALDO POR API (DolarApi)
# ==========================================================
def fetch_bcv_fallback_api():
    print("🔄 [Opción 2 - Respaldo] Activando consulta a DolarApi...")
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
# CLOUDFLARE KV: LECTURA, MERGE Y PROTECCIÓN ANTI-CEROS
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

# ==========================================================
# FLUJO PRINCIPAL
# ==========================================================
def main():
    print("=" * 50)
    print(f"🕐 Actualizador BCV: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 50)

    if not all([CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN]):
        print("🚨 Faltan credenciales en GitHub Secrets.")
        return

    # Leer el JSON actual en Cloudflare para conservar las tasas P2P
    kv_data = get_current_kv_data() or {
        "success": True,
        "prices": {}
    }

    # 1. Intentar Scraping Directo
    bcv_rates = scrape_bcv_official()

    # 2. Si falla, intentar Respaldo API
    if not bcv_rates:
        bcv_rates = fetch_bcv_fallback_api()

    # 3. Guardar solo si obtuvimos tasas válidas (> 0)
    if bcv_rates and bcv_rates.get("usd", 0) > 0 and bcv_rates.get("eur", 0) > 0:
        kv_data["bcv"] = bcv_rates
        kv_data["updated_at"] = int(time.time())
        kv_data["updated_at_human"] = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        
        if upload_to_cloudflare_kv(kv_data):
            print("🚀 Tasa oficial BCV actualizada y guardada con éxito en Cloudflare.")
        else:
            print("❌ Error al subir a Cloudflare KV.")
    else:
        # ESCUDO ANTI-CEROS:
        print("⚠️ No se pudo obtener tasa fresca del BCV en este ciclo.")
        print("🛡️ PROTECCIÓN ACTIVA: Se conservan intactas las tasas previas sin romper la app.")

if __name__ == "__main__":
    main()
