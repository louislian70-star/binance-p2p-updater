import os
import json
import time
import random
import statistics
from curl_cffi import requests as curl_req
import requests

# ==========================================================
# CREDENCIALES (Inyectadas desde GitHub Secrets)
# ==========================================================
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

# ==========================================================
# LOS 6 DATOS EXACTOS CONFIGURADOS CON IDS VERIFICADOS
# ==========================================================
QUERIES = [
    {
        "fiat": "VES",
        "asset": "USDT",
        "tradeType": "BUY",
        "payTypes": ["Banesco"],
        "key": "VES_Banesco"
    },
    {
        "fiat": "VES",
        "asset": "USDT",
        "tradeType": "BUY",
        "payTypes": ["PagoMovil"],
        "key": "VES_PagoMovil"
    },
    {
        "fiat": "VES",
        "asset": "USDT",
        "tradeType": "BUY",
        "payTypes": ["BancoDeVenezuela"],
        "key": "VES_BancoDeVenezuela"
    },
    {
        "fiat": "VES",
        "asset": "USDT",
        "tradeType": "BUY",
        "payTypes": [],  # Lista vacía = consulta general sin filtrar banco
        "key": "VES_General"
    },
    {
        "fiat": "USD",
        "asset": "USDT",
        "tradeType": "BUY",
        "payTypes": ["Zinli"],
        "key": "USD_Zinli"
    },
    {
        "fiat": "USD",
        "asset": "USDT",
        "tradeType": "BUY",
        "payTypes": ["Zelle"],
        "key": "USD_Zelle"
    }
]

BINANCE_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

# ==========================================================
# PROTECCIONES ANTI-BLOQUEO
# ==========================================================
IMPERSONATE_OPTIONS = ["chrome120", "chrome124", "chrome131"]

def random_delay():
    """Pausa aleatoria entre 2.5 y 4.5 segundos para no crear patrones."""
    time.sleep(random.uniform(2.5, 4.5))

def fetch_with_retry(fiat, asset, trade_type, pay_types, max_retries=3):
    """Petición con suplantación de huella TLS y reintentos automáticos."""
    payload = {
        "page": 1,
        "rows": 20,
        "payTypes": pay_types,
        "asset": asset,
        "tradeType": trade_type,
        "fiat": fiat,
        "publisherType": None,
        "merchantCheck": False
    }

    for attempt in range(1, max_retries + 1):
        try:
            chosen_fingerprint = random.choice(IMPERSONATE_OPTIONS)

            response = curl_req.post(
                BINANCE_URL,
                json=payload,
                impersonate=chosen_fingerprint,
                timeout=15,
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                    "Origin": "https://p2p.binance.com",
                    "Referer": f"https://p2p.binance.com/es/trade/{trade_type.lower()}/{asset}?fiat={fiat}",
                    "Cache-Control": "no-cache",
                }
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success") and data.get("data"):
                    return data
                else:
                    print(f"  ⚠️ Intento {attempt}: Binance respondió success=false")
            elif response.status_code == 429:
                wait_time = 10 * attempt
                print(f"  ⚠️ Rate Limit (429). Esperando {wait_time}s...")
                time.sleep(wait_time)
                continue
            elif response.status_code == 403:
                wait_time = 15 * attempt
                print(f"  ⚠️ Bloqueo temporal (403). Esperando {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                print(f"  ⚠️ Intento {attempt}: Código HTTP {response.status_code}")

        except Exception as e:
            print(f"  ⚠️ Intento {attempt}: Error de conexión: {e}")

        if attempt < max_retries:
            time.sleep(4 * attempt)

    return None

def process_ads(raw_data):
    """Filtra anuncios sospechosos y calcula mejor precio y promedio real."""
    ads = raw_data.get("data", [])
    if not ads:
        return None

    valid_prices = []
    for item in ads:
        advertiser = item.get("advertiser", {})
        adv = item.get("adv", {})
        
        finish_rate = advertiser.get("monthFinishRate", 0) * 100
        order_count = advertiser.get("monthOrderCount", 0)

        # Filtro de calidad: solo comerciantes con historial confiable
        if finish_rate >= 95.0 and order_count >= 10:
            try:
                price = float(adv.get("price", 0))
                if price > 0:
                    valid_prices.append(price)
            except (ValueError, TypeError):
                continue

    if not valid_prices:
        return None

    valid_prices.sort()

    # Promedio recortado (elimina extremos para evitar outliers)
    if len(valid_prices) >= 4:
        trimmed = valid_prices[1:-1]
        avg_price = round(statistics.mean(trimmed), 2)
    else:
        avg_price = round(statistics.mean(valid_prices), 2)

    return {
        "best": round(min(valid_prices), 2),
        "avg": avg_price,
        "min": round(min(valid_prices), 2),
        "max": round(max(valid_prices), 2),
        "count": len(valid_prices)
    }

def upload_to_cloudflare_kv(data):
    """Guarda todo el resultado en un único registro en Cloudflare KV."""
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/storage/kv/namespaces/"
        f"{CF_KV_NAMESPACE_ID}/values/LATEST_PRICES"
    )
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        response = requests.put(
            url,
            data=json.dumps(data, ensure_ascii=False),
            headers=headers,
            timeout=15
        )
        result = response.json()
        if result.get("success"):
            print("✅ Precios guardados exitosamente en Cloudflare KV.")
            return True
        else:
            print(f"❌ Error en la API de Cloudflare: {result}")
            return False
    except Exception as e:
        print(f"🚨 Error al subir a Cloudflare: {e}")
        return False

def main():
    print("=" * 55)
    print(f"🕐 Iniciando ciclo: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 55)

    if not all([CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN]):
        print("🚨 Error: Faltan credenciales en los Secrets del repositorio.")
        return

    result = {
        "success": True,
        "updated_at": int(time.time()),
        "updated_at_human": time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
        "prices": {}
    }

    success_count = 0
    total_queries = len(QUERIES)

    for q in QUERIES:
        key = q["key"]
        method_label = q["payTypes"][0] if q["payTypes"] else "GENERAL (TODOS)"
        print(f"\n🔄 Consultando [{q['fiat']}] USDT vía {method_label}...")

        raw = fetch_with_retry(q["fiat"], q["asset"], q["tradeType"], q["payTypes"])

        if raw:
            processed = process_ads(raw)
            if processed:
                result["prices"][key] = processed
                success_count += 1
                print(f"   ✅ Mejor: {processed['best']} | Prom: {processed['avg']} | Anuncios analizados: {processed['count']}")
            else:
                print(f"   ⚠️ No se encontraron anuncios que cumplan el filtro de seguridad.")
        else:
            print(f"   ❌ Falló la consulta tras los reintentos.")

        random_delay()

    print(f"\n{'=' * 55}")
    print(f"📊 Resultado: {success_count}/{total_queries} consultas exitosas.")

    if success_count > 0:
        upload_to_cloudflare_kv(result)
    else:
        print("⚠️ No se subieron datos porque ninguna consulta tuvo éxito.")

    print("🏁 Ejecución finalizada.")

if __name__ == "__main__":
    main()
