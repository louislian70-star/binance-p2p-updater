import os
import json
import time
import random
import statistics
from curl_cffi import requests as curl_req
import requests

# ==========================================================
# CREDENCIALES (GitHub Secrets)
# ==========================================================
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

# ==========================================================
# ÚNICAMENTE TUS 6 DATOS (Sin otros países ni métodos extra)
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
        "payTypes": [],  # General: todos los bancos
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
IMPERSONATE_OPTIONS = ["chrome120", "chrome124", "chrome131"]

def random_delay():
    time.sleep(random.uniform(2.5, 4.5))

def fetch_with_retry(fiat, asset, trade_type, pay_types, max_retries=3):
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
            response = curl_req.post(
                BINANCE_URL,
                json=payload,
                impersonate=random.choice(IMPERSONATE_OPTIONS),
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
            elif response.status_code in [403, 429]:
                time.sleep(10 * attempt)
                continue

        except Exception as e:
            pass

        if attempt < max_retries:
            time.sleep(4 * attempt)

    return None

def process_ads(raw_data):
    ads = raw_data.get("data", [])
    if not ads:
        return None

    valid_prices = []
    for item in ads:
        advertiser = item.get("advertiser", {})
        adv = item.get("adv", {})
        
        finish_rate = advertiser.get("monthFinishRate", 0) * 100
        order_count = advertiser.get("monthOrderCount", 0)

        # Filtro de reputación para evitar estafas
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
        return response.json().get("success", False)
    except:
        return False

def main():
    print("=" * 50)
    print(f"🕐 Actualizando solo 6 tasas: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 50)

    if not all([CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN]):
        print("🚨 Faltan credenciales en GitHub Secrets.")
        return

    result = {
        "success": True,
        "updated_at": int(time.time()),
        "updated_at_human": time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
        "prices": {}
    }

    ok = 0
    for q in QUERIES:
        key = q["key"]
        print(f"🔄 Consultando {key}...")

        raw = fetch_with_retry(q["fiat"], q["asset"], q["tradeType"], q["payTypes"])
        if raw:
            processed = process_ads(raw)
            if processed:
                result["prices"][key] = processed
                ok += 1
                print(f"   ✅ Mejor: {processed['best']} | Promedio: {processed['avg']}")
            else:
                print(f"   ⚠️ Sin anuncios calificados")
        else:
            print(f"   ❌ Error al consultar Binance")

        random_delay()

    print(f"\n📊 Resultado: {ok}/6 exitosas")

    if ok > 0:
        if upload_to_cloudflare_kv(result):
            print("🚀 Guardado con éxito en Cloudflare KV.")
        else:
            print("❌ Error al guardar en Cloudflare.")
    else:
        print("⚠️ No hubo datos para guardar.")

if __name__ == "__main__":
    main()
