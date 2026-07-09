#!/usr/bin/env python3
"""Download the Bundeslebensmittelschluessel (BLS) 4.0 dataset and
extract its Excel files into a source folder.

BLS 4.0 is Open Data under CC BY 4.0 (Max Rubner-Institut, Karlsruhe) -
no license barrier since the Dec 2025 release. This scrapes the official
download page (https://blsdb.de/download) for the current data zip
(the link carries a rotating token, so it is read fresh each run),
downloads it, and extracts the .xlsx files bls_to_ont_csv.py needs:

    source/bls/
      BLS_4_0_Daten_2025_DE.xlsx        (foods x nutrients)
      BLS_4_0_Components_DE_EN.xlsx      (component catalog, DE + EN)
      BLS_4_0_Dokumentation_DE.pdf       (docs; kept for reference)

Then convert:
    python bls_to_ont_csv.py \
        --data source/bls/BLS_4_0_Daten_2025_DE.xlsx \
        --components source/bls/BLS_4_0_Components_DE_EN.xlsx \
        --output ../../out/bls

Usage:
    python download_bls.py                     # -> ./source/bls
    python download_bls.py --output source     # also -> ./source/bls
    python download_bls.py --output /data --keep-zip   # -> /data/bls
    python download_bls.py --url "https://blsdb.de/assets/uploads/BLS_4_0_2025_DE.zip?token=..."

blsdb.de gates the download behind a per-session token (cookie +
?token=...). The script carries the cookie and reads the token
automatically; if the site layout changes, copy the "Download
BLS-Daten" link from https://blsdb.de/download and pass it via --url.

Attribution required when using the data:
    Max Rubner-Institut (2025): Bundeslebensmittelschluessel (BLS),
    Version 4.0 - Deutsche Naehrstoffdatenbank. Karlsruhe.
    DOI: 10.25826/Data20251217-134202-0

Standard library only (urllib, zipfile).
"""

from __future__ import annotations

import argparse
import http.cookiejar
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urljoin

BASE = "https://blsdb.de"
HOME = BASE + "/"
DOWNLOAD_PAGE = BASE + "/download"
# the data zip, e.g. /assets/uploads/BLS_4_0_2025_DE.zip?token=...
ZIP_RE = re.compile(r'(?:href|src)="([^"]*BLS[^"]*\.zip[^"]*)"', re.IGNORECASE)
# blsdb.de gates links behind a per-session token (?token=XXXX-XXXX-...)
TOKEN_RE = re.compile(r'[?&]token=([A-Za-z0-9-]{8,})')
# fallback: the zip path has been stable across releases
ZIP_PATH_FALLBACK = "/assets/uploads/BLS_4_0_2025_DE.zip"
UA = {"User-Agent": "Mozilla/5.0 (ont-bls-downloader)"}

# one opener with a cookie jar - the token is tied to a session cookie
# that plain urlopen() would drop across the redirect.
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def fetch(url: str) -> bytes:
    with _opener.open(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read()


def find_zip_url(html: str, token: str | None) -> str | None:
    m = ZIP_RE.search(html)
    if m:
        return urljoin(BASE, m.group(1))
    if token:                                    # build it from the token
        return f"{BASE}{ZIP_PATH_FALLBACK}?token={token}"
    return None


def download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=180) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"  {done >> 20:,} / {total >> 20:,} MiB "
                          f"({done * 100 // total}%)", end="\r", flush=True)
    print()


def extract(zip_path: Path, dest_dir: Path) -> list[str]:
    """Extract .xlsx (and .pdf docs) flat into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = Path(info.filename).name
            if info.is_dir() or not name.lower().endswith((".xlsx", ".pdf")):
                continue
            with zf.open(info) as src, open(dest_dir / name, "wb") as fh:
                shutil.copyfileobj(src, fh)
            out.append(name)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Download + extract BLS 4.0 data")
    ap.add_argument("--output", type=Path, default=Path("source"),
                    help="base source folder; files go to <output>/bls "
                         "(default: ./source -> ./source/bls)")
    ap.add_argument("--keep-zip", action="store_true",
                    help="keep the downloaded .zip")
    ap.add_argument("--url", default=None,
                    help="download the BLS zip from this URL directly "
                         "(copy the 'Download BLS-Daten' link from "
                         "https://blsdb.de/download if auto-detect fails)")
    args = ap.parse_args()

    if args.url:
        url = args.url
    else:
        # blsdb.de issues a session token (cookie + ?token=...); visit the
        # home page first so the cookie is set, grab the token, then read
        # the download page with it.
        print("establishing session on blsdb.de ...")
        try:
            home = fetch(HOME).decode("utf-8", "replace")
            m = TOKEN_RE.search(home)
            token = m.group(1) if m else None
            page = DOWNLOAD_PAGE + (f"?token={token}" if token else "")
            html = fetch(page).decode("utf-8", "replace")
            if token is None:                    # token may only appear here
                m = TOKEN_RE.search(html)
                token = m.group(1) if m else None
        except Exception as e:
            sys.exit(f"error: could not load blsdb.de: {e}")
        url = find_zip_url(html, token)
        if not url:
            sys.exit(
                "error: could not find the BLS .zip link automatically.\n"
                "Open https://blsdb.de/download in a browser, copy the\n"
                "'Download BLS-Daten' link, and pass it with --url '<link>'.")
    print(f"data zip: {url.split('?')[0].split('/')[-1]}")

    # always land in <output>/bls so bls_to_ont_csv --source-dir source/bls
    # finds the files (accepts --output source OR --output source/bls).
    dest = args.output if args.output.name == "bls" else args.output / "bls"
    args.output = dest
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bls_dl_") as tmp:
        zip_path = (args.output if args.keep_zip else Path(tmp)) / "bls.zip"
        download(url, zip_path)
        files = extract(zip_path, args.output)

    if not files:
        sys.exit("error: no .xlsx found inside the BLS zip")
    print(f"extracted {len(files)} file(s) -> {args.output}")
    for f in sorted(files):
        print(f"  {f}")
    print("\nRemember to attribute the Max Rubner-Institut (CC BY 4.0) "
          "when using this data.")


if __name__ == "__main__":
    main()
