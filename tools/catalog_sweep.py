#!/usr/bin/env python
"""Barrido de catalogos de elevacion < 30 m para Chile (SOLO LECTURA).

Consulta APIs/paginas publicas (sin descargar datasets, sin cuentas, sin enviar
formularios) y escribe un JSON con los hallazgos en
poc/tools/out/catalog_sweep_<fecha>.json.

Uso:  python tools/catalog_sweep.py [--only ckan,geoportal,ot,datacite,zenodo,pangaea,figshare,dryad,cems,pages,arcgis,ziprange]
      python tools/catalog_sweep.py --rm [--only ...]   # barrido enfocado en la Region Metropolitana (RM)

Con --rm se cambian los puntos de referencia (centros de comunas piloto, publicos), la caja de busqueda,
las consultas y las paginas a revisar, y el resultado se escribe en poc/tools/out/catalog_sweep_rm_<fecha>.json.
Solo informacion publica: no se usa ninguna capa de predios, de tramites ni de clientes.

Cada hallazgo: source, id, title, provider, url, license, date, resolution_hint,
pilots {nombre: 'yes'|'no'|'unknown' + como se chequeo}, notes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(parents=True, exist_ok=True)
TODAY = dt.date.today().isoformat()

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) catalog-sweep/1.0 (read-only research)"}
SESSION = requests.Session()
SESSION.headers.update(H)

# Modo nacional (regiones, en pausa): puntos de referencia = centros aproximados de comunas (informacion publica).
# La RM se barre con --rm (ver RM_PILOTS mas abajo).
PILOTS = {
    "Vina_del_Mar": (-71.552, -33.024),
    "Puerto_Varas": (-72.985, -41.318),
    "Coquimbo": (-71.343, -29.953),
}
CHILE_BBOX = (-76.0, -56.0, -66.0, -17.0)  # minx, miny, maxx, maxy

# ---- Modo --rm: Region Metropolitana. Puntos = centros aproximados de comunas (informacion publica, no predios).
RM_PILOTS = {
    "P1_Nunoa_Providencia": (-70.600, -33.445),
    "P2_Maipu_Pudahuel": (-70.780, -33.480),
    "P3_Penalolen_LaReina": (-70.520, -33.475),
    "P4_LoBarnechea_LasCondes": (-70.510, -33.375),
    "P5_PuenteAlto_LaFlorida": (-70.560, -33.570),
    "P6_Renca_Huechuraba": (-70.680, -33.380),
    "P7_Colina_Chicureo": (-70.670, -33.200),
    "P8_Pirque_SanJoseMaipo": (-70.500, -33.660),
}
RM_BBOX = (-71.8, -34.4, -69.8, -32.9)
RM_CHILE_RE = re.compile(r"chile|santiago|metropolitan|maipo|mapocho|san ram|pirque|colina|barnechea|andes|"
                         r"puente alto|maip.|pudahuel|renca|huechuraba|cord.ll?era|piedemonte|piedmont", re.I)
RM_CKAN_Q = ["lidar", "LiDAR Santiago", "modelo digital de elevación Santiago", "modelo digital de terreno Región Metropolitana",
             "curvas de nivel Región Metropolitana", "curvas de nivel Santiago", "ortofoto Región Metropolitana", "ortofoto Santiago",
             "restitución Santiago", "aerofotogramétrico Región Metropolitana", "topografía Santiago", "cartografía base Región Metropolitana",
             "quebradas Santiago", "falla San Ramón", "Cajón del Maipo", "Mapocho", "río Maipo", "nube de puntos Santiago",
             "DEM Región Metropolitana", "MDT Santiago", "MDE Santiago", "altimetría Santiago", "plan regulador metropolitano",
             "riesgo remoción en masa Región Metropolitana"]
RM_GEO_TERMS = ["lidar", "Metropolitana", "Santiago", "Maipo", "Mapocho", "San Ramon", "Pirque", "Colina", "curvas de nivel",
                "modelo digital", "MDE", "MDT", "DEM", "topograf", "ortofoto", "restitucion", "aerofotogram", "altimetr",
                "nube de puntos", "quebrada", "SAF", "Servicio Aerofotogrametrico", "pendiente"]
RM_REPO_Q = [
    '"Santiago de Chile" AND (lidar OR LiDAR OR DEM OR DSM OR DTM OR "digital elevation" OR photogrammetry OR UAV OR "point cloud")',
    '"San Ramón fault" OR "San Ramon fault" OR "Falla San Ramón"',
    '(Maipo OR Mapocho OR "Cajón del Maipo") AND (DEM OR lidar OR topography OR "elevation model" OR UAV)',
    '(Pirque OR Colina OR "Lo Barnechea" OR Peñalolén OR "La Reina" OR "Las Condes") AND (DEM OR lidar OR topography OR UAV)',
    '"Metropolitan Region" AND Chile AND (lidar OR DEM OR DTM OR photogrammetry OR "point cloud")',
    '("Andes" AND Santiago) AND (lidar OR DEM OR "elevation model" OR "high-resolution topography")',
    '("quebrada de Macul" OR "Quebrada de Ramón" OR "San Cristóbal" OR "cerro Manquehue") AND (DEM OR lidar OR topography)',
    'Chile AND (CIGIDEN OR CEDEUS) AND (lidar OR DEM OR UAV OR "point cloud" OR topography)',
]
RM_ZEN_Q = ["Santiago Chile lidar", "Santiago Chile DEM", "Santiago de Chile digital elevation model", "San Ramón fault",
            "Maipo lidar", "Maipo DEM", "Mapocho", "Andes Santiago DEM", "Santiago Chile UAV orthomosaic", "Pirque", "Colina Chile DEM",
            "Cajón del Maipo UAV", "Chile CIGIDEN", "Chile CEDEUS", "Santiago building height", "Santiago canopy height",
            "Metropolitan Region Chile elevation", "Santiago photogrammetry"]
RM_PANGAEA_QS = ['(Santiago OR Maipo OR Mapocho OR "San Ramón" OR Pirque) AND Chile AND (lidar OR DEM OR elevation OR topography OR UAV)',
                 'Chile AND "Metropolitan Region" AND (lidar OR DEM OR photogrammetry)',
                 'Chile AND (Andes OR cordillera) AND ("point cloud" OR "digital elevation") AND Santiago']
RM_FIG_Q = ["Santiago Chile lidar", "Santiago Chile DEM", "San Ramón fault", "Maipo DEM", "Santiago Chile topography", "Santiago UAV Chile"]
RM_DRYAD_Q = ["Santiago Chile", "San Ramón fault", "Maipo", "Santiago lidar", "Andes Santiago DEM"]
RM_PROBE_PAGES = {
    "GORE Metropolitano": ["https://www.gobiernosantiago.cl/", "https://www.gobiernosantiago.cl/transparencia/"],
    "Seremi MINVU RM": ["https://metropolitana.minvu.gob.cl/", "https://ide.minvu.cl/", "https://geoide.minvu.cl/"],
    "MOP DOH": ["https://doh.mop.gob.cl/", "https://www.mop.gob.cl/"],
    "MOP DGA": ["https://dga.mop.gob.cl/", "https://snia.mop.gob.cl/"],
    "SAF": ["https://www.saf.cl/"], "IGM": ["https://www.igm.cl/"], "CIREN": ["https://www.ciren.cl/"],
    "CIGIDEN": ["https://cigiden.cl/", "https://cigidenweb.ing.uc.cl/"],
    "CEDEUS": ["https://www.cedeus.cl/"],
    "Nunoa": ["https://www.nunoa.cl/"], "Providencia": ["https://providencia.cl/provi/site/edic/base/port/mapas.html"],
    "Las Condes": ["https://datos.lascondes.cl/", "https://www.lascondes.cl/"],
    "Vitacura": ["https://vitacura.cl/"], "Lo Barnechea": ["https://lobarnechea.cl/", "https://opendata-vecinos-sitmlb.hub.arcgis.com/"],
    "La Reina": ["https://lareina.cl/"], "Penalolen": ["https://www.penalolen.cl/"], "La Florida": ["https://www.laflorida.cl/"],
    "Puente Alto": ["https://www.mpuentealto.cl/", "https://datos.mpuentealto.cl/home"], "Maipu": ["https://www.maipu.cl/"],
    "Pudahuel": ["https://www.pudahuel.cl/"], "Renca": ["https://renca.cl/"], "Huechuraba": ["https://huechuraba.cl/"],
    "Colina": ["https://www.colina.cl/", "https://colina.filedom.cl/mapas/"], "Pirque": ["https://www.pirque.cl/"],
    "San Jose de Maipo": ["https://www.sanjosedemaipo.cl/"],
    "OpenTopography": ["https://opentopography.org/"],
    "Digimapas": ["http://www.digimapas.cl/"],
}
# Servicios ArcGIS REST publicos a enumerar (solo nombres de servicios, sin descargar datos)
ARCGIS_ROOTS = ["https://geoide.minvu.cl/server/rest/services"]
# ZIP grande a inspeccionar solo con HEAD / Range (no se descarga)
ZIP_URLS = {
    "MTT/SECTRA Topografias Valparaiso-OHiggins-Metropolitana (geoportal.cl 35914)":
        "https://geoportal.cl/geoportal/catalog/download/41419ca4-d999-36aa-acf8-cc132cf4c403",
}

ELEV_RE = re.compile(
    r"(lidar|\bDEM\b|\bDSM\b|\bDTM\b|\bMDE\b|\bMDT\b|\bMDS\b|elevaci|elevation|topograf|topograph|"
    r"altimetr|curvas? de nivel|contour|photogramm|fotogram|aerofotogram|point ?cloud|nube de puntos|"
    r"terrain|terreno|restituci|ortofoto|orthophoto|orthomosaic|ortomosaico|digital surface|modelo digital|"
    r"UAV|drone|dron\b|SfM|structure.from.motion|relieve|pendiente|slope)",
    re.I,
)
CHILE_RE = re.compile(r"chile|santiago|valpara|coquimbo|andes|atacama|patagon|puerto varas|llanquihue|vi.a del mar|barnechea|.u.oa", re.I)

LOG: list[str] = []
PANGAEA_QS: list[str] = []
FIG_Q: list[str] = []
DRYAD_Q: list[str] = []
MODE = "national"


def log(*a):
    s = " ".join(str(x) for x in a)
    LOG.append(s)
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode(), flush=True)


def get(url, *, params=None, timeout=60, retries=2, **kw):
    last = None
    for i in range(retries + 1):
        try:
            return SESSION.get(url, params=params, timeout=timeout, **kw)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    log("  ! GET fallo", url, last)
    return None


# ----------------------------------------------------------------- geometria
def _pip(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-300) + xi:
            inside = not inside
        j = i
    return inside


def pt_in_polygon(x, y, rings):
    """rings[0]=exterior, resto=huecos."""
    if not rings or not _pip(x, y, rings[0]):
        return False
    return not any(_pip(x, y, h) for h in rings[1:])


def pilots_vs_geojson(gj):
    polys = []

    def walk(g):
        if not g:
            return
        t = g.get("type")
        if t == "FeatureCollection":
            for f in g.get("features", []):
                walk(f)
        elif t == "Feature":
            walk(g.get("geometry"))
        elif t == "Polygon":
            polys.append(g["coordinates"])
        elif t == "MultiPolygon":
            polys.extend(g["coordinates"])
        elif t == "GeometryCollection":
            for q in g.get("geometries", []):
                walk(q)

    walk(gj)
    if not polys:
        return {k: "unknown (sin huella)" for k in PILOTS}
    res = {}
    for name, (lon, lat) in PILOTS.items():
        h = any(pt_in_polygon(lon, lat, p) for p in polys)
        res[name] = "yes (punto dentro de huella)" if h else "no (punto fuera de huella)"
    return res


def pilots_vs_bbox(w, s, e, n, how="bbox del metadato (grueso)"):
    res = {}
    for name, (lon, lat) in PILOTS.items():
        inb = w <= lon <= e and s <= lat <= n
        res[name] = f"{'yes' if inb else 'no'} ({how})"
    return res


def unknown_pilots(why="sin coordenadas en metadatos"):
    return {k: f"unknown ({why})" for k in PILOTS}


def clean(txt):
    txt = html.unescape(txt or "")
    txt = re.sub(r"&amp;?(amp;)+", "&", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def hit(**kw):
    base = dict(source="", id="", title="", provider="", url="", license="", date="",
                resolution_hint="", pilots=unknown_pilots(), notes="", access="", relevant=False)
    base.update(kw)
    return base


def dist_km(lon, lat, lo, la):
    return (((lon - lo) * 95) ** 2 + ((lat - la) * 111) ** 2) ** 0.5


# --------------------------------------------------------------------- CKAN
CKAN_Q = ["lidar", "LiDAR", "MDE", "MDT", "DEM", "modelo digital", "modelo digital de elevación",
          "modelo digital de terreno", "curvas de nivel", "altimetría", "restitución", "ortofoto",
          "levantamiento aerofotogramétrico", "aerofotogramétrico", "fotogrametría", "topografía",
          "elevación", "nube de puntos", "DSM", "batimetría", "dron", "RPAS", "relieve", "pendiente"]


def sweep_ckan():
    out = []
    seen = {}
    counts = {}
    base = "https://datos.gob.cl/api/3/action/package_search"
    for q in CKAN_Q:
        r = get(base, params={"q": q, "rows": 100})
        if r is None or not r.ok:
            counts[q] = "error"
            continue
        j = r.json()["result"]
        counts[q] = j["count"]
        for p in j["results"]:
            seen[p["id"]] = p
    log(f"[ckan] consultas={len(CKAN_Q)} paquetes unicos={len(seen)}")
    for p in seen.values():
        title = clean(p.get("title"))
        text = " ".join([title, clean(p.get("notes")), " ".join((x.get("name") or "") for x in p.get("resources", []))])
        relevant = bool(ELEV_RE.search(text))
        out.append(hit(
            source="datos.gob.cl (CKAN)", id=p["name"], title=title,
            provider=(p.get("organization") or {}).get("title", ""),
            url=f"https://datos.gob.cl/dataset/{p['name']}",
            license=f"{p.get('license_id')} / {p.get('license_title')}",
            date=(p.get("metadata_modified") or "")[:10],
            notes=clean(p.get("notes"))[:300],
            access="; ".join(sorted({(x.get('format') or '?') + ':' + (x.get('url') or '')[:100]
                                      for x in p.get("resources", [])}))[:600],
            relevant=relevant,
        ))
    return {"hits": out, "query_counts": counts}


# ---------------------------------------------------------------- Geoportal
GEO = "https://geoportal.cl/geoportal/catalog"
GEO_TERMS = ["lidar", "MDE", "MDT", "DEM", "modelo digital", "curvas de nivel", "curva", "altimetr",
             "topograf", "restitucion", "ortofoto", "aerofotogram", "fotogram", "relieve", "elevacion",
             "pendiente", "nube de puntos", "batimetr", "dron", "RPAS", "orto", "terreno", "SRTM", "ALOS"]


def _geo_list(params):
    items = {}
    page = 1
    total = None
    while True:
        p = dict(params)
        p.update({"action": "search", "per-page": 50, "page": page})
        r = get(GEO, params=p)
        if r is None or not r.ok:
            break
        r.encoding = "utf-8"
        t = r.text
        m = re.search(r"de\s*([0-9]+)\s*registros", re.sub(r"<[^>]+>", " ", t))
        if m:
            total = int(m.group(1))
        found = re.findall(r'href="https://geoportal\.cl/geoportal/catalog/(\d+)/([^"]+)"', t)
        new = 0
        for idn, slug in found:
            if idn not in items:
                items[idn] = requests.utils.unquote(slug)
                new += 1
        if new == 0 or (total is not None and len(items) >= total) or page >= 30:
            break
        page += 1
    return items, total


def _geo_detail(idn, slug):
    url = f"https://geoportal.cl/geoportal/catalog/{idn}/{requests.utils.quote(slug)}"
    r = get(url)
    if r is None or not r.ok:
        return None
    r.encoding = "utf-8"
    t = r.text
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", t, flags=re.S)
    txt = clean(re.sub(r"</(div|p|li|tr|h\d)>", " ", body))
    d = {"url": url, "id": idn, "title": slug}
    m = re.search(r"Resumen del recurso:(.*?)Fecha del Recurso", txt, re.S)
    d["summary"] = clean(m.group(1))[:900] if m else ""
    m = re.search(r"Fecha del Recurso \(yyyy-mm-dd\):\s*([0-9\-]+)", txt)
    d["date"] = m.group(1) if m else ""
    m = re.search(r"Nombre de la Organizaci.n:\s*(.*?)\s*E-Mail", txt)
    d["provider"] = m.group(1).strip() if m else ""
    bb = {}
    for k, lab in (("w", "Oeste"), ("e", "Este"), ("s", "Sur"), ("n", "Norte")):
        m = re.search(rf"Coordenadas {lab}:\s*(-?[0-9.]+)", txt)
        if m:
            bb[k] = float(m.group(1))
    d["bbox"] = bb
    d["downloads"] = sorted(set(re.findall(r'href="(https://geoportal\.cl/geoportal/catalog/download/[^"]+)"', t)))
    m = re.search(r'href="(https://geoportal\.cl/geoportal/catalog/xml\?identifier=[^"]+)"', t)
    d["xml"] = html.unescape(m.group(1)) if m else ""
    d["constraints"] = ""
    d["spatialResolution"] = ""
    if d["xml"]:
        rx = get(d["xml"])
        if rx is not None and rx.ok:
            x = rx.text
            parts = []
            for tag in ("useLimitation", "otherConstraints", "accessConstraints", "useConstraints"):
                for mm in re.finditer(rf"<gmd:{tag}[^>]*>(.*?)</gmd:{tag}>", x, re.S):
                    c = clean(mm.group(1))
                    if c:
                        parts.append(f"{tag}:{c}")
            cons = re.search(r"<gmd:resourceConstraints>(.*?)</gmd:resourceConstraints>", x, re.S)
            if cons and not parts:
                c = clean(cons.group(1))
                if c:
                    parts.append(c)
            d["constraints"] = " | ".join(parts)[:600]
            mm = re.search(r"<gmd:spatialResolution>.*?</gmd:spatialResolution>", x, re.S)
            d["spatialResolution"] = clean(mm.group(0))[:200] if mm else ""
    return d


def sweep_geoportal():
    out = []
    elev, tot = _geo_list({"categories[]": "elevation"})
    log(f"[geoportal] categoria elevation: {len(elev)} (declarado {tot})")
    term_ids = {}
    term_counts = {}
    for term in GEO_TERMS:
        items, _ = _geo_list({"searchText": term})
        term_counts[term] = len(items)
        for idn, slug in items.items():
            term_ids.setdefault(idn, slug)
    log(f"[geoportal] busqueda por termino: {len(term_ids)} fichas unicas en {len(GEO_TERMS)} terminos")
    todo = dict(term_ids)
    todo.update(elev)
    for idn, slug in sorted(todo.items(), key=lambda kv: int(kv[0])):
        in_cat = idn in elev
        if not in_cat and not ELEV_RE.search(slug):
            h = hit(source="geoportal.cl (IDE Chile)", id=idn, title=slug,
                    url=f"https://geoportal.cl/geoportal/catalog/{idn}/{requests.utils.quote(slug)}",
                    notes="encontrado por texto libre; titulo no sugiere elevacion; detalle no leido")
            h["in_elevation_category"] = False
            out.append(h)
            continue
        d = _geo_detail(idn, slug)
        if not d:
            continue
        bb = d["bbox"]
        pil = pilots_vs_bbox(bb["w"], bb["s"], bb["e"], bb["n"]) if len(bb) == 4 else unknown_pilots("sin bbox")
        txt = d["summary"] + " " + slug
        res = re.findall(r"(?:p[ií]xel|resoluci[oó]n)\s*(?:de\s*)?(\d+(?:[.,]\d+)?)\s*m", txt, re.I)
        res += re.findall(r"(\d+(?:[.,]\d+)?)\s*(?:m\b|metros?|mt)\s*(?:de\s*|por\s*)?(?:p[ií]xel|resoluci)", txt, re.I)
        h = hit(source="geoportal.cl (IDE Chile)", id=idn, title=clean(slug), provider=d["provider"],
                url=d["url"], date=d["date"],
                license=d["constraints"] or "no declarada en ficha/XML (el paquete CKAN de la categoria es CC BY)",
                resolution_hint=", ".join(res), pilots=pil, notes=d["summary"][:500],
                access=("descarga: " + "; ".join(d["downloads"])) if d["downloads"] else "sin descarga directa (servicio/analogo)",
                relevant=True)
        h["in_elevation_category"] = in_cat
        out.append(h)
    return {"hits": out, "term_counts": term_counts, "elevation_total": tot}


# ---------------------------------------------------------- OpenTopography
OT_API = "https://portal.opentopography.org/API/otCatalog"


def _ot_license(doi_url):
    r = get(doi_url, allow_redirects=True)
    if r is None or not r.ok:
        return "", "", r.url if r is not None else ""
    b = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
    txt = clean(re.sub(r"</(div|p|li|tr|h\d)>", " ", b))
    lic = re.search(r"Use License\s*:?\s*(.{0,60}?)\s*(?:Dataset keywords|Classifications|Survey|Coordinate|$)", txt)
    add = re.search(r"Additional Information\s*:?\s*(.{0,200}?)\s*Use License", txt)
    return (lic.group(1).strip() if lic else ""), (add.group(1).strip() if add else ""), r.url


def sweep_ot():
    out = []
    seen = set()
    for fmt in ("PointCloud", "Raster"):
        r = get(OT_API, params={"productFormat": fmt, "minx": CHILE_BBOX[0], "miny": CHILE_BBOX[1],
                                "maxx": CHILE_BBOX[2], "maxy": CHILE_BBOX[3], "detail": "true",
                                "outputFormat": "json", "include_federated": "true"})
        if r is None or not r.ok:
            log("[ot] error", fmt)
            continue
        for dd in r.json().get("Datasets", []):
            d = dd["Dataset"]
            key = (fmt, d["identifier"]["value"])
            if key in seen:
                continue
            seen.add(key)
            gj = (d.get("spatialCoverage") or {}).get("geo", {}).get("geojson")
            pil = pilots_vs_geojson(gj) if gj else unknown_pilots("sin huella")
            ident = d["identifier"]["value"]
            lic, add, landing = ("", "", "")
            if ident.startswith("OTDS"):
                lic, add, landing = _ot_license(d.get("url", ""))
            vm = {v["name"]: v["value"] for v in d.get("variableMeasured", []) if isinstance(v, dict)}
            h = hit(source=f"OpenTopography ({fmt})", id=ident, title=d["name"],
                    provider="OpenTopography / " + clean(d.get("citation", ""))[:80],
                    url=landing or d.get("url", ""), date=d.get("dateCreated", ""),
                    license=(lic + (" | " + add if add else "")).strip(" |") or "producto global: ver pagina del dataset",
                    resolution_hint=json.dumps(vm, ensure_ascii=False), pilots=pil,
                    notes=clean(d.get("description", ""))[:300],
                    access="OpenTopography API/descarga", relevant=True)
            out.append(h)
    log(f"[ot] datasets unicos: {len(out)}")
    return {"hits": out}


# ----------------------------------------------------------------- DataCite
DC_URL = "https://api.datacite.org/dois"
REPO_Q = [
    "Chile AND (lidar OR LiDAR)",
    "Chile AND (DEM OR DSM OR DTM OR \"digital elevation model\" OR \"digital terrain model\" OR \"digital surface model\")",
    "Chile AND (photogrammetry OR \"structure from motion\" OR UAV OR drone)",
    "Chile AND (\"point cloud\" OR topography OR \"high-resolution topography\")",
    "Chile AND (\"airborne laser\" OR ALS OR \"laser scanning\")",
    "\"Santiago de Chile\" AND (DEM OR lidar OR elevation)",
    "(\"Viña del Mar\" OR Valparaíso OR Valparaiso) AND (DEM OR lidar OR elevation OR topography)",
    "(Coquimbo OR \"La Serena\") AND (DEM OR lidar OR elevation OR topography)",
    "(\"Puerto Varas\" OR Llanquihue OR \"Los Lagos\") AND (DEM OR lidar OR elevation OR topography)",
    "(\"Lo Barnechea\" OR \"San Ramón fault\" OR \"Andes Santiago\") AND (DEM OR lidar OR elevation OR topography)",
]
DC_REPOS = ["gfz.gfz", "cern.zenodo", "pangaea.pangaea", "dryad.dryad", "figshare.ars", "ethz.da-rd"]


def _dc_query(q, client=None, size=100, pages=3):
    res = []
    for pg in range(1, pages + 1):
        p = {"query": q, "page[size]": size, "page[number]": pg}
        if client:
            p["client-id"] = client
        r = get(DC_URL, params=p)
        if r is None or not r.ok:
            break
        j = r.json()
        res.extend(j.get("data", []))
        if len(j.get("data", [])) < size:
            break
    return res


def _dc_to_hit(a, src):
    at = a["attributes"]
    titles = "; ".join(t.get("title", "") for t in at.get("titles", []))
    desc = " ".join(d.get("description", "") for d in at.get("descriptions", []))
    rights = "; ".join(
        (r.get("rights") or "") + (f" [{r.get('rightsIdentifier')}]" if r.get("rightsIdentifier") else "")
        + (f" <{r.get('rightsUri')}>" if r.get("rightsUri") else "") for r in at.get("rightsList", []))
    pil = unknown_pilots("sin geoLocation en DataCite")
    gl_txt = []
    for g in at.get("geoLocations") or []:
        bb = g.get("geoLocationBox")
        pt = g.get("geoLocationPoint")
        try:
            if bb:
                w, e = float(bb["westBoundLongitude"]), float(bb["eastBoundLongitude"])
                s, n = float(bb["southBoundLatitude"]), float(bb["northBoundLatitude"])
                gl_txt.append(f"box[{w:.2f},{s:.2f},{e:.2f},{n:.2f}]")
                if (e - w) < 20:
                    pil = pilots_vs_bbox(w, s, e, n, "geoLocationBox DataCite")
            elif pt:
                gl_txt.append(f"pt[{pt.get('pointLongitude')},{pt.get('pointLatitude')}]")
                lo, la = float(pt["pointLongitude"]), float(pt["pointLatitude"])
                pil = {n: f"unknown (punto del dataset a {dist_km(lo, la, x, y):.0f} km; huella no declarada)"
                       for n, (x, y) in PILOTS.items()}
            elif g.get("geoLocationPolygon"):
                gl_txt.append("polygon")
        except Exception:  # noqa: BLE001
            pass
        if g.get("geoLocationPlace"):
            gl_txt.append(str(g["geoLocationPlace"])[:60])
    pub = at.get("publisher")
    pub = pub if isinstance(pub, str) else (pub or {}).get("name", "")
    return hit(source=src, id=a["id"], title=clean(titles)[:200], provider=pub or "",
               url=at.get("url") or f"https://doi.org/{a['id']}",
               license=rights or "no declarada en DataCite", date=str(at.get("publicationYear") or ""),
               pilots=pil, notes=clean(desc)[:350], access="; ".join(gl_txt))


def sweep_datacite():
    seen = {}
    qlog = {}
    for q in REPO_Q:
        data = _dc_query(q)
        qlog[q] = len(data)
        for a in data:
            seen[a["id"]] = a
    for client in DC_REPOS:
        data = _dc_query("Chile AND (lidar OR DEM OR DSM OR DTM OR photogrammetry OR \"point cloud\" OR topography)", client)
        qlog[f"[{client}] Chile+topo"] = len(data)
        for a in data:
            seen[a["id"]] = a
    out = []
    for a in seen.values():
        h = _dc_to_hit(a, "DataCite (" + (a["attributes"].get("clientId") or "?") + ")")
        txt = h["title"] + " " + h["notes"]
        h["relevant"] = bool(ELEV_RE.search(txt)) and bool(CHILE_RE.search(txt + " " + h["access"]))
        out.append(h)
    log(f"[datacite] unicos={len(out)} relevantes={sum(h['relevant'] for h in out)}")
    return {"hits": out, "query_counts": qlog}


# ------------------------------------------------------------------- Zenodo
ZEN = "https://zenodo.org/api/records"
ZEN_Q = ["Chile lidar", "Chile DEM", "Chile \"digital elevation model\"", "Chile DSM", "Chile \"digital terrain model\"",
         "Chile photogrammetry", "Chile \"point cloud\"", "Chile UAV orthomosaic DEM", "Santiago Chile DEM",
         "Valparaíso DEM", "\"Viña del Mar\" DEM lidar", "Coquimbo DEM", "\"Puerto Varas\" DEM lidar",
         "\"Lo Barnechea\"", "Ñuñoa", "Chile building heights", "Chile canopy height", "Chile terrain elevation 10 m"]


def sweep_zenodo():
    seen = {}
    qc = {}
    for q in ZEN_Q:
        for page in (1, 2, 3, 4):  # anonimo: maximo 25 por pagina
            r = get(ZEN, params={"q": q, "size": 25, "page": page, "type": "dataset"})
            if r is None or not r.ok:
                qc[q] = f"error {r.status_code if r is not None else 'net'}"
                break
            j = r.json()
            qc[q] = j["hits"]["total"]
            for rec in j["hits"]["hits"]:
                seen[rec["id"]] = rec
            if len(j["hits"]["hits"]) < 25:
                break
            time.sleep(0.6)
        time.sleep(0.6)
    out = []
    for rec in seen.values():
        m = rec["metadata"]
        title = m.get("title", "")
        desc = clean(m.get("description", ""))
        lic = m.get("license")
        lic = lic.get("id", "") if isinstance(lic, dict) else str(lic or "")
        pil = unknown_pilots("sin location en Zenodo")
        lt = []
        for lo in m.get("locations") or []:
            if "lat" in lo:
                lt.append(f"{lo.get('place', '')}[{lo['lon']},{lo['lat']}]")
        files = rec.get("files") or []
        size = sum(f.get("size", 0) for f in files)
        h = hit(source="Zenodo", id=str(rec["id"]), title=clean(title)[:200],
                provider=", ".join(c.get("name", "") for c in m.get("creators", [])[:3]),
                url=rec["links"].get("html") or rec["links"].get("self_html") or f"https://zenodo.org/records/{rec['id']}", license=lic or "no declarada", date=m.get("publication_date", ""),
                pilots=pil, notes=desc[:350], access=f"{len(files)} archivos, {size / 1e6:.1f} MB; ubic: {'; '.join(lt)[:150]}")
        h["relevant"] = bool(ELEV_RE.search(title + " " + desc)) and bool(CHILE_RE.search(title + " " + desc))
        out.append(h)
    log(f"[zenodo] unicos={len(out)} relevantes={sum(h['relevant'] for h in out)}")
    return {"hits": out, "query_counts": qc}


# ------------------------------------------------------------------ PANGAEA
def sweep_pangaea():
    out = []
    seen = set()
    qs = PANGAEA_QS or ["Chile AND (lidar OR DEM OR DSM OR DTM OR photogrammetry OR \"point cloud\" OR \"digital elevation\")",
          "(Santiago OR Valparaiso OR Coquimbo OR \"Puerto Varas\") AND Chile AND (lidar OR DEM OR elevation OR topography)",
          "Chile AND (UAV OR drone OR orthophoto OR orthomosaic)"]
    keys = []
    for q in qs:
        r = get("https://ws.pangaea.de/es/pangaea/panmd/_search", params={"q": q, "size": 100})
        if r is None or not r.ok:
            log("[pangaea] error", q)
            continue
        for x in r.json()["hits"]["hits"]:
            if x["_id"] in seen:
                continue
            seen.add(x["_id"])
            s = x["_source"]
            if not keys:
                keys = sorted(s.keys())
            title = s.get("citation_title") or s.get("title") or s.get("sf-title") or ""
            pos = s.get("meanPosition") or {}
            lat, lon = pos.get("lat"), pos.get("lon")
            if lat is not None and lon is not None:
                pil = {n: f"unknown (punto medio PANGAEA a {dist_km(lon, lat, lo, la):.0f} km; sin huella)"
                       for n, (lo, la) in PILOTS.items()}
            else:
                pil = unknown_pilots("sin posicion")
            lic = s.get("license") or s.get("licence") or s.get("sf-license") or ""
            h = hit(source="PANGAEA", id=str(x["_id"]), title=clean(str(title))[:200],
                    provider=clean(str(s.get("citation_authors") or s.get("authors") or ""))[:80],
                    url=f"https://doi.pangaea.de/10.1594/PANGAEA.{x['_id']}",
                    license=str(lic) or "ver pagina (PANGAEA suele CC BY 3.0/4.0)",
                    date=str(s.get("datestamp") or s.get("internal-datestamp") or "")[:10], pilots=pil,
                    notes=f"meanPosition=({lon},{lat})")
            h["relevant"] = bool(ELEV_RE.search(str(title))) and lat is not None and -57 < lat < -17 and -77 < lon < -65
            out.append(h)
    log(f"[pangaea] unicos={len(out)} relevantes={sum(h['relevant'] for h in out)}")
    return {"hits": out, "source_keys": keys}


# ----------------------------------------------------------------- figshare
def sweep_figshare():
    out = []
    seen = set()
    for q in FIG_Q or ["Chile lidar", "Chile DEM", "Chile digital elevation model", "Chile photogrammetry", "Chile point cloud",
                       "Chile DSM", "Santiago Chile topography", "Valparaíso topography"]:
        try:
            r = SESSION.post("https://api.figshare.com/v2/articles/search",
                             json={"search_for": q, "page_size": 100}, timeout=60)
        except Exception as e:  # noqa: BLE001
            log("[figshare] fallo", e)
            continue
        if not r.ok:
            continue
        for a in r.json():
            if a["id"] in seen:
                continue
            seen.add(a["id"])
            title = a.get("title", "")
            h = hit(source="figshare", id=str(a["id"]), title=clean(title)[:200],
                    url=a.get("url_public_html") or a.get("url", ""), date=(a.get("published_date") or "")[:10],
                    license="no leida (ver pagina)", provider="figshare")
            h["relevant"] = bool(ELEV_RE.search(title)) and bool(CHILE_RE.search(title))
            out.append(h)
    log(f"[figshare] unicos={len(out)} relevantes={sum(h['relevant'] for h in out)}")
    return {"hits": out}


# -------------------------------------------------------------------- Dryad
def sweep_dryad():
    out = []
    seen = set()
    for q in DRYAD_Q or ["Chile", "Chile lidar", "Chile DEM", "Chilean", "Santiago Chile", "Patagonia lidar", "Andes DEM"]:
        r = get("https://datadryad.org/api/v2/search", params={"q": q, "per_page": 100})
        if r is None or not r.ok:
            continue
        for d in r.json().get("_embedded", {}).get("stash:datasets", []):
            ident = d.get("identifier")
            if ident in seen:
                continue
            seen.add(ident)
            title = d.get("title", "")
            ab = clean(d.get("abstract", ""))
            h = hit(source="Dryad", id=ident, title=clean(title)[:200], url=f"https://doi.org/{str(ident).replace('doi:', '')}",
                    license=str(d.get("license") or "CC0 (Dryad por defecto)"), date=(d.get("publicationDate") or ""),
                    notes=ab[:300])
            h["relevant"] = bool(ELEV_RE.search(title + " " + ab)) and bool(re.search(r"chile", title + " " + ab, re.I))
            out.append(h)
    log(f"[dryad] unicos={len(out)} relevantes={sum(h['relevant'] for h in out)}")
    return {"hits": out}


# --------------------------------------------------------------------- CEMS
def sweep_cems():
    base = "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations-info/"
    acts = []
    off = 0
    while True:
        r = get(base, params={"limit": 200, "offset": off})
        if r is None or not r.ok:
            break
        j = r.json()
        acts.extend(j["results"])
        if not j.get("next") or not j["results"]:
            break
        off += 200
    log(f"[cems] activaciones leidas: {len(acts)}")
    chile = [a for a in acts if any("chile" in c.lower() for c in a.get("countries", []))]
    out = []
    for a in chile:
        detail = get("https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations/",
                     params={"code": a["code"]})
        prods = []
        if detail is not None and detail.ok:
            try:
                dj = detail.json()
                res = dj.get("results", [dj]) if isinstance(dj, dict) else dj
                for rr in res:
                    for aoi in rr.get("aois", []):
                        for p in aoi.get("products", []):
                            prods.append(str(p.get("type")))
            except Exception as e:  # noqa: BLE001
                prods.append(f"error parse {e}")
        h = hit(source="Copernicus EMS Rapid Mapping", id=a["code"], title=a["name"], provider="Copernicus EMS (JRC/EC)",
                url=f"https://rapidmapping.emergency.copernicus.eu/{a['code']}", date=a.get("eventTime", "")[:10],
                license="ver notas / pagina de terminos del portal CEMS",
                notes=f"categoria={a['category']}; aois={a['n_aois']}; productos={a['n_products']}; tipos={sorted(set(prods))[:12]}",
                relevant=True)
        h["centroid"] = a.get("centroid")
        m = re.match(r"POINT \(([-0-9.]+) ([-0-9.]+)\)", a.get("centroid") or "")
        if m:
            lon, lat = float(m.group(1)), float(m.group(2))
            h["pilots"] = {n: f"unknown (centroide activacion a {dist_km(lon, lat, lo, la):.0f} km)"
                           for n, (lo, la) in PILOTS.items()}
        out.append(h)
    log(f"[cems] activaciones Chile: {len(out)} -> " + ", ".join(f"{h['id']} {h['title'][:40]}" for h in out))
    return {"hits": out, "total_activations": len(acts)}


# -------------------------------------------------------------------- pages
PROBE_PAGES = {
    "MINVU": ["https://www.minvu.gob.cl/", "https://ide.minvu.cl/", "https://www.minvu.gob.cl/datos-abiertos/"],
    "MOP-DGA": ["https://dga.mop.gob.cl/", "https://snia.mop.gob.cl/", "https://mop.gob.cl/"],
    "SHOA": ["https://www.shoa.cl/"],
    "CONAF": ["https://sit.conaf.cl/", "https://www.conaf.cl/"],
    "CIREN": ["https://www.ciren.cl/", "https://ide.ciren.cl/"],
    "IGM": ["https://www.igm.cl/"],
    "SAF": ["https://www.saf.cl/"],
    "Nunoa": ["https://www.nunoa.cl/"],
    "Vina del Mar": ["https://www.vinadelmar.cl/"],
    "Lo Barnechea": ["https://www.lobarnechea.cl/"],
    "Puerto Varas": ["https://www.puertovaras.cl/"],
    "Coquimbo": ["https://www.municoquimbo.cl/"],
    "Copernicus DEM": ["https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM",
                       "https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model"],
    "TanDEM-X": ["https://geoservice.dlr.de/web/dataguide/tdm90/"],
    "ALOS PALSAR RTC ASF": ["https://asf.alaska.edu/data-sets/derived-data-sets/alos-palsar-rtc/"],
    "Meta/WRI canopy": ["https://registry.opendata.aws/dataforgood-fb-forests/"],
    "Google Open Buildings 2.5D": ["https://sites.research.google/gr/open-buildings/temporal/"],
}
# Solo palabras de elevacion (no "licencia"): los extractos de portales municipales traian texto de tramites ajeno al tema.
KW = re.compile(r"(\blidar\b|\bMDT\b|\bMDE\b|modelo digital|curvas de nivel|ortofoto|aerofotogram|topograf)", re.I)


def sweep_pages():
    out = []
    for org, urls in PROBE_PAGES.items():
        for u in urls:
            r = get(u, timeout=30, retries=1)
            if r is None:
                out.append(hit(source="probe", id=org, title=org, url=u, notes="sin respuesta (red/TLS)"))
                continue
            r.encoding = r.apparent_encoding or "utf-8"
            body = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
            txt = clean(body)
            ex = []
            for m in KW.finditer(txt):
                ex.append(txt[max(0, m.start() - 40): m.end() + 60])
                if len(ex) >= 8:
                    break
            out.append(hit(source="probe", id=org, title=org, url=u,
                           notes=f"HTTP {r.status_code}, {len(txt)} chars; extractos: " + " || ".join(ex)[:1500]))
            log(f"[pages] {org} {u} -> {r.status_code}")
    return {"hits": out}


# ------------------------------------------------------------ ArcGIS REST (solo nombres)
def sweep_arcgis():
    """Enumera los servicios publicos de un ArcGIS Server (solo nombres/metadatos, sin pedir features)."""
    out = []
    for root in ARCGIS_ROOTS:
        names = []
        folders = []
        r = get(root, params={"f": "json"}, timeout=40)
        if r is None or not r.ok:
            out.append(hit(source="ArcGIS REST", id=root, title=root, url=root, notes="sin respuesta"))
            continue
        j = r.json()
        names += [(s_["name"], s_["type"]) for s_ in j.get("services", [])]
        folders = j.get("folders", [])
        empty = []
        for f in folders:
            rr = get(root + "/" + f, params={"f": "json"}, timeout=40)
            if rr is None or not rr.ok:
                continue
            try:
                sv = rr.json().get("services", [])
            except Exception:  # noqa: BLE001
                sv = []
            if not sv:
                empty.append(f)
            names += [(s_["name"], s_["type"]) for s_ in sv]
        elev = [n for n in names if ELEV_RE.search(n[0]) or re.search(r"(DEM|MDE|MDT|lidar|curva|contour|hillshade|sombreado|relieve)", n[0], re.I)]
        out.append(hit(source="ArcGIS REST (enumeracion de servicios)", id=root, title=root, url=root,
                       provider="MINVU (geoide.minvu.cl)", relevant=bool(elev),
                       notes=f"{len(names)} servicios en {len(folders)} carpetas; carpetas sin servicios publicos: {empty}; "
                             f"servicios cuyo nombre sugiere elevacion: {elev}",
                       access="; ".join(sorted({n[0].split('/')[0] for n in names}))))
        log(f"[arcgis] {root}: {len(names)} servicios, nombres de elevacion: {elev}")
    return {"hits": out}


# -------------------------------------------------- ZIP grande: HEAD + prueba de Range
def sweep_ziprange():
    """No descarga el ZIP: solo HEAD y una peticion Range de 1 KB para saber si el servidor admite lectura parcial."""
    out = []
    for name, u in ZIP_URLS.items():
        info = {"url": u}
        try:
            hh = SESSION.head(u, timeout=60, allow_redirects=True)
            info["head_status"] = hh.status_code
            info["content_length"] = hh.headers.get("Content-Length")
            info["accept_ranges"] = hh.headers.get("Accept-Ranges")
            info["content_type"] = hh.headers.get("Content-Type")
            n = int(info["content_length"] or 0)
            rg = SESSION.get(u, headers={"Range": f"bytes={max(n - 65536, 0)}-{max(n - 1, 0)}"}, stream=True, timeout=60)
            info["range_tail_status"] = rg.status_code
            info["range_tail_content_range"] = rg.headers.get("Content-Range")
            rg.close()  # se corta la conexion: no se baja el cuerpo
            info["range_supported"] = rg.status_code == 206
        except Exception as e:  # noqa: BLE001
            info["error"] = str(e)[:200]
        h = hit(source="ZIP range-probe", id=name, title=name, url=u, relevant=True,
                notes=json.dumps(info, ensure_ascii=False),
                access="Range OK: se puede listar el directorio central" if info.get("range_supported")
                else "SIN soporte de Range: no se puede listar sin bajar el ZIP completo (no se bajo)")
        out.append(h)
        log(f"[ziprange] {name}: {info}")
    return {"hits": out}


SWEEPS = {
    "ckan": sweep_ckan, "geoportal": sweep_geoportal, "ot": sweep_ot, "datacite": sweep_datacite,
    "zenodo": sweep_zenodo, "pangaea": sweep_pangaea, "figshare": sweep_figshare, "dryad": sweep_dryad,
    "cems": sweep_cems, "pages": sweep_pages, "arcgis": sweep_arcgis, "ziprange": sweep_ziprange,
}
NATIONAL_DEFAULT = "ckan,geoportal,ot,datacite,zenodo,pangaea,figshare,dryad,cems,pages"
RM_DEFAULT = "ckan,geoportal,ot,datacite,zenodo,pangaea,figshare,dryad,pages,arcgis,ziprange"


def use_rm_mode():
    """Cambia los parametros globales al modo Region Metropolitana."""
    global PILOTS, CHILE_BBOX, CKAN_Q, GEO_TERMS, REPO_Q, ZEN_Q, PROBE_PAGES, CHILE_RE, PANGAEA_QS, FIG_Q, DRYAD_Q, MODE
    PILOTS = RM_PILOTS
    CHILE_BBOX = RM_BBOX
    CKAN_Q = RM_CKAN_Q
    GEO_TERMS = RM_GEO_TERMS
    REPO_Q = RM_REPO_Q
    ZEN_Q = RM_ZEN_Q
    PROBE_PAGES = RM_PROBE_PAGES
    CHILE_RE = RM_CHILE_RE
    PANGAEA_QS = RM_PANGAEA_QS
    FIG_Q = RM_FIG_Q
    DRYAD_Q = RM_DRYAD_Q
    MODE = "rm"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rm", action="store_true", help="barrido enfocado en la Region Metropolitana")
    ap.add_argument("--only", default=None)
    a = ap.parse_args()
    if a.rm:
        use_rm_mode()
    default = RM_DEFAULT if a.rm else NATIONAL_DEFAULT
    only = a.only or default
    results = {}
    for k in only.split(","):
        k = k.strip()
        if k not in SWEEPS:
            continue
        log(f"== {k} ==")
        try:
            results[k] = SWEEPS[k]()
        except Exception as e:  # noqa: BLE001
            import traceback
            log(f"!! {k} fallo: {e}")
            results[k] = {"error": str(e), "trace": traceback.format_exc(), "hits": []}
    doc = {
        "date": TODAY,
        "mode": MODE,
        "pilots": {k: {"lon": v[0], "lat": v[1]} for k, v in PILOTS.items()},
        "note": "Solo lectura. pilots[...] indica como se chequeo cobertura (huella, bbox grueso, o unknown). "
                + ("Modo RM: los puntos son centros aproximados de comunas, informacion publica." if a.rm else ""),
        "results": results,
        "log": LOG,
    }
    path = OUT / (f"catalog_sweep_rm_{TODAY}.json" if a.rm else f"catalog_sweep_{TODAY}.json")
    if only != default and path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        old["results"].update(results)
        old["log"] = old.get("log", []) + LOG
        doc = old
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    log("escrito", path)


if __name__ == "__main__":
    sys.exit(main())
