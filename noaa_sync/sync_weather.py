#!/usr/bin/env python3
"""
sync_weather.py — Récupère METAR / TAF / SIGMET internationaux depuis l'API
officielle NOAA Aviation Weather Center et pousse le résultat dans Supabase.

Source des données : https://aviationweather.gov/data/api/  (API publique, gratuite,
sans clé, gérée par le NOAA/NWS Aviation Weather Center). CORS désactivé côté NOAA :
c'est pourquoi ça doit tourner côté serveur (ce script), pas dans le navigateur.

USAGE
-----
    pip install -r requirements.txt
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_SERVICE_KEY="eyJ..."          # clé service_role, PAS la clé anon
    python sync_weather.py                # récupère + pousse tout
    python sync_weather.py --dry-run      # récupère et affiche, ne pousse rien
    python sync_weather.py --no-sigmet    # ignore les SIGMET (plus rares)

IMPORTANT — à vérifier avant le premier vrai run :
    1. Les noms de colonnes ci-dessous (voir map_metar/map_taf/map_sigmet) sont
       calqués sur le schéma qu'on avait posé (database.doc). Si vos tables
       Supabase ont des noms différents, ajustez les dictionnaires "row = {...}".
    2. Lancez d'abord avec --dry-run et vérifiez la sortie JSON avant de pousser
       pour de vrai — l'API NOAA peut faire évoluer ses noms de champs.
    3. Pensez à créer une contrainte UNIQUE sur (icao_code) pour metar_data et
       (icao_code, issue_time) pour taf_data, sinon l'upsert échouera ou dupliquera.
"""
import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Horodatage explicite de CE run, envoyé sur chaque ligne. Indispensable : la colonne
# "fetched_at default now()" ne s'applique qu'à une INSERTION, jamais à une MISE À JOUR
# (upsert) — sans ce champ explicite, la date reste figée dès la 2e synchro d'un terrain.
RUN_TIME = datetime.now(timezone.utc).isoformat()

NOAA_BASE = "https://aviationweather.gov/api/data"
# NOAA demande un User-Agent explicite pour éviter d'être filtré par erreur
# comme du trafic automatisé abusif — mettez une vraie adresse de contact.
USER_AGENT = "GuardOCC-sync/1.0 (contact: votre-email@exemple.fr)"

HERE = os.path.dirname(os.path.abspath(__file__))
ICAOS_FILE = os.path.join(HERE, "icaos.json")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def load_icaos():
    with open(ICAOS_FILE, encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# RÉCUPÉRATION NOAA
# ============================================================
def fetch_metars(icaos):
    """METAR décodés (l'API calcule déjà catégorie de vol, plafond, etc.)."""
    out = []
    for chunk in chunked(icaos, 100):
        url = f"{NOAA_BASE}/metar?ids={','.join(chunk)}&format=json"
        try:
            data = http_get_json(url)
            out.extend(data)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            print(f"  ! erreur METAR sur ce lot ({len(chunk)} terrains) : {e}", file=sys.stderr)
        time.sleep(0.5)  # respect du service — pas de rafale de requêtes
    return out


def fetch_tafs(icaos):
    out = []
    for chunk in chunked(icaos, 100):
        url = f"{NOAA_BASE}/taf?ids={','.join(chunk)}&format=json"
        try:
            data = http_get_json(url)
            out.extend(data)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            print(f"  ! erreur TAF sur ce lot ({len(chunk)} terrains) : {e}", file=sys.stderr)
        time.sleep(0.5)
    return out


def fetch_sigmets():
    """SIGMET internationaux (isigmet couvre l'Europe et le reste du monde hors USA ;
    airsigmet est spécifique aux USA — inclus aussi au cas où votre réseau s'étend là-bas).
    Sur le test réel : 147 SIGMET actifs dans le monde à l'instant T, dont l'écrasante
    majorité hors Europe (Afrique du Sud, Argentine...) — sans intérêt pour votre réseau.
    On filtre donc à une zone large autour de l'Europe avant de pousser vers Supabase."""
    out = []
    for endpoint in ("isigmet", "airsigmet"):
        url = f"{NOAA_BASE}/{endpoint}?format=json"
        try:
            data = http_get_json(url)
            for row in data:
                row["_source_endpoint"] = endpoint
            out.extend(data)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            print(f"  ! erreur {endpoint} : {e}", file=sys.stderr)
        time.sleep(0.5)
    return out


# Zone large autour de l'Europe (lat, lon) — à ajuster si votre réseau s'étend ailleurs.
EUROPE_BBOX = {"lat_min": 30, "lat_max": 75, "lon_min": -25, "lon_max": 45}


def sigmet_in_area(s):
    geom = s.get("coords") or s.get("area")
    if not geom:
        return True  # pas de géométrie exploitable -> on la garde par prudence plutôt que de la perdre
    # Certains SIGMET ont geom="AREAS" : une LISTE DE POLYGONES (liste de listes de points),
    # pas une liste de points directement. On aplatit un niveau si besoin pour couvrir les deux cas
    # (c'est ce qui laissait passer Brazzaville par erreur : ses points n'étaient jamais lus).
    points = []
    for item in geom:
        if isinstance(item, dict):
            points.append(item)
        elif isinstance(item, list):
            points.extend(p for p in item if isinstance(p, dict))
    lats = [pt.get("lat") for pt in points if pt.get("lat") is not None]
    lons = [pt.get("lon") for pt in points if pt.get("lon") is not None]
    if not lats or not lons:
        return True
    lat_c, lon_c = sum(lats) / len(lats), sum(lons) / len(lons)
    b = EUROPE_BBOX
    return b["lat_min"] <= lat_c <= b["lat_max"] and b["lon_min"] <= lon_c <= b["lon_max"]


# ============================================================
# DÉCOUPAGE HORAIRE DU TAF — port Python de expandHourly() (décodeur JS)
# Calcul fait UNE SEULE FOIS ici, à la réception, et stocké tel quel en JSONB.
# La carte / le curseur temporel n'ont plus qu'à LIRE ce tableau, jamais à le recalculer.
# NOAA nous simplifie la tâche : les tranches (fcsts) sont déjà découpées avec
# fcstChange (FM/BECMG/TEMPO) et probability (PROB30/40) — pas besoin de reparser le texte.
# ============================================================
CAT_RANK = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}


def parse_visib_sm(v):
    """Convertit le champ 'visib' NOAA (nombre, '6+', '1/2', '1 1/2'...) en milles terrestres (float)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip("+")
    try:
        if " " in s:  # ex: "1 1/2"
            whole, frac = s.split(" ", 1)
            n, d = frac.split("/")
            return float(whole) + float(n) / float(d)
        if "/" in s:  # ex: "1/2"
            n, d = s.split("/")
            return float(n) / float(d)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def ceiling_from_clouds(clouds):
    if not clouds:
        return None
    ceilings = [c.get("base") for c in clouds if c.get("cover") in ("BKN", "OVC") and c.get("base") is not None]
    return min(ceilings) if ceilings else None


def flight_category(vis_sm, ceiling_ft):
    vis_sm = 10.0 if vis_sm is None else vis_sm
    ceiling_ft = 5000 if ceiling_ft is None else ceiling_ft
    if ceiling_ft < 500 or vis_sm < 1:
        return "LIFR"
    if ceiling_ft < 1000 or vis_sm < 3:
        return "IFR"
    if ceiling_ft < 3000 or vis_sm < 5:
        return "MVFR"
    return "VFR"


def _merge_period(base_state, extra):
    merged = dict(base_state)
    for k in ("wdir", "wspd", "wgst", "visib", "clouds", "wxString", "vertVis"):
        v = extra.get(k)
        if v not in (None, [], ""):
            merged[k] = v
    return merged


def expand_hourly(taf_row):
    """Reçoit le résultat de map_taf() (avec sa clé 'periods' = fcsts NOAA) et renvoie
    une liste d'états, un par heure, de valid_from à valid_to."""
    periods = taf_row.get("periods") or []
    valid_from, valid_to = taf_row.get("valid_from"), taf_row.get("valid_to")
    if not periods or not valid_from or not valid_to:
        return []

    base = periods[0]
    evolutions = sorted(
        (p for p in periods if p.get("fcstChange") in ("FM", "BECMG") and p.get("timeFrom")),
        key=lambda p: p["timeFrom"],
    )
    overlays = [p for p in periods if p is not base and (p.get("fcstChange") == "TEMPO" or p.get("probability"))]

    hours = list(range(valid_from, valid_to + 1, 3600))
    result = []
    for h in hours:
        current = dict(base)
        for ev in evolutions:
            if ev["timeFrom"] <= h:
                current = _merge_period(current, ev)

        worst, worst_cat, worst_tag = None, None, None
        for ov in overlays:
            tf, tt = ov.get("timeFrom"), ov.get("timeTo")
            if tf is None or tt is None or not (tf <= h < tt):
                continue
            merged = _merge_period(current, ov)
            cat = flight_category(parse_visib_sm(merged.get("visib")), ceiling_from_clouds(merged.get("clouds")))
            if worst is None or CAT_RANK[cat] > CAT_RANK[worst_cat]:
                worst, worst_cat = merged, cat
                worst_tag = f"PROB{ov['probability']}" if ov.get("probability") else "TEMPO"

        eff = worst if worst else current
        ceil_ft = ceiling_from_clouds(eff.get("clouds"))
        vis_sm = parse_visib_sm(eff.get("visib"))
        result.append({
            "time": h,
            "tag": worst_tag,  # None = "prévu" (pas de TEMPO/PROB actif à cette heure)
            "category": flight_category(vis_sm, ceil_ft),
            "wind_direction": eff.get("wdir"),
            "wind_speed_kt": eff.get("wspd"),
            "wind_gust_kt": eff.get("wgst"),
            "visibility_sm": vis_sm,
            "ceiling_ft": ceil_ft,
            "weather_phenomena": eff.get("wxString"),
        })
    return result


# ============================================================
# MISE EN FORME POUR SUPABASE (à ajuster à votre schéma réel si besoin)
# ============================================================
def map_metar(m):
    return {
        "icao_code": m.get("icaoId"),
        "raw_metar": m.get("rawOb"),
        "observation_time": m.get("obsTime") or m.get("reportTime"),
        "wind_direction": m.get("wdir") if isinstance(m.get("wdir"), int) else None,
        "wind_speed_kt": m.get("wspd"),
        "wind_gust_kt": m.get("wgst"),
        "visibility_sm": m.get("visib"),
        "temperature_c": m.get("temp"),
        "dewpoint_c": m.get("dewp"),
        "qnh_hpa": round(m["altim"]) if m.get("altim") else None,
        # Peut être null si le METAR est incomplet côté source (ex: nuages "///" non
        # exploitables, comme vu sur EDDB pendant le test) — cas normal, pas un bug.
        "flight_category": m.get("fltCat"),
        "cloud_layers": m.get("clouds"),
        "weather_phenomena": m.get("wxString"),
        "lat": m.get("lat"),
        "lng": m.get("lon"),
        "source": "noaa_awc",
        "fetched_at": RUN_TIME,
    }


def map_taf(t):
    row = {
        "icao_code": t.get("icaoId"),
        "raw_taf": (t.get("rawTAF") or "").strip(),
        "issue_time": t.get("issueTime"),
        "valid_from": t.get("validTimeFrom"),
        "valid_to": t.get("validTimeTo"),
        # Tableau brut des tranches NOAA (fcstChange = FM/BECMG/TEMPO, probability = PROB30/40),
        # gardé pour référence/débogage.
        "periods": t.get("fcsts"),
        "source": "noaa_awc",
        "fetched_at": RUN_TIME,
    }
    # Calculé UNE FOIS ici, à la réception -> stocké tel quel. La carte et le curseur
    # temporel n'ont plus qu'à lire ce tableau (voir expand_hourly() ci-dessus).
    row["hourly"] = expand_hourly(row)
    return row


def map_sigmet(s):
    # Le nom exact du champ texte brut variait selon les sources testées ; on essaie
    # plusieurs clés connues ET on garde tout le JSON reçu (raw_json) pour ne jamais
    # perdre d'information même si NOAA renomme un champ un jour.
    raw_text = s.get("rawAirSigmet") or s.get("rawSigmet") or s.get("rawText") or s.get("raw")
    fir = s.get("firId") or s.get("icaoId")
    valid_from = s.get("validTimeFrom")
    hazard = s.get("hazard")
    return {
        # Clé d'unicité pour l'upsert : ne dépend jamais du texte brut (qui peut être vide),
        # toujours calculable à partir de champs structurés.
        "sig_key": f"{fir}_{valid_from}_{hazard}_{s.get('_source_endpoint')}",
        "raw_sigmet": raw_text,
        "raw_json": s,
        "hazard": hazard,
        "fir": fir,
        "valid_from": valid_from,
        "valid_to": s.get("validTimeTo"),
        "geometry": s.get("coords") or s.get("area"),
        "source_endpoint": s.get("_source_endpoint"),
        "source": "noaa_awc",
        "fetched_at": RUN_TIME,
    }


# ============================================================
# ENVOI SUPABASE (REST / PostgREST) — upsert via en-tête Prefer
# ============================================================
def supabase_upsert(table, rows, on_conflict):
    if not rows:
        print(f"  (rien à envoyer pour {table})")
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  ! SUPABASE_URL / SUPABASE_SERVICE_KEY non définis — envoi ignoré.", file=sys.stderr)
        return

    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"  -> {table}: {len(rows)} ligne(s) envoyée(s) (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        print(f"  ! erreur Supabase sur {table} (HTTP {e.code}) : {e.read().decode('utf-8')[:500]}", file=sys.stderr)


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Synchronise METAR/TAF/SIGMET NOAA vers Supabase")
    parser.add_argument("--dry-run", action="store_true", help="récupère et affiche, n'envoie rien à Supabase")
    parser.add_argument("--no-sigmet", action="store_true", help="ignore la récupération des SIGMET")
    parser.add_argument("--limit", type=int, default=None, help="ne traiter que les N premiers terrains (tests)")
    args = parser.parse_args()

    icaos = load_icaos()
    if args.limit:
        icaos = icaos[: args.limit]
    print(f"Terrains suivis : {len(icaos)}")

    print("\n[1/3] METAR…")
    metars = fetch_metars(icaos)
    metar_rows = [map_metar(m) for m in metars]
    print(f"  {len(metar_rows)} METAR récupérés")
    if args.dry_run:
        print(json.dumps(metar_rows[:3], indent=2, ensure_ascii=False))
    else:
        supabase_upsert("metar_data", metar_rows, on_conflict="icao_code")

    print("\n[2/3] TAF…")
    tafs = fetch_tafs(icaos)
    taf_rows = [map_taf(t) for t in tafs]
    print(f"  {len(taf_rows)} TAF récupérés")
    if args.dry_run:
        print(json.dumps(taf_rows[:2], indent=2, ensure_ascii=False))
    else:
        supabase_upsert("taf_data", taf_rows, on_conflict="icao_code")

    if not args.no_sigmet:
        print("\n[3/3] SIGMET…")
        sigmets = fetch_sigmets()
        sigmets_eur = [s for s in sigmets if sigmet_in_area(s)]
        print(f"  {len(sigmets)} SIGMET actifs dans le monde, {len(sigmets_eur)} dans la zone Europe/réseau")
        sigmet_rows = [map_sigmet(s) for s in sigmets_eur]
        if args.dry_run:
            print(json.dumps(sigmet_rows[:3], indent=2, ensure_ascii=False))
        else:
            supabase_upsert("sigmet_data", sigmet_rows, on_conflict="sig_key")

    print("\nTerminé.")


if __name__ == "__main__":
    main()
