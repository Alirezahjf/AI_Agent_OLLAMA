"""Weather and currency/crypto actions — all SAFE (read-only).

Uses free public APIs that do not require authentication:
  * Weather: Open-Meteo (no API key needed)
  * Currency: exchangerate-api.com / coingecko
"""

from __future__ import annotations

import json
from typing import Any

import requests

from ..core.errors import AssistantError
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_info_services(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="weather",
        description=(
            "آب‌وهوای فعلی یک شهر (دما، رطوبت، باد، شرایط). "
            "از Open-Meteo استفاده می‌کند و نیاز به API key ندارد. SAFE."
        ),
        parameters={
            "city": {"type": "string", "description": "نام شهر (فارسی یا انگلیسی)"},
            "forecast_days": {"type": "integer", "description": "تعداد روز پیش‌بینی (0=فقط فعلی, حداکثر 7)"},
        },
        required=("city",),
    )(weather)

    registry.decorator(
        name="currency_rate",
        description=(
            "نرخ تبدیل ارز (مثلاً USD به IRR). از API رایگان استفاده می‌کند. SAFE."
        ),
        parameters={
            "from_currency": {"type": "string", "description": "ارز مبدأ (مثلاً USD, EUR, IRR)"},
            "to_currency": {"type": "string", "description": "ارز مقصد"},
            "amount": {"type": "number", "description": "مقدار (پیش‌فرض: 1)"},
        },
        required=("from_currency", "to_currency"),
    )(currency_rate)

    registry.decorator(
        name="crypto_price",
        description=(
            "قیمت رمزارز (BTC, ETH, ...) به دلار یا ارز دیگر. از CoinGecko API. SAFE."
        ),
        parameters={
            "coin": {"type": "string", "description": "نام رمزارز (bitcoin, ethereum, solana, ...)"},
            "vs_currency": {"type": "string", "description": "ارز مقصد (usd, eur, ...)"},
        },
        required=("coin",),
    )(crypto_price)

    registry.decorator(
        name="youtube_search",
        description=(
            "جست‌وجوی ویدیو در YouTube (عنوان، لینک، مدت، بازدید). SAFE."
        ),
        parameters={
            "query": {"type": "string"},
            "max_results": {"type": "integer", "description": "حداکثر نتیجه (پیش‌فرض 5)"},
        },
        required=("query",),
    )(youtube_search)

    registry.decorator(
        name="rss_feed",
        description=(
            "خواندن آخرین مطالب یک RSS feed. SAFE."
        ),
        parameters={
            "url": {"type": "string", "description": "آدرس RSS feed"},
            "limit": {"type": "integer", "description": "حداکثر مطالب (پیش‌فرض 10)"},
        },
        required=("url",),
    )(rss_feed)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def weather(*, city: str, forecast_days: int = 0, context: ActionContext) -> str:
    """Get weather using Open-Meteo free API (no key needed)."""
    city_name = str(city).strip()
    if not city_name:
        raise AssistantError("نام شهر خالی است")

    # Geocode the city
    try:
        geo_resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city_name, "count": 1, "language": "fa"},
            timeout=15,
        )
        geo_resp.raise_for_status()
        geo = geo_resp.json()
    except Exception as exc:
        raise AssistantError(f"جست‌وجوی شهر ناموفق بود: {exc}")

    results = geo.get("results", [])
    if not results:
        raise AssistantError(f"شهر «{city_name}» پیدا نشد. نام انگلیسی را امتحان کنید.")

    loc = results[0]
    lat, lon = loc["latitude"], loc["longitude"]
    display_name = loc.get("name", city_name)
    country = loc.get("country", "")

    # Get weather
    days = max(0, min(int(forecast_days or 0), 7))
    try:
        params: dict[str, Any] = {
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                       "wind_speed_10m,wind_direction_10m,weather_code,pressure_msl",
            "timezone": "auto",
        }
        if days > 0:
            params["daily"] = "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code,wind_speed_10m_max"
            params["forecast_days"] = days

        weather_resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params, timeout=15,
        )
        weather_resp.raise_for_status()
        data = weather_resp.json()
    except Exception as exc:
        raise AssistantError(f"دریافت آب‌وهوا ناموفق بود: {exc}")

    current = data.get("current", {})
    temp = current.get("temperature_2m", "?")
    feels = current.get("apparent_temperature", "?")
    humidity = current.get("relative_humidity_2m", "?")
    wind = current.get("wind_speed_10m", "?")
    wind_dir = current.get("wind_direction_10m", 0)
    code = current.get("weather_code", -1)

    condition = _wmo_code_to_text(code)
    wind_compass = _degrees_to_compass(wind_dir)

    lines = [
        f"🌤️ آب‌وهوای {display_name} ({country}):",
        f"  وضعیت: {condition}",
        f"  دما: {temp}°C (احساس: {feels}°C)",
        f"  رطوبت: {humidity}%",
        f"  باد: {wind} km/h ({wind_compass})",
    ]

    # Forecast
    daily = data.get("daily", {})
    if daily and daily.get("time"):
        lines.append(f"\n  📅 پیش‌بینی {days} روز:")
        for i, date in enumerate(daily["time"]):
            max_t = daily.get("temperature_2m_max", [])[i] if i < len(daily.get("temperature_2m_max", [])) else "?"
            min_t = daily.get("temperature_2m_min", [])[i] if i < len(daily.get("temperature_2m_min", [])) else "?"
            precip = daily.get("precipitation_sum", [])[i] if i < len(daily.get("precipitation_sum", [])) else 0
            wcode = daily.get("weather_code", [])[i] if i < len(daily.get("weather_code", [])) else -1
            cond = _wmo_code_to_text(wcode)
            lines.append(f"    {date}: {min_t}°C ~ {max_t}°C | {cond} | بارش: {precip}mm")

    return "\n".join(lines)


def _wmo_code_to_text(code: int) -> str:
    """Convert WMO weather code to Persian text."""
    codes = {
        0: "☀️ صاف", 1: "🌤️ اغلب صاف", 2: "⛅ نیمه‌ابری", 3: "☁️ ابری",
        45: "🌫️ مه", 48: "🌫️ مه یخ‌زده",
        51: "🌦️ نم‌نم", 53: "🌦️ باران ملایم", 55: "🌧️ باران شدید",
        56: "🌧️ باران یخ‌زده ملایم", 57: "🌧️ باران یخ‌زده شدید",
        61: "🌧️ باران کم", 63: "🌧️ باران متوسط", 65: "🌧️ باران زیاد",
        66: "🌧️ باران یخ‌زده کم", 67: "🌧️ باران یخ‌زده زیاد",
        71: "🌨️ برف کم", 73: "🌨️ برف متوسط", 75: "❄️ برف زیاد",
        77: "❄️ دانه‌های برف",
        80: "🌦️ رگبار کم", 81: "🌧️ رگبار متوسط", 82: "⛈️ رگبار شدید",
        85: "🌨️ رگبار برف کم", 86: "❄️ رگبار برف زیاد",
        95: "⛈️ طوفان رعدوبرق", 96: "⛈️ طوفان با تگرگ کم", 99: "⛈️ طوفان با تگرگ شدید",
    }
    return codes.get(code, f"کد {code}")


def _degrees_to_compass(deg: float | int) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    try:
        return dirs[int((float(deg) + 22.5) / 45) % 8]
    except (TypeError, ValueError, ZeroDivisionError):
        return "?"


@risk(Risk.SAFE)
def currency_rate(*, from_currency: str, to_currency: str,
                  amount: float = 1.0, context: ActionContext) -> str:
    """Get currency exchange rate from exchangerate-api.com (free tier)."""
    src = str(from_currency).strip().upper()
    dst = str(to_currency).strip().upper()
    amt = float(amount or 1.0)
    if not src or not dst:
        raise AssistantError("نام ارز خالی است")

    try:
        resp = requests.get(
            f"https://open.er-api.com/v6/latest/{src}",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise AssistantError(f"دریافت نرخ ارز ناموفق بود: {exc}")

    rates = data.get("rates", {})
    if dst not in rates:
        available = ", ".join(sorted(rates.keys())[:20])
        raise AssistantError(f"ارز {dst} شناخته‌شده نیست. نمونه‌ها: {available}")

    rate = rates[dst]
    result = amt * rate
    time_last = data.get("time_last_update_utc", "")
    return (
        f"💱 نرخ ارز:\n"
        f"  {amt} {src} = {result:,.2f} {dst}\n"
        f"  نرخ: 1 {src} = {rate:,.4f} {dst}\n"
        f"  آخرین به‌روزرسانی: {time_last}"
    )


@risk(Risk.SAFE)
def crypto_price(*, coin: str, vs_currency: str = "usd",
                 context: ActionContext) -> str:
    """Get crypto price from CoinGecko free API."""
    coin_id = str(coin).strip().lower()
    vs = str(vs_currency or "usd").strip().lower()
    if not coin_id:
        raise AssistantError("نام رمزارز خالی است")

    # Common aliases
    aliases = {
        "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
        "bnb": "binancecoin", "xrp": "ripple", "ada": "cardano",
        "doge": "dogecoin", "dot": "polkadot", "avax": "avalanche-2",
        "matic": "matic-network", "link": "chainlink", "uni": "uniswap",
        "atom": "cosmos", "ltc": "litecoin", "ton": "the-open-network",
        "trx": "tron", "near": "near", "apt": "aptos",
        "sui": "sui", "arb": "arbitrum", "op": "optimism",
    }
    coin_id = aliases.get(coin_id, coin_id)

    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": vs,
                "include_24hr_change": "true",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise AssistantError(f"دریافت قیمت رمزارز ناموفق بود: {exc}")

    if coin_id not in data:
        raise AssistantError(
            f"رمزارز «{coin_id}» پیدا نشد. نام‌های رایج: "
            "bitcoin, ethereum, solana, cardano, dogecoin"
        )

    info = data[coin_id]
    price = info.get(vs, 0)
    change = info.get(f"{vs}_24h_change", 0)
    mcap = info.get(f"{vs}_market_cap", 0)
    vol = info.get(f"{vs}_24h_vol", 0)

    change_icon = "📈" if (change or 0) >= 0 else "📉"
    return (
        f"₿ {coin_id.upper()} ({vs.upper()}):\n"
        f"  قیمت: {price:,.2f}\n"
        f"  تغییر ۲۴ ساعته: {change_icon} {change:+.2f}%\n"
        f"  ارزش بازار: {_format_large_number(mcap)}\n"
        f"  حجم ۲۴ ساعته: {_format_large_number(vol)}"
    )


def _format_large_number(n: float | int) -> str:
    n = float(n or 0)
    if n >= 1e12:
        return f"${n/1e12:.2f}T"
    if n >= 1e9:
        return f"${n/1e9:.2f}B"
    if n >= 1e6:
        return f"${n/1e6:.2f}M"
    if n >= 1e3:
        return f"${n/1e3:.1f}K"
    return f"${n:,.0f}"


@risk(Risk.SAFE)
def youtube_search(*, query: str, max_results: int = 5,
                   context: ActionContext) -> str:
    """Search YouTube using Invidious public API (no key needed)."""
    q = str(query).strip()
    if not q:
        raise AssistantError("عبارت جست‌وجو خالی است")
    limit = max(1, min(int(max_results or 5), 20))

    # Use Invidious public instances as a YouTube search proxy
    instances = [
        "https://vid.puffyan.us",
        "https://invidious.fdn.fr",
        "https://invidious.privacyredirect.com",
    ]
    for base in instances:
        try:
            resp = requests.get(
                f"{base}/api/v1/search",
                params={"q": q, "type": "video"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                break
        except Exception:
            continue
    else:
        # Fallback: DuckDuckGo HTML search for YouTube
        try:
            resp = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": f"site:youtube.com {q}"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            return f"🔍 نتایج YouTube برای «{q}»:\n  (Invidious در دسترس نیست — جست‌وجوی وب جایگزین استفاده شد)\n  " + resp.text[:500]
        except Exception as exc:
            raise AssistantError(f"جست‌وجوی YouTube ناموفق بود: {exc}")

    videos = [v for v in data if v.get("type") == "video"][:limit]
    if not videos:
        return f"ویدیویی برای «{q}» یافت نشد."

    lines = [f"🎬 نتایج YouTube برای «{q}» ({len(videos)} ویدیو):"]
    for v in videos:
        title = v.get("title", "?")
        vid_id = v.get("videoId", "")
        author = v.get("author", "?")
        length = v.get("lengthSeconds", 0)
        views = v.get("viewCount", 0)
        mins = int(length) // 60 if length else 0
        secs = int(length) % 60 if length else 0
        duration = f"{mins}:{secs:02d}" if mins else f"{secs}s"
        view_str = _format_large_number(views).replace("$", "")
        lines.append(
            f"  • {title}\n"
            f"    👤 {author} | ⏱️ {duration} | 👁️ {view_str}\n"
            f"    https://youtube.com/watch?v={vid_id}"
        )
    return "\n".join(lines)


@risk(Risk.SAFE)
def rss_feed(*, url: str, limit: int = 10, context: ActionContext) -> str:
    """Read an RSS/Atom feed."""
    feed_url = str(url).strip()
    if not feed_url:
        raise AssistantError("آدرس RSS خالی است")
    max_items = max(1, min(int(limit or 10), 50))

    try:
        resp = requests.get(feed_url, headers={"User-Agent": "LocalAssistant/1.0"}, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        raise AssistantError(f"دریافت RSS feed ناموفق بود: {exc}")

    # Simple XML parsing without external dependency
    import re
    from html import unescape

    text = resp.text
    items = re.findall(r"<item>(.*?)</item>", text, re.DOTALL)
    if not items:
        items = re.findall(r"<entry>(.*?)</entry>", text, re.DOTALL)
    if not items:
        return f"مطلبی در {feed_url} یافت نشد (فرمت RSS/Atom شناسایی نشد)."

    # Feed title
    feed_title_match = re.search(r"<title>(.*?)</title>", text[:2000])
    feed_title = unescape(feed_title_match.group(1)) if feed_title_match else feed_url

    lines = [f"📰 {feed_title} (آخرین {min(max_items, len(items))} مطلب):"]
    for item_xml in items[:max_items]:
        title_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item_xml, re.DOTALL)
        link_m = re.search(r"<link>(.*?)</link>", item_xml)
        if not link_m:
            link_m = re.search(r'<link[^>]*href=["\']([^"\']+)["\']', item_xml)
        date_m = re.search(r"<(?:pubDate|published|updated)>(.*?)</(?:pubDate|published|updated)>", item_xml)
        title = unescape(title_m.group(1).strip()) if title_m else "?"
        link = link_m.group(1).strip() if link_m else ""
        date = date_m.group(1).strip()[:25] if date_m else ""
        lines.append(f"  • {title}")
        if date:
            lines.append(f"    📅 {date}")
        if link:
            lines.append(f"    🔗 {link}")
    return "\n".join(lines)
