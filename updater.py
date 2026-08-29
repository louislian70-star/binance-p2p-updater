import os
import json
import time
import statistics
from curl_cffi import requests as curl_req
import requests

# ==========================================
# CONFIGURACIÓN (se lee de variables de entorno de GitHub Secrets)
# ==========================================
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

# ==========================================
# CONFIGURACIÓN DE CONSULTAS
# Agrega o quita monedas/métodos de pago aquí
# ==========================================
QUERIES = [
    # Venezuela
    {"fiat": "VES", "asset": "USDT", "tradeType": "BUY",  "payTypes": ["Banesco", "PagoMovil", "Mercantil"]},
    {"fiat": "VES", "asset": "USDT", "tradeType": "SELL", "payTypes": ["Banesco", "PagoMovil"]},
    # Colombia
    {"fiat": "COP", "asset": "USDT", "tradeType": "BUY",  "payTypes": ["Nequi", "Daviplata", "Bancolombia"]},
    # Argentina
    {"fiat": "ARS", "asset": "USDT", "tradeType": "BUY",  "payTypes": ["MercadoPago"]},
    # Perú
    {"fiat": "PEN", "asset": "USDT", "tradeType": "BUY",  "payTypes": ["Yape", "Plin"]},
]

BINANCE_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"


def fetch_binance_p2p(fiat: str, asset: str, trade_type: str, pay_type: str) -> dict | None:
    """Consulta el endpoint de Binance P2P simulando Chrome real."""
    payload = {
        "page": 1,
        "rows": 15,
        "payTypes": [pay_type],
        "asset": asset,
        "tradeType": trade_type,
        "fiat": fiat,
        "publisherType": None,
        "merchantCheck": False
    }

    try:
        response = curl_req.post(
            BINANCE_URL,
            json=payload,
            impersonate="chrome120",
            timeout=15
        )

        if response.status_code == 200:
            data = response.json()
            if data.get("success") and data.get("data"):
                return data
            else:
                print(f"  ⚠️ Binance devolvió success=false para {pay_type}")
                return None
        else:
            print(f"  ❌ HTTP {response.status_code} para {fiat}/{pay_type}")
            return None

    except Exception as e:
        print(f"  🚨 Error de red: {e}")
        return None


def process_ads(raw_data: dict, trade_type: str) -> dict | None:
    """
    Filtra anuncios de estafadores y calcula precios.
    CORREGIDO: usa min() para BUY y max() para SELL.
    """
    ads = raw_data.get("data", [])
    if not ads:
        return None

    valid_prices = []

    for item in ads:
        advertiser = item.get("advertiser", {})
        adv = item.get("adv", {})

        # Filtro de calidad: solo comerciantes confiables
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

    # CORREGIDO: El mejor precio depende del tipo de operación
    if trade_type == "BUY":
        best_price = min(valid_prices)  # El más barato es mejor para comprar
    else:
        best_price = max(valid_prices)  # El más caro es mejor para vender

    # Promedio recortado (quita el más alto y el más bajo para eliminar outliers)
    if len(valid_prices) >= 4:
        trimmed = valid_prices[1:-1]
        avg_price = round(statistics.mean(trimmed), 2)
    else:
        avg_price = round(statistics.mean(valid_prices), 2)

    return {
        "best": round(best_price, 2),
        "avg": avg_price,
        "min": round(min(valid_prices), 2),
        "max": round(max(valid_prices), 2),
        "count": len(valid_prices)
    }


def upload_to_cloudflare_kv(data: dict) -> bool:
    """Sube todo el JSON como UN SOLO KEY a Cloudflare KV."""
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
            print("✅ Datos subidos a Cloudflare KV exitosamente")
            return True
        else:
            print(f"❌ Error de Cloudflare: {result}")
            return False

    except Exception as e:
        print(f"🚨 Error subiendo a Cloudflare: {e}")
        return False


def main():
    print("=" * 50)
    print(f"🕐 Iniciando actualización - {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 50)

    # Verificar que las credenciales existen
    if not all([CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN]):
        print("🚨 ERROR: Faltan credenciales de Cloudflare en los Secrets de GitHub")
        return

    # Estructura final que se guardará en KV
    result = {
        "success": True,
        "updated_at": int(time.time()),
        "updated_at_human": time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
        "rates": {}
    }

    total_queries = 0
    successful_queries = 0

    for query in QUERIES:
        fiat = query["fiat"]
        asset = query["asset"]
        trade_type = query["tradeType"]

        # Crear la estructura para esta moneda si no existe
        if fiat not in result["rates"]:
            result["rates"][fiat] = {}

        for pay_type in query["payTypes"]:
            total_queries += 1
            print(f"\n🔄 [{fiat}/{asset}] {trade_type} vía {pay_type}...")

            raw_data = fetch_binance_p2p(fiat, asset, trade_type, pay_type)

            if raw_data:
                processed = process_ads(raw_data, trade_type)
                if processed:
                    # Guardar en la estructura
                    if pay_type not in result["rates"][fiat]:
                        result["rates"][fiat][pay_type] = {}

                    result["rates"][fiat][pay_type][trade_type] = processed
                    successful_queries += 1
                    print(f"   ✅ Mejor: {processed['best']} | Promedio: {processed['avg']} | Anuncios válidos: {processed['count']}")
                else:
                    print(f"   ⚠️ No se encontraron anuncios válidos (comerciantes con >95% y >10 órdenes)")
            else:
                print(f"   ❌ No se pudo obtener datos")

            # Delay de seguridad entre consultas
            time.sleep(3)

    print(f"\n{'=' * 50}")
    print(f"📊 Resumen: {successful_queries}/{total_queries} consultas exitosas")

    if successful_queries > 0:
        upload_to_cloudflare_kv(result)
    else:
        print("⚠️ No se subieron datos porque no hubo consultas exitosas")

    print("🏁 Proceso terminado")


if __name__ == "__main__":
    main()
