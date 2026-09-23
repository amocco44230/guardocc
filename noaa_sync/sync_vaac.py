#!/usr/bin/env python3
"""
sync_vaac.py — Récupère la dernière alerte VAA (Volcanic Ash Advisory) du VAAC de
Toulouse pour l'Etna, et la pousse dans Supabase.

Source : https://vaac.meteo.fr/ (Météo-France, VAAC Toulouse — autorité officielle
ICAO pour les cendres volcaniques sur l'Europe/Afrique/une partie de l'Atlantique).
PAS d'API publique propre : on lit directement les pages HTML du site (structure
stable, format texte fixe des avis VAA), donc plus fragile qu'un vrai flux JSON —
si Météo-France change un jour la mise en page du site, ce script devra être ajusté.

Volontairement limité à l'ETNA pour l'instant (code volcan OACI 211060) — voir
VOLCANOES ci-dessous pour étendre à d'autres volcans plus tard (Stromboli, La Palma,
Piton de la Fournaise...), chacun avec son propre code et sa page /volcanoes/<slug>/.

USAGE
-----
    pip install -r requirements.txt
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_SERVICE_KEY="eyJ..."
    python sync_vaac.py                # récupère et pousse
    python sync_vaac.py --dry-run      # récupère et affiche, ne pousse rien

Nécessite la table vaac_advisories (voir 64_vaac_advisories.sql).
"""
import os
import sys
import re
import json
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

RUN_TIME = datetime.now(timezone.utc).isoformat()
USER_AGENT = "GuardOCC-sync/1.0 (contact: votre-email@exemple.fr)"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Un seul volcan actif pour l'instant. Pour en ajouter un : trouver son code OACI et
# son slug de page sur https://vaac.meteo.fr/volcanoes/ (liste complète en bas de
# chaque page du site), et ajouter une entrée ici — le reste du script s'adapte tout seul.
VOLCANOES = {
    "ETNA": {"slug": "etna", "lat": 37.734, "lng": 14.999},  # PSN N3744 E01459 (VAA officiel)
}

VAAC_BASE = "https://vaac.meteo.fr"


def http_get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def find_latest_advisory_url(volcano_slug):
    """Page /volcanoes/<slug>/ : liste des avis, le plus récent est le premier lien
    de la forme /advisory/AAAA/XXXXXX_YYYYMMDDHHMMSS/XXXXXX_YYYYMMDDHHMMSS/.

    Motif volontairement souple (pas d'ancrage sur href="..." précis) : cherche le
    chemin /advisory/... n'importe où dans le HTML, qu'il soit en URL relative ou
    absolue, entre guillemets simples ou doubles -- plus robuste si Météo-France
    modifie légèrement la structure de ses balises un jour."""
    html = http_get_text(f"{VAAC_BASE}/volcanoes/{volcano_slug}/")
    matches = re.findall(r"/advisory/\d{4}/\d+_\d+/\d+_\d+/", html)
    if not matches:
        raise RuntimeError(
            f"aucun avis trouvé sur la page /volcanoes/{volcano_slug}/ "
            f"(page récupérée : {len(html)} caractères, début : {html[:200]!r})"
        )
    return VAAC_BASE + matches[0]


# Champs du bloc texte fixe "VA ADVISORY ... NXT ADVISORY: ...=". On capture chaque
# champ individuellement par étiquette plutôt que de tenter de découper des colonnes
# fixes -- plus robuste si Météo-France change légèrement l'espacement un jour.
FIELD_PATTERNS = {
    "dtg": r"DTG:\s*([^\n]+)",
    "vaac": r"VAAC:\s*([^\n]+)",
    "volcano": r"VOLCANO:\s*([^\n]+)",
    "psn": r"PSN:\s*([^\n]+)",
    "area": r"AREA:\s*([^\n]+)",
    "source_elev": r"SOURCE ELEV:\s*([^\n]+)",
    "advisory_nr": r"ADVISORY NR:\s*([^\n]+)",
    "info_source": r"INFO SOURCE:\s*([^\n]+)",
    "colour_code": r"AVIATION COLOUR CODE:\s*([^\n]+)",
    "eruption_details": r"ERUPTION DETAILS:\s*([^\n]+)",
    "obs_va_dtg": r"OBS VA DTG:\s*([^\n]+)",
    # Champs pouvant s'étendre jusqu'à la ligne suivante avant le prochain libellé --
    # [\s\S] (et non [^\n]) pour pouvoir traverser un saut de ligne le cas échéant.
    "obs_va_cld": r"OBS VA CLD:\s*([\s\S]+?)(?=FCST VA CLD \+6|$)",
    "fcst_6h": r"FCST VA CLD \+6 HR:\s*([\s\S]+?)(?=FCST VA CLD \+12|$)",
    "fcst_12h": r"FCST VA CLD \+12 HR:\s*([\s\S]+?)(?=FCST VA CLD \+18|$)",
    "fcst_18h": r"FCST VA CLD \+18 HR:\s*([\s\S]+?)(?=RMK:|$)",
    "remark": r"RMK:\s*([\s\S]+?)(?=NXT ADVISORY:|$)",
    "next_advisory": r"NXT ADVISORY:\s*([^\n=]+)",
}


def parse_psn(psn_str):
    """'N3744 E01459' -> (37.7333, 14.9833). Repli sur les coordonnées connues du
    volcan (VOLCANOES ci-dessus) si le format ne correspond pas à ce qui est attendu."""
    m = re.match(r"([NS])(\d{2})(\d{2})\s+([EW])(\d{3})(\d{2})", psn_str.strip())
    if not m:
        return None, None
    ns, lat_d, lat_m, ew, lon_d, lon_m = m.groups()
    lat = int(lat_d) + int(lat_m) / 60
    lon = int(lon_d) + int(lon_m) / 60
    if ns == "S":
        lat = -lat
    if ew == "W":
        lon = -lon
    return round(lat, 4), round(lon, 4)


def fetch_advisory(volcano_name, cfg):
    url = find_latest_advisory_url(cfg["slug"])
    html = http_get_text(url)
    # Le bloc texte brut est délimité par "VA ADVISORY" ... "=" (fin standard ICAO).
    block_match = re.search(r"VA ADVISORY.*?=", html, re.S)
    if not block_match:
        raise RuntimeError(f"bloc VA ADVISORY introuvable sur {url}")
    block = block_match.group(0)

    fields = {}
    for key, pattern in FIELD_PATTERNS.items():
        m = re.search(pattern, block, re.S)
        fields[key] = m.group(1).strip() if m else None

    lat, lng = parse_psn(fields["psn"]) if fields.get("psn") else (None, None)
    if lat is None:
        lat, lng = cfg["lat"], cfg["lng"]  # repli sur la position connue

    colour = (fields.get("colour_code") or "").strip().upper()

    return {
        "volcano": volcano_name,
        "advisory_url": url,
        "dtg": fields.get("dtg"),
        "advisory_nr": fields.get("advisory_nr"),
        "aviation_colour_code": colour or None,
        "eruption_details": fields.get("eruption_details"),
        "obs_va_cld": fields.get("obs_va_cld"),
        "fcst_6h": fields.get("fcst_6h"),
        "fcst_12h": fields.get("fcst_12h"),
        "fcst_18h": fields.get("fcst_18h"),
        "remark": fields.get("remark"),
        "next_advisory": fields.get("next_advisory"),
        "raw_text": block.strip(),
        "lat": lat,
        "lng": lng,
        "source": "vaac_toulouse",
        "fetched_at": RUN_TIME,
    }


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


def main():
    parser = argparse.ArgumentParser(description="Synchronise les alertes VAAC Toulouse (volcans) vers Supabase")
    parser.add_argument("--dry-run", action="store_true", help="récupère et affiche, n'envoie rien à Supabase")
    args = parser.parse_args()

    rows = []
    for name, cfg in VOLCANOES.items():
        print(f"[{name}] récupération…")
        try:
            row = fetch_advisory(name, cfg)
            print(f"  DTG {row['dtg']} — code couleur {row['aviation_colour_code']}")
            rows.append(row)
        except Exception as e:
            print(f"  ! échec pour {name} : {e}", file=sys.stderr)

    if args.dry_run:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        supabase_upsert("vaac_advisories", rows, on_conflict="volcano")

    print("\nTerminé.")


if __name__ == "__main__":
    main()
