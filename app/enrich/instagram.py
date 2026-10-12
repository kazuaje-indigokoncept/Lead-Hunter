"""Instagram Graph API - Business Discovery (API oficial de Meta).

Requisitos: una cuenta de Instagram profesional propia vinculada a una página de
Facebook, una app de Meta y un token de acceso (Facebook Login) con permisos
instagram_basic y pages_read_engagement.
Solo funciona con cuentas objetivo profesionales (empresa o creador).
Límite: ~200 llamadas por hora por usuario de la app.
"""
import httpx

GRAPH = "https://graph.facebook.com/v25.0"
FIELDS = "username,name,biography,website,followers_count,media_count"


async def lookup(client: httpx.AsyncClient, ig_user_id: str, token: str, username: str,
                 version_url: str = GRAPH) -> dict:
    params = {"fields": f"business_discovery.username({username}){{{FIELDS}}}",
              "access_token": token}
    r = await client.get(f"{version_url}/{ig_user_id}", params=params, timeout=20)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text
        return {"error": msg[:200]}
    bd = r.json().get("business_discovery", {})
    return {
        "ig_followers": bd.get("followers_count"),
        "ig_bio": bd.get("biography"),
        "ig_website": bd.get("website"),
    }
