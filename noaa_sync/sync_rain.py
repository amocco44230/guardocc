#!/usr/bin/env python3
"""
sync_rain.py — Récupère les prévisions de précipitations GFS (NOAA, modèle mondial,
open data gratuit) via le service NOMADS "grib filter", limité à l'Europe, pour 4
échéances (+3h/+6h/+12h/+24h). Convertit chaque échéance en une image PNG (fond
transparent hors précipitation) et la pousse dans Supabase en base64.

Source : https://nomads.ncep.noaa.gov/ (NOAA/NCEP, GFS 0.25°, domaine public, mis à
jour 4x/jour : runs 00Z/06Z/12Z/18Z).

DÉPENDANCES SYSTÈME (au-delà de pip) : ce script a besoin de la bibliothèque eccodes
pour lire le format GRIB2 -- sur le runner GitHub Actions (ubuntu-latest), installez-la
AVANT d'exécuter ce script :
    sudo apt-get update && sudo apt-get install -y libeccodes-dev
    pip install cfgrib xarray matplotlib numpy --break-system-packages

USAGE
-----
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_SERVICE_KEY="eyJ..."
    python sync_rain.py                # récupère les 4 échéances et pousse
    python sync_rain.py --dry-run      # récupère et enregistre les PNG localement, ne pousse rien

IMPORTANT : GFS met en moyenne 4 à 5 heures pour publier un run après son heure
nominale (ex: le run 00Z n'est généralement disponible qu'à partir de ~04-05h UTC).
Ce script recule automatiquement jusqu'au dernier run RÉELLEMENT disponible (voir
find_latest_available_run()) -- pas besoin de le lancer à un horaire précis, un
déclenchement toutes les quelques heures suffit (pas la peine de le faire toutes les
5 minutes comme la météo : GFS ne se met à jour que 4x/jour de toute façon).
"""
import os
import sys
import json
import argparse
import urllib.request
import urllib.error
import base64
import io
from datetime import datetime, timedelta, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
USER_AGENT = "GuardOCC-sync/1.0 (contact: votre-email@exemple.fr)"

# Même zone que EUROPE_BBOX dans sync_weather.py (filtrage SIGMET) -- cohérence du réseau.
BBOX = {"leftlon": -25, "rightlon": 45, "toplat": 75, "bottomlat": 30}
FORECAST_HOURS = [3, 6, 12, 24]
NOMADS_BASE = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"


def find_latest_available_run(now=None):
    """GFS publie ~4-5h après l'heure nominale du run. On recule par pas de 6h
    (00/06/12/18Z) jusqu'à un run vieux d'au moins 5h, sans jamais vérifier
    l'existence réelle (le téléchargement échouera proprement sinon, voir main())."""
    now = now or datetime.now(timezone.utc)
    candidate = now - timedelta(hours=5)
    run_hour = (candidate.hour // 6) * 6
    run_dt = candidate.replace(hour=run_hour, minute=0, second=0, microsecond=0)
    return run_dt.strftime("%Y%m%d"), f"{run_hour:02d}"


def build_url(run_date, run_hour, forecast_hour):
    return (
        f"{NOMADS_BASE}?file=gfs.t{run_hour}z.pgrb2.0p25.f{forecast_hour:03d}"
        f"&var_APCP=on&lev_surface=on&subregion="
        f"&leftlon={BBOX['leftlon']}&rightlon={BBOX['rightlon']}"
        f"&toplat={BBOX['toplat']}&bottomlat={BBOX['bottomlat']}"
        f"&dir=/gfs.{run_date}/{run_hour}/atmos"
    )


def download_grib(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if len(data) < 1000:
        # NOMADS renvoie une petite page HTML d'erreur (pas un vrai GRIB2) si le run
        # demandé n'est pas encore publié -- on le détecte à la taille du fichier.
        raise RuntimeError(f"réponse trop petite ({len(data)} octets) -- run probablement pas encore disponible")
    with open(dest_path, "wb") as f:
        f.write(data)


def grib_to_png_base64(grib_path):
    """Lit le GRIB2 (via cfgrib/eccodes), rend une image PNG transparente hors
    précipitation, renvoie (base64_png, bounds)."""
    import xarray as xr
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    ds = xr.open_dataset(grib_path, engine="cfgrib")
    # Le nom exact de la variable varie selon la version d'eccodes ('tp' ou 'unknown')
    # -- on prend la première variable de données du fichier plutôt que de fixer un nom.
    varname = list(ds.data_vars)[0]
    precip = ds[varname].values  # mm (kg/m² = mm d'eau)
    lats = ds.latitude.values
    lons = ds.longitude.values

    # Palette : transparent sous 0.2mm (pas de pluie significative), puis bleu -> violet
    # selon l'intensité, jusqu'à un plafond de 50mm (au-delà, saturé).
    cmap = mcolors.LinearSegmentedColormap.from_list(
        "rain", ["#3b82f600", "#3b82f6", "#2563eb", "#7c3aed", "#c026d3"]
    )
    norm = mcolors.Normalize(vmin=0, vmax=50)
    rgba = cmap(norm(np.clip(precip, 0, 50)))
    rgba[precip < 0.2, 3] = 0  # totalement transparent en dessous du seuil

    fig = plt.figure(figsize=(precip.shape[1] / 100, precip.shape[0] / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    # origin="upper" car GFS liste les latitudes du nord vers le sud par défaut.
    ax.imshow(rgba, origin="upper" if lats[0] > lats[-1] else "lower", extent=[lons.min(), lons.max(), lats.min(), lats.max()])
    buf = io.BytesIO()
    plt.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")

    bounds = [[float(lats.min()), float(lons.min())], [float(lats.max()), float(lons.max())]]
    return b64, bounds


def supabase_upsert(table, rows, on_conflict):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  ! SUPABASE_URL / SUPABASE_SERVICE_KEY non définis — envoi ignoré.", file=sys.stderr)
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"  -> {table}: {len(rows)} ligne(s) envoyée(s) (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        print(f"  ! erreur Supabase (HTTP {e.code}) : {e.read().decode('utf-8')[:500]}", file=sys.stderr)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  ! délai réseau dépassé ({e}) — retenté au prochain run.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Synchronise la couche pluie GFS (Europe) vers Supabase")
    parser.add_argument("--dry-run", action="store_true", help="enregistre les PNG localement, n'envoie rien à Supabase")
    args = parser.parse_args()

    run_date, run_hour = find_latest_available_run()
    print(f"Run GFS ciblé : {run_date} {run_hour}Z")

    rows = []
    for fh in FORECAST_HOURS:
        print(f"\n[+{fh}h] récupération…")
        url = build_url(run_date, run_hour, fh)
        grib_path = f"/tmp/gfs_rain_f{fh:03d}.grib2"
        try:
            download_grib(url, grib_path)
            b64, bounds = grib_to_png_base64(grib_path)
            print(f"  OK -- image {len(b64)} caractères base64")
            if args.dry_run:
                png_path = f"/tmp/gfs_rain_f{fh:03d}.png"
                with open(png_path, "wb") as f:
                    f.write(base64.b64decode(b64))
                print(f"  (dry-run) image enregistrée : {png_path}")
            else:
                rows.append({
                    "forecast_hour": fh, "run_date": run_date, "run_hour": run_hour,
                    "image_base64": b64, "bounds": bounds,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            print(f"  ! échec pour +{fh}h : {e}", file=sys.stderr)

    if rows and not args.dry_run:
        supabase_upsert("rain_forecast_layers", rows, on_conflict="forecast_hour")

    print("\nTerminé.")


if __name__ == "__main__":
    main()
