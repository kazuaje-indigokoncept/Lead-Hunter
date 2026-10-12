"""Directorio oficial de Colombia: RUES vía Datos Abiertos (datos.gov.co, API Socrata).

Dataset "Establecimientos - Agencias - Sucursales" (nb3d-v3n7): nombre comercial,
cámara de comercio, código CIIU, estado y fecha de matrícula.
No trae teléfono ni correo: por eso luego se cruza con Google Maps y la web.
Útil para encontrar negocios NUEVOS (matriculados recientemente) por actividad.
"""
import httpx

DATASET = "https://www.datos.gov.co/resource/nb3d-v3n7.json"


def _esc(s: str) -> str:
    return s.replace("'", "''").upper()


async def search(client: httpx.AsyncClient, keyword: str = "", camara: str = "",
                 ciiu: str = "", since: str = "", limit: int = 200,
                 app_token: str = ""):
    """since: fecha AAAAMMDD mínima de matrícula (ej. 20260101)."""
    where = ["estado_matricula = 'ACTIVA'"]
    if keyword:
        where.append(f"upper(razon_social) like '%{_esc(keyword)}%'")
    if camara:
        where.append(f"upper(camara_comercio) like '%{_esc(camara)}%'")
    if ciiu:
        codes = [c.strip() for c in ciiu.split(",") if c.strip().isdigit()]
        if codes:
            where.append("cod_ciiu_act_econ_pri in (" + ",".join(f"'{c}'" for c in codes) + ")")
    if since and since.isdigit():
        where.append(f"fecha_matricula >= '{since}'")
    params = {"$where": " AND ".join(where), "$limit": limit,
              "$order": "fecha_matricula DESC"}
    headers = {"X-App-Token": app_token} if app_token else {}
    r = await client.get(DATASET, params=params, headers=headers, timeout=60)
    r.raise_for_status()
    return [normalize(x) for x in r.json()]


def normalize(x: dict) -> dict:
    f = x.get("fecha_matricula", "")
    fecha = f"{f[:4]}-{f[4:6]}-{f[6:8]}" if len(f) == 8 else f
    return {
        "dedupe_key": f"rues:{x.get('codigo_camara')}:{x.get('matricula')}",
        "source": "rues",
        "name": (x.get("razon_social") or "").title(),
        "category": f"CIIU {x.get('cod_ciiu_act_econ_pri', '')}",
        "city": (x.get("camara_comercio") or "").title(),
        "extra": {
            "matricula": x.get("matricula"),
            "camara": x.get("camara_comercio"),
            "fecha_matricula": fecha,
            "ultimo_ano_renovado": x.get("ultimo_ano_renovado"),
            "ciiu": x.get("cod_ciiu_act_econ_pri"),
            "nit_propietario": x.get("numero_identificacion"),
        },
    }
