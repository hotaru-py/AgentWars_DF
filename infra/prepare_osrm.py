"""One-time OSRM data preparation (spec §3.2). Standard library + Docker only.

    python infra/prepare_osrm.py              # Chennai area only (fast, ~1-2 GB RAM)
    python infra/prepare_osrm.py --full       # whole Southern Zone (532 MB download, needs ~8 GB RAM)

Steps: download the Geofabrik India Southern Zone extract -> (optional) clip to the Chennai
bounding box with osmium -> osrm-extract (car profile) -> osrm-partition -> osrm-customize.
Output: infra/osrm-data/region.osrm*, served by the `osrm` service in docker-compose.yml.
Check that the Southern Zone covers Tamil Nadu before relying on it (Geofabrik's page doesn't
list states).
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "osrm-data")
URL = "https://download.geofabrik.de/asia/india/southern-zone-latest.osm.pbf"
CHENNAI_BBOX = "79.95,12.75,80.35,13.25"     # lng_min,lat_min,lng_max,lat_max
OSRM_IMAGE = "osrm/osrm-backend:v5.25.0"
OSMIUM_IMAGE = "iboates/osmium:latest"


def download(url: str, dest: str, tries: int = 8) -> None:
    """Download to dest + '.part', resuming after dropped connections; rename only when complete,
    so an interrupted run never leaves a truncated file that a later run would trust."""
    part = dest + ".part"
    total = None
    for attempt in range(1, tries + 1):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        req = urllib.request.Request(url, headers={"User-Agent": "dispatch-v2-setup",
                                                   **({"Range": f"bytes={have}-"} if have else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:      # server ignored Range: start over
                    have = 0
                length = int(r.headers.get("Content-Length") or 0)
                total = have + length if length else total
                print(f"Downloading {url} (attempt {attempt}, {have / 1e6:.0f} MB done"
                      f"{f' of {total / 1e6:.0f} MB' if total else ''})...", flush=True)
                with open(part, "ab" if have else "wb") as f:
                    shutil.copyfileobj(r, f, length=1 << 20)
            if total is None or os.path.getsize(part) == total:
                os.replace(part, dest)
                print(f"Downloaded {os.path.getsize(dest) / 1e6:.0f} MB.", flush=True)
                return
        except OSError as e:
            print(f"  connection problem ({e}); retrying...", flush=True)
            time.sleep(min(30, 3 * attempt))
    raise SystemExit(f"Download failed after {tries} attempts; re-run to resume from {part}.")


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def docker(image: str, *args: str) -> None:
    run(["docker", "run", "--rm", "-v", f"{DATA}:/data", image, *args])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="use the whole Southern Zone (no clipping)")
    args = ap.parse_args()
    if not shutil.which("docker"):
        print("Docker is not installed or not on PATH. Install Docker Desktop first.")
        return 1
    os.makedirs(DATA, exist_ok=True)
    src = os.path.join(DATA, "southern-zone-latest.osm.pbf")
    if not os.path.exists(src):
        download(URL, src)
    region = "southern-zone-latest.osm.pbf"
    if not args.full:
        docker(OSMIUM_IMAGE, "extract", "-b", CHENNAI_BBOX, f"/data/{region}", "-o", "/data/chennai.osm.pbf",
               "--overwrite")
        region = "chennai.osm.pbf"
    shutil.copyfile(os.path.join(DATA, region), os.path.join(DATA, "region.osm.pbf"))
    docker(OSRM_IMAGE, "osrm-extract", "-p", "/opt/car.lua", "/data/region.osm.pbf")
    docker(OSRM_IMAGE, "osrm-partition", "/data/region.osrm")
    docker(OSRM_IMAGE, "osrm-customize", "/data/region.osrm")
    print("\nDone. Start it with: docker compose -f infra/docker-compose.yml --profile osrm up -d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
