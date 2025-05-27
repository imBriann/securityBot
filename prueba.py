import requests, uuid, time

GRAPH_URL = "https://graph.facebook.com/v18.0"
TOKEN     = "EAAIUbXSn5WcBO8PZAXbnab3l7ELqiHZBekK2EMnp5e6Em6MXgat92DkNd2QmbY3GAoKHi8EBEqjzoyuRTl15ViXTcewgFIqmhfgVfUyW5Btu1Gr6cGP6NdjndmBaFrHXKoPZBlIoh9tdQO6GiQ1D1RF4I7uv7c39ls4CwPMboyq43tMzn46ZCtY25C76mEZClIt4ZD"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "User-Agent": "curl/7.64.1"        # mitiga 404 por agente
}

def get_media_url(media_id: str) -> str:
    r = requests.get(f"{GRAPH_URL}/{media_id}", headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()["url"]             # enlace efímero (~5 min)

def download_media(media_id: str, max_attempts: int = 2) -> str:
    url = get_media_url(media_id)
    for attempt in range(max_attempts):
        resp = requests.get(url, headers=HEADERS, stream=True, timeout=60)
        if resp.status_code == 200:
            fname = f"img_{media_id}_{uuid.uuid4().hex}.jpg"
            with open(fname, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            return fname
        elif resp.status_code == 404 and attempt == 0:
            # enlace expiró: pedir uno nuevo y reintentar
            url = get_media_url(media_id)
            continue
        resp.raise_for_status()        # otros errores

    raise RuntimeError("No se pudo descargar el archivo.")

# Uso
file_path = download_media("1042247794637553")
print("Guardado en:", file_path)
