"""Google Places API (New) - Text Search.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
Máx. 20 resultados por página y 60 por consulta. Para cubrir más negocios,
divide la búsqueda en más palabras clave y más zonas (barrios, localidades).
"""
import asyncio
import httpx

URL = "https://places.googleapis.com/v1/places:searchText"

# Estos campos caen en el SKU "Text Search Enterprise" (por teléfono/web/rating).
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.nationalPhoneNumber", "places.internationalPhoneNumber",
    "places.websiteUri", "places.rating", "places.userRatingCount",
    "places.googleMapsUri", "places.primaryTypeDisplayName", "places.types",
    "places.location", "places.businessStatus", "nextPageToken",
])


class PlacesError(Exception):
    pass


async def search(client: httpx.AsyncClient, api_key: str, text: str,
                 max_pages: int = 3, language: str = "es", region: str = "CO",
                 min_rating: float | None = None, on_call=None):
    """Devuelve una lista de leads normalizados para una consulta de texto."""
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK,
               "Content-Type": "application/json"}
    body = {"textQuery": text, "pageSize": 20, "languageCode": language}
    if region:
        body["regionCode"] = region
    if min_rating:
        body["minRating"] = min_rating

    results, page = [], 0
    while page < max_pages:
        r = await client.post(URL, headers=headers, json=body, timeout=30)
        if on_call:
            on_call()
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise PlacesError(f"Places API {r.status_code}: {msg[:300]}")
        data = r.json()
        results += [normalize(p) for p in data.get("places", [])]
        token = data.get("nextPageToken")
        page += 1
        if not token:
            break
        body["pageToken"] = token
        await asyncio.sleep(1.5)  # el token tarda un momento en activarse
    return results


def normalize(p: dict) -> dict:
    loc = p.get("location") or {}
    intl = p.get("internationalPhoneNumber") or ""
    return {
        "dedupe_key": "gmaps:" + p.get("id", ""),
        "source": "google_maps",
        "name": (p.get("displayName") or {}).get("text"),
        "category": (p.get("primaryTypeDisplayName") or {}).get("text")
                    or ", ".join((p.get("types") or [])[:2]),
        "address": p.get("formattedAddress"),
        "phone": p.get("nationalPhoneNumber") or intl,
        "whatsapp": to_wa(intl),
        "website": p.get("websiteUri"),
        "rating": p.get("rating"),
        "reviews": p.get("userRatingCount"),
        "maps_url": p.get("googleMapsUri"),
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "business_status": p.get("businessStatus"),
    }


def to_wa(intl_phone: str) -> str | None:
    """Convierte +57 300 123 4567 en enlace wa.me solo si parece celular."""
    digits = "".join(ch for ch in intl_phone or "" if ch.isdigit())
    # Colombia: celulares 57 + 3xx xxx xxxx. Otros países: se deja el número completo.
    if digits.startswith("57") and len(digits) == 12 and digits[2] == "3":
        return f"https://wa.me/{digits}"
    if digits and not digits.startswith("57") and len(digits) >= 10:
        return f"https://wa.me/{digits}"
    return None
