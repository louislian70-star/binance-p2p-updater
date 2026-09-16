"""
=============================================================
ACTUALIZADOR DE PRECIOS BINANCE P2P
=============================================================
- Consulta 6 pares en Binance P2P
- Filtra estafadores (reputación >=95%, órdenes >=10)
- Mantiene histórico de precios si una consulta individual falla
- Guarda exclusivamente en la clave: P2P_DATA
- No toca ni depende del BCV
=============================================================
"""

import os
import json
import time
import random
import statistics
from curl_cffi import requests as curl_req
import requests

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

KEY_NAME = "P2P_DATA"

QUERIES = [
    {"fiat": "VES", "asset": "USDT", "tradeType": "BUY", "payTypes": ["Banesco"],          "key": "VES_Banesco"},
    {"fiat": "VES", "asset": "USDT", "tradeType": "BUY", "payTypes": ["PagoMovil"],         "key": "VES_PagoMovil"},
    {"fiat": "VES", "asset": "USDT", "tradeType": "BUY", "payTypes": ["BancoDeVenezuela"],  "key": "VES_BancoDeVenezuela"},
    {"fiat": "VES", "asset": "USDT", "tradeType": "BUY", "payTypes": [],                    "key": "VES_General"},
    {"fiat": "USD", "asset": "USDT", "tradeType": "BUY", "payTypes": ["Zinli"],             "key": "USD_Zinli"},
    {"fiat": "USD", "asset": "USDT", "tradeType": "BUY", "payTypes": ["Zelle"],             "key": "USD_Zelle"},
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
        except Exception:
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


def read_kv():
    """Lee exclusivamente la clave P2P_DATA de Cloudflare KV."""
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/{KEY_NAME}"
    )
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    try:
        response = requests.get(url, headers=headers, timeout=12)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            return {}
        else:
            print(f"⚠️ Error al leer KV ({response.status_code}): {response.text}")
            return None
    except Exception as e:
        print(f"⚠️ Error de red al leer KV: {e}")
        return None


def write_kv(data):
    """Guarda exclusivamente en la clave P2P_DATA de Cloudflare KV."""
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/{KEY_NAME}"
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
    except Exception as e:
        print(f"🚨 Error escribiendo en KV: {e}")
        return False


def main():
    print("=" * 55)
    print(f"🕐 P2P Binance: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 55)

    if not all([CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN]):
        print("🚨 Faltan credenciales en GitHub Secrets.")
        return

    # 1. Leer precios previos para no perderlos si un par falla
    existing_kv = read_kv()
    if existing_kv is None:
        print("🛡️ Abortando por seguridad: no se pudo leer el estado actual.")
        return

    current_prices = existing_kv.get("prices", {})

    # 2. Consultar Binance P2P
    ok = 0
    for q in QUERIES:
        key = q["key"]
        print(f"🔄 {key}...")

        raw = fetch_with_retry(q["fiat"], q["asset"], q["tradeType"], q["payTypes"])
        if raw:
            processed = process_ads(raw)
            if processed:
                current_prices[key] = processed
                ok += 1
                print(f"   ✅ Mejor: {processed['best']} | Prom: {processed['avg']}")
            else:
                print(f"   ⚠️ Sin anuncios calificados (se conserva valor previo)")
        else:
            print(f"   ❌ Error de red (se conserva valor previo)")

        random_delay()

    # 3. Guardar en Cloudflare KV
    print(f"\n📊 P2P: {ok}/6 consultas exitosas")

    payload = {
        "success": True,
        "updated_at": int(time.time()),
        "updated_at_human": time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
        "prices": current_prices
    }

    if current_prices:
        if write_kv(payload):
            print("🚀 Guardado exitoso en Cloudflare KV (Clave: P2P_DATA).")
        else:
            print("❌ Error al guardar en KV.")
    else:
        print("⚠️ No hay datos para guardar.")


if __name__ == "__main__":
    main()
