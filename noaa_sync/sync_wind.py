#!/usr/bin/env python3
"""
sync_wind.py — Récupère les prévisions de vent GFS (NOAA, composantes U/V à 10m du
sol) via NOMADS, limité à l'Europe, pour 4 échéances (+3h/+6h/+12h/+24h). Calcule la
vitesse du vent (magnitude de U/V) et la convertit en image PNG colorée transparente,
poussée dans Supabase en base64. REMPLACE l'ancienne couche vent OpenWeatherMap
(proxy wm-tile-proxy) -- même principe de rendu que sync_rain.py.

Source : https://nomads.ncep.noaa.gov/ (NOAA/NCEP, GFS 0.25°, domaine public).

DÉPENDANCES SYSTÈME : identiques à sync_rain.py (voir son en-tête) --
    sudo apt-get install -y libeccodes-dev
    pip install cfgrib xarray matplotlib numpy --break-system-packages

USAGE
-----
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_SERVICE_KEY="eyJ..."
    python sync_wind.py
    python sync_wind.py --dry-run
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

BBOX = {"leftlon": -25, "rightlon": 45, "toplat": 75, "bottomlat": 30}
FORECAST_HOURS = [3, 6, 12, 24]
NOMADS_BASE = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"


def find_latest_available_run(now=None):
    """GFS commence à publier ~3h30 après l'heure nominale du run, disponibilité
    complète ~4h40 après. On vise le dernier run théoriquement complet (recul de 4h40),
    avec repli automatique sur le run précédent si le téléchargement échoue encore
    (voir download_grib_with_retry) -- au cas où le calendrier réel aurait un peu de
    retard ce jour-là."""
    now = now or datetime.now(timezone.utc)
    candidate = now - timedelta(hours=4, minutes=40)
    run_hour = (candidate.hour // 6) * 6
    run_dt = candidate.replace(hour=run_hour, minute=0, second=0, microsecond=0)
    return run_dt.strftime("%Y%m%d"), f"{run_hour:02d}"


def previous_run(run_date, run_hour):
    """Recule de 6h -- pour retomber sur le run précédent si celui ciblé n'est pas
    encore publié (retard ponctuel côté NOAA)."""
    dt = datetime.strptime(f"{run_date}{run_hour}", "%Y%m%d%H").replace(tzinfo=timezone.utc)
    dt -= timedelta(hours=6)
    return dt.strftime("%Y%m%d"), f"{dt.hour:02d}"


def build_url(run_date, run_hour, forecast_hour):
    return (
        f"{NOMADS_BASE}?file=gfs.t{run_hour}z.pgrb2.0p25.f{forecast_hour:03d}"
        f"&var_UGRD=on&var_VGRD=on&lev_10_m_above_ground=on&subregion="
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
    """Lit le GRIB2 (via cfgrib/eccodes), calcule la vitesse du vent (magnitude des
    composantes U/V, converties en nœuds), rend une image PNG transparente sous un
    seuil bas avec des flèches de direction, renvoie (base64_png, bounds)."""
    import xarray as xr
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    from PIL import Image

    # U et V à 10m sont dans le même fichier -- cfgrib les sépare en 2 datasets distincts
    # via filter_by_keys plutôt que de les mélanger dans un seul open_dataset (plus fiable
    # sur ce type de fichier GFS que de laisser cfgrib deviner tout seul).
    ds_u = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"filter_by_keys": {"shortName": "10u"}})
    ds_v = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"filter_by_keys": {"shortName": "10v"}})
    u = ds_u[list(ds_u.data_vars)[0]].values
    v = ds_v[list(ds_v.data_vars)[0]].values
    lats = ds_u.latitude.values
    lons = ds_u.longitude.values

    speed_ms = np.sqrt(u ** 2 + v ** 2)
    speed_kt = speed_ms * 1.94384  # m/s -> nœuds, convention aéronautique

    # Palette par PALIERS (bandes nettes, pas un dégradé continu) -- façon carte
    # meteociel de référence : blanc/transparent sous 10kt, puis bandes bleu -> vert ->
    # jaune -> orange -> rouge tous les ~10kt. Modifiable ici si besoin (LEVELS_KT).
    LEVELS_KT = [0, 10, 20, 30, 40, 50, 60, 200]  # 8 bornes -> 7 intervalles, 7 couleurs
    COLORS = ["#ffffff00", "#a7d8f0", "#5fb8e0", "#2fbf71", "#e8d33c", "#e8a13c", "#e0483e"]

    fig = plt.figure(figsize=(speed_kt.shape[1] / 100, speed_kt.shape[0] / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.set_xlim(lons.min(), lons.max())
    ax.set_ylim(lats.min(), lats.max()) if lats[0] < lats[-1] else ax.set_ylim(lats.max(), lats.min())

    lon2d, lat2d = np.meshgrid(lons, lats)
    ax.contourf(lon2d, lat2d, speed_kt, levels=LEVELS_KT, colors=COLORS)

    # Flèches de direction -- beaucoup moins denses qu'avant (une pointe tous les ~16
    # points de grille) et nettement plus grandes/contrastées (liseré blanc autour du
    # trait noir) pour rester lisibles même sur un fond de carte chargé.
    step = 16
    q = ax.quiver(
        lon2d[::step, ::step], lat2d[::step, ::step],
        u[::step, ::step], v[::step, ::step],
        color="#1a1a1a", scale=280, width=0.0055, headwidth=3.2, headlength=4, alpha=0.95,
    )
    q.set_path_effects([pe.Stroke(linewidth=2.2, foreground="white"), pe.Normal()])

    buf = io.BytesIO()
    plt.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)

    # Fondu sur les bords -- sans ça, le rectangle Europe se voit nettement en bord de
    # carte (arête franche). On estompe l'opacité sur les ~8% extérieurs de l'image
    # plutôt que de la couper net.
    img = Image.open(buf).convert("RGBA")
    w, h = img.size
    alpha = np.array(img.getchannel("A"), dtype=np.float32)
    margin_x, margin_y = int(w * 0.08), int(h * 0.08)
    fade = np.ones((h, w), dtype=np.float32)
    for i in range(margin_x):
        f = i / margin_x
        fade[:, i] = np.minimum(fade[:, i], f)
        fade[:, w - 1 - i] = np.minimum(fade[:, w - 1 - i], f)
    for j in range(margin_y):
        f = j / margin_y
        fade[j, :] = np.minimum(fade[j, :], f)
        fade[h - 1 - j, :] = np.minimum(fade[h - 1 - j, :], f)
    alpha = (alpha * fade).astype(np.uint8)
    img.putalpha(Image.fromarray(alpha))
    out = io.BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    b64 = base64.b64encode(out.read()).decode("ascii")

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
    parser = argparse.ArgumentParser(description="Synchronise la couche vent GFS (Europe) vers Supabase")
    parser.add_argument("--dry-run", action="store_true", help="enregistre les PNG localement, n'envoie rien à Supabase")
    args = parser.parse_args()

    run_date, run_hour = find_latest_available_run()
    print(f"Run GFS ciblé : {run_date} {run_hour}Z")

    rows = []
    for fh in FORECAST_HOURS:
        print(f"\n[+{fh}h] récupération…")
        this_date, this_hour = run_date, run_hour
        grib_path = f"/tmp/gfs_wind_f{fh:03d}.grib2"
        ok = False
        for attempt in range(2):  # run ciblé, puis repli sur le run précédent si besoin
            url = build_url(this_date, this_hour, fh)
            try:
                download_grib(url, grib_path)
                ok = True
                break
            except Exception as e:
                print(f"  ! run {this_date} {this_hour}Z indisponible ({e}) — repli sur le run précédent.", file=sys.stderr)
                this_date, this_hour = previous_run(this_date, this_hour)
        if not ok:
            print(f"  ! échec pour +{fh}h après 2 tentatives, abandon pour cette échéance.", file=sys.stderr)
            continue
        try:
            b64, bounds = grib_to_png_base64(grib_path)
            print(f"  OK -- image {len(b64)} caractères base64")
            if args.dry_run:
                png_path = f"/tmp/gfs_wind_f{fh:03d}.png"
                with open(png_path, "wb") as f:
                    f.write(base64.b64decode(b64))
                print(f"  (dry-run) image enregistrée : {png_path}")
            else:
                rows.append({
                    "forecast_hour": fh, "run_date": this_date, "run_hour": this_hour,
                    "image_base64": b64, "bounds": bounds,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            print(f"  ! échec pour +{fh}h : {e}", file=sys.stderr)

    if rows and not args.dry_run:
        supabase_upsert("wind_forecast_layers", rows, on_conflict="forecast_hour")

    print("\nTerminé.")


if __name__ == "__main__":
    main()
