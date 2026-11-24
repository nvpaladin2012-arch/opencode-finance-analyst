#!/usr/bin/env python3
"""Собирает актуальные котировки и публикует data.json + digest.md для GitHub Pages.

Запускается GitHub Actions каждый день в 07:00 UTC = 12:00 по Тюмени (UTC+5).
Только стандартная библиотека Python.
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# В России переходов на летнее время нет: Тюмень UTC+5, Москва UTC+3.
TYUMEN = timezone(timedelta(hours=5), "UTC+5")
MOSCOW = timezone(timedelta(hours=3), "UTC+3")
ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0 (compatible; finance-analyst-digest/1.0)"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SOURCES = {
    "cbr": "https://www.cbr.ru/scripts/XML_daily.asp",
    "moex_usd": "https://iss.moex.com/iss/engines/currency/markets/selt/securities/USD000UTSTOM.json?iss.meta=off",
    "moex_imoex": "https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX.json?iss.meta=off",
    "coingecko": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd,rub&include_24hr_change=true",
    "binance_btc": "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT",
    "binance_eth": "https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDT",
}

ALERT_EMAIL = "nvpaladin2012@gmail.com"
ALERT_ENDPOINT = f"https://formsubmit.co/ajax/{ALERT_EMAIL}"
ALERT_STATE = ROOT / "alert_state.json"
THRESHOLDS = {
    "usd_below": "USD/RUB ≤ 76,77 ₽",
    "btc_below": "BTC < 60 000 USD",
    "eth_below": "ETH < 1 600 USD",
    "imoex_below": "IMOEX < 2 000 п.",
}


def _is_ssl_verify_error(error: Exception) -> bool:
    cur: BaseException | None = error
    while cur is not None:
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        msg = str(cur).lower()
        if "certificate_verify_failed" in msg or "self-signed certificate" in msg:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _open_with_moex_fallback(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers=UA)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except Exception as error:
        is_moex = url.startswith("https://iss.moex.com/")
        if not (is_moex and _is_ssl_verify_error(error)):
            raise
        http_url = "http://" + url[len("https://"):]
        print(f"WARN moex ssl fallback: {url} -> {http_url}: {error}", file=sys.stderr)
        http_req = urllib.request.Request(http_url, headers=UA)
        return urllib.request.urlopen(http_req, timeout=timeout)


def fetch_json(url: str, timeout: int = 20) -> dict:
    with _open_with_moex_fallback(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url: str, encoding: str = "utf-8", timeout: int = 20) -> str:
    with _open_with_moex_fallback(url, timeout=timeout) as response:
        return response.read().decode(encoding, errors="replace")


def rows(payload: dict, block: str) -> list[dict]:
    section = payload.get(block, {})
    columns = section.get("columns", [])
    return [dict(zip(columns, row)) for row in section.get("data", [])]


def r2(value) -> float | None:
    return None if value is None else round(float(value), 2)


def money(value) -> int | float | None:
    if value is None:
        return None
    number = float(value)
    return round(number) if abs(number) >= 1000 else round(number, 2)


def dec(value: float, digits: int = 2, grouped: bool = False) -> str:
    pattern = f"{{:{',' if grouped else ''}.{digits}f}}"
    return pattern.format(value).replace(",", "\u00a0").replace(".", ",")


def signed_pct(value) -> str:
    return "н/д" if value is None else f"{value:+.2f}".replace(".", ",") + "%"


def get_cbr_usd() -> dict:
    raw = fetch_text(SOURCES["cbr"], encoding="windows-1251")
    root = ET.fromstring(raw)
    for valute in root.findall("Valute"):
        if valute.findtext("CharCode") == "USD":
            value = float(valute.findtext("Value").replace(",", "."))
            nominal = float(valute.findtext("Nominal") or 1)
            return {"value": r2(value / nominal), "date": root.attrib.get("Date")}
    raise ValueError("USD не найден в ответе ЦБ РФ")


def get_moex_usd() -> dict:
    payload = fetch_json(SOURCES["moex_usd"])
    candidates = [r for r in rows(payload, "marketdata") if r.get("LAST") is not None]
    row = next((r for r in candidates if r.get("BOARDID") == "CETS"), candidates[0] if candidates else {})
    return {
        "value": r2(row.get("LAST")),
        "change_24h": r2(row.get("LASTCHANGEPRCNT")),
        "updatetime": row.get("UPDATETIME"),
    }


def get_imoex(now: datetime) -> dict:
    payload = fetch_json(SOURCES["moex_imoex"])
    row = next(iter(rows(payload, "marketdata")), {})
    trade_date = row.get("TRADEDATE")
    moscow = now.astimezone(MOSCOW)
    minutes = moscow.hour * 60 + moscow.minute
    weekday = moscow.weekday() < 5
    intraday = 7 * 60 <= minutes <= 23 * 60 + 50
    today = trade_date == now.strftime("%Y-%m-%d") or trade_date == moscow.strftime("%Y-%m-%d")
    status = "Торги идут" if weekday and intraday and today else "Закрыты"
    return {
        "value": r2(row.get("CURRENTVALUE")),
        "change_pct": r2(row.get("LASTCHANGEPRC")),
        "change_points": r2(row.get("LASTCHANGE")),
        "status": status,
        "trade_date": trade_date,
        "updatetime": row.get("UPDATETIME"),
    }


def get_crypto(fx_fallback: float | None) -> dict:
    try:
        payload = fetch_json(SOURCES["coingecko"])
        result = {}
        for key, coin in (("bitcoin", "btc"), ("ethereum", "eth")):
            item = payload[key]
            result[coin] = {
                "usd": money(item.get("usd")),
                "rub": money(item.get("rub")),
                "change_24h": r2(item.get("usd_24h_change")),
            }
        result["source"] = "CoinGecko"
        return result
    except Exception as cg_error:
        result = {}
        for coin, url_key in (("btc", "binance_btc"), ("eth", "binance_eth")):
            try:
                item = fetch_json(SOURCES[url_key])
                price = float(item["lastPrice"])
                rub = price * fx_fallback if fx_fallback else None
                result[coin] = {
                    "usd": money(price),
                    "rub": money(rub),
                    "change_24h": r2(item.get("priceChangePercent")),
                }
            except Exception as binance_error:
                result[coin] = {"usd": None, "rub": None, "change_24h": None, "error": str(binance_error)}
        result["source"] = "Binance (fallback)"
        result["fallback_reason"] = str(cg_error)
        return result


def empty(name: str, error: Exception) -> dict:
    print(f"WARN {name}: {error}", file=sys.stderr)
    return {"error": str(error)}


def build_digest(data: dict) -> str:
    usd, crypto, imoex = data["usd"], data["crypto"], data["imoex"]
    btc, eth = crypto.get("btc", {}), crypto.get("eth", {})
    cbr = usd.get("cbr", {})
    market = usd.get("market", {})
    def fmt(value, suffix="", digits=2):
        return "н/д" if value is None else dec(value, digits, grouped=True) + suffix
    def crypto(item):
        usd_value = "н/д" if item.get("usd") is None else "$" + dec(item["usd"], 0 if abs(item["usd"]) >= 1000 else 2, grouped=True)
        rub_value = "н/д" if item.get("rub") is None else "~" + dec(item["rub"], 0 if abs(item["rub"]) >= 1000 else 2, grouped=True) + " ₽"
        return f"{usd_value} ({rub_value}) | {signed_pct(item.get('change_24h'))}"

    return "\n".join([
        f"📊 **Финансовая сводка на {data['generated_at_display']}**",
        "",
        "* **🇺🇸 Доллар (USD/RUB):**",
        f"  * Курс ЦБ РФ: {fmt(cbr.get('value'), ' ₽')} *(данные от {cbr.get('date', 'н/д')})*",
        f"  * Рыночный курс: {fmt(market.get('value'), ' ₽')} ({signed_pct(market.get('change_24h'))})",
        f"* **₿ Биткоин (BTC):** {crypto(btc)}",
        f"* **Ξ Эфириум (ETH):** {crypto(eth)}",
        "* **📈 Индекс Мосбиржи (IMOEX):** "
        f"{fmt(imoex.get('value'), ' п.')} | {signed_pct(imoex.get('change_pct'))} "
        f"({'+' if (imoex.get('change_points') or 0) >= 0 else ''}{dec(imoex['change_points']) if imoex.get('change_points') is not None else 'н/д'} п.) "
        f"*(Статус: {imoex.get('status', 'н/д')})*",
        "",
        "Источники: ЦБ РФ, Московская биржа, " + data.get("crypto", {}).get("source", "н/д") + ".",
        *([f"⚠️ Сработавшие пороги: {'; '.join(data['alerts'])}."] if data.get("alerts") else []),
        "Данные носят информационный характер и не являются финансовой рекомендацией.",
        "",
    ])


def alert_conditions(data: dict) -> dict[str, tuple[bool, str]]:
    usd = data["usd"].get("market", {}).get("value")
    if usd is None:
        usd = data["usd"].get("cbr", {}).get("value")
    btc = data["crypto"].get("btc", {}).get("usd")
    eth = data["crypto"].get("eth", {}).get("usd")
    index = data["imoex"].get("value")
    return {
        "usd_below": (
            usd is not None and usd <= 76.77,
            f"USD/RUB = {dec(usd)} ₽ (порог ≤ 76,77 ₽)" if usd is not None else "USD/RUB: н/д",
        ),
        "btc_below": (
            btc is not None and btc < 60000,
            f"BTC = ${dec(btc, 0)} (порог < 60 000 USD)" if btc is not None else "BTC: н/д",
        ),
        "eth_below": (
            eth is not None and eth < 1600,
            f"ETH = ${dec(eth, 0)} (порог < 1 600 USD)" if eth is not None else "ETH: н/д",
        ),
        "imoex_below": (
            index is not None and index < 2000,
            f"IMOEX = {dec(index)} п. (порог < 2 000 п.)" if index is not None else "IMOEX: н/д",
        ),
    }


def load_alert_state() -> dict:
    try:
        return json.loads(ALERT_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_alert_state(state: dict) -> None:
    ALERT_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send_email(subject: str, message: str) -> bool:
    payload = json.dumps({"subject": subject, "message": message}, ensure_ascii=False).encode("utf-8")
    headers = {
        **UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://nvpaladin2012-arch.github.io",
        "Referer": "https://nvpaladin2012-arch.github.io/",
    }
    try:
        request = urllib.request.Request(ALERT_ENDPOINT, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read().decode("utf-8", errors="replace")
            print(f"MAIL {response.status}: {body[:200]}")
            try:
                return bool(json.loads(body).get("success"))
            except ValueError:
                return 200 <= response.status < 300
    except Exception as error:
        print(f"WARN mail: {error}", file=sys.stderr)
        return False


def process_alerts(data: dict) -> None:
    """Пишет один раз при первом пересечении порога; повтор — после возврата выше порога."""
    conditions = alert_conditions(data)
    state = load_alert_state()
    to_send = [text for key, (triggered, text) in conditions.items()
               if triggered and not state.get(key, {}).get("active")]
    sent = False
    if to_send:
        subject = "⚠️ finance-analyst: сработали пороги котировок"
        body = (
            "Сработали настроенные пороги:\n\n"
            + "\n".join(f"• {line}" for line in to_send)
            + "\n\n" + build_digest(data)
        )
        sent = send_email(subject, body)

    new_state = {}
    for key, (triggered, text) in conditions.items():
        previous = state.get(key, {})
        active = triggered
        if triggered and not previous.get("active") and not sent:
            active = False  # отправить повторно при следующем запуске
        entry = {"active": active, "last_value": text, "checked_at": data["generated_at"]}
        if active and not previous.get("active"):
            entry["sent_at"] = data["generated_at"]
        new_state[key] = entry
    save_alert_state(new_state)

    data["alerts"] = [text for triggered, text in conditions.values() if triggered]
    data["alert_email_sent"] = sent if to_send else None


def main() -> int:
    if "--test-alert" in sys.argv:
        ok = send_email(
            "finance-analyst: тест оповещений",
            "Это тестовое письмо. Если вы его видите — доставка оповещений настроена.\n"
            "Пороги: USD/RUB ≤ 76,77 ₽, BTC < 60 000 USD, ETH < 1 600 USD, IMOEX < 2 000 п.",
        )
        print("Тестовое письмо отправлено" if ok else "Не удалось отправить тестовое письмо")
        return 0 if ok else 1

    now = datetime.now(TYUMEN)
    data = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_display": now.strftime("%d.%m.%Y, %H:%M") + " Тюмень (UTC+5)",
        "schedule": "Каждый день в 12:00 по Тюмени (07:00 UTC)",
        "usd": {},
        "imoex": {},
        "crypto": {},
        "notes": [],
    }

    try:
        data["usd"]["cbr"] = get_cbr_usd()
    except Exception as error:
        data["usd"]["cbr"] = empty("cbr", error)
    try:
        data["usd"]["market"] = get_moex_usd()
    except Exception as error:
        data["usd"]["market"] = empty("moex_usd", error)
    try:
        data["imoex"] = get_imoex(now)
    except Exception as error:
        data["imoex"] = empty("imoex", error)

    market_rate = data["usd"].get("market", {}).get("value")
    cbr_rate = data["usd"].get("cbr", {}).get("value")
    data["crypto"] = get_crypto(market_rate or cbr_rate)

    missing = [
        name for name, ok in (
            ("курс ЦБ РФ", data["usd"].get("cbr", {}).get("value") is not None),
            ("рыночный курс USD/RUB", data["usd"].get("market", {}).get("value") is not None),
            ("BTC", data.get("crypto", {}).get("btc", {}).get("usd") is not None),
            ("ETH", data.get("crypto", {}).get("eth", {}).get("usd") is not None),
            ("IMOEX", data.get("imoex", {}).get("value") is not None),
        ) if not ok
    ]
    data["notes"] = [f"Не удалось получить: {', '.join(missing)}. В сводке указано «н/д»."] if missing else []
    data["ok"] = not missing
    process_alerts(data)

    (ROOT / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (ROOT / "digest.md").write_text(build_digest(data), encoding="utf-8")

    print(f"OK: {data['generated_at_display']} | sources ok={data['ok']}")
    print(build_digest(data))
    if not data["ok"]:
        print("WARN: digest saved with partial data; exiting 0 to keep scheduled publish alive", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
