#!/usr/bin/env python3
"""Download the ANSES CIQUAL food composition table (XML export) into a
source folder.

Since the 2025 release ANSES publishes CIQUAL on recherche.data.gouv.fr
(a Dataverse instance) - the old data.gouv.fr / ciqual.anses.fr links
point to the superseded 2020 table. This script asks the Dataverse API
for the newest "Table de composition nutritionnelle des aliments Ciqual
<year>" dataset (falling back to the known 2025 DOI), then downloads its
XML files plus the PDF documentation:

    source/ciqual/
      alim_2025_11_03.xml       foods: code, French + English name,
                                scientific name, group codes
      alim_grp_2025_11_03.xml   food groups / sub-groups (FR + EN)
      compo_2025_11_03.xml      composition: food x constituent values
      const_2025_11_03.xml      constituent catalog (FR + EN, INFOODS)
      sources_2025_11_03.xml    literature citations behind each value
      Table Ciqual 2025 doc FR_2025_11_19.pdf   (docs; kept for reference)
      ciqual_release.json       DOI / title / version of what was fetched

Then convert:
    python ciqual_to_ont_csv.py --output ../../out/ciqual   # reads source/ciqual

Usage:
    python download_ciqual.py                      # -> ./source/ciqual
    python download_ciqual.py --output source      # also -> ./source/ciqual
    python download_ciqual.py --doi doi:10.57745/RDMHWY   # pin a release
    python download_ciqual.py --url https://.../XML_2020_07_07.zip
        # a zip of the XML files (e.g. the legacy 2020 export); only
        # *.xml / *.pdf members are extracted, flattened, traversal-safe

License: Licence Ouverte / Etalab 2.0 (open licence; reuse, modification
and redistribution allowed with attribution). Attribution required:
    Anses. 2025. Table de composition nutritionnelle des aliments Ciqual
    (detailed: "Anses. 2025. Table de composition nutritionnelle des
    aliments Ciqual 2025. https://doi.org/10.57745/RDMHWY")
The source and its version must be named and the data must not be
presented in a misleading way (ANSES reuse conditions).

Standard library only (urllib, json, zipfile).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

DATAVERSE = "https://entrepot.recherche.data.gouv.fr"
KNOWN_DOI = "doi:10.57745/RDMHWY"            # CIQUAL 2025 (Nov 2025)
TITLE_PREFIX = "Table de composition nutritionnelle des aliments Ciqual"
TITLE_RE = re.compile(re.escape(TITLE_PREFIX) + r"\s+(\d{4})\s*$")
# files we keep from the dataset: the five XML tables + the PDF docs
WANTED_RE = re.compile(
    r"^(?:(?:alim|alim_grp|compo|const|sources)_\d{4}_\d{2}_\d{2}\.xml"
    r"|[^/\\]*\.pdf)$", re.IGNORECASE)
UA = {"User-Agent": "Mozilla/5.0 (ont-ciqual-downloader)"}


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def latest_doi() -> str:
    """Newest published 'Table ... Ciqual <year>' dataset on the
    Dataverse; KNOWN_DOI if the search fails or finds nothing."""
    q = urllib.parse.quote(f'title:"{TITLE_PREFIX}"')
    try:
        items = fetch_json(f"{DATAVERSE}/api/search?q={q}&type=dataset"
                           f"&per_page=50")["data"]["items"]
    except Exception as e:                      # noqa: BLE001 - network
        print(f"  note: Dataverse search failed ({e}); using {KNOWN_DOI}",
              file=sys.stderr)
        return KNOWN_DOI
    best = None
    for it in items:
        m = TITLE_RE.match(it.get("name") or "")
        if m and it.get("global_id"):
            key = (int(m.group(1)), it.get("published_at") or "")
            if best is None or key > best[0]:
                best = (key, it["global_id"])
    return best[1] if best else KNOWN_DOI


def safe_name(name: str) -> str | None:
    """Basename only; reject anything that could escape the dest dir."""
    base = Path(name.replace("\\", "/")).name
    if not base or base in (".", "..") or base != name.strip():
        return None
    return base


def download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=300) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"  {dest.name}: {done >> 20:,} / {total >> 20:,} MiB "
                          f"({done * 100 // total}%)", end="\r", flush=True)
    print(f"  {dest.name}: {dest.stat().st_size:,} bytes" + " " * 20)


def from_dataverse(doi: str, dest: Path) -> list[str]:
    meta = fetch_json(f"{DATAVERSE}/api/datasets/:persistentId/"
                      f"?persistentId={urllib.parse.quote(doi, safe=':/')}")
    version = meta["data"]["latestVersion"]
    title = next((f["value"] for f in version["metadataBlocks"]["citation"]
                  ["fields"] if f["typeName"] == "title"), "")
    license_ = (version.get("license") or {}).get("name", "")
    print(f"release: {title} ({doi}, v{version.get('versionNumber')}."
          f"{version.get('versionMinorNumber')}, license: {license_})")
    if "etalab" not in license_.lower():
        print(f"  WARNING: license is {license_!r}, not Etalab/Licence "
              f"Ouverte - check reuse terms before redistributing.",
              file=sys.stderr)

    got = []
    for f in version["files"]:
        df = f["dataFile"]
        name = safe_name(df.get("filename") or "")
        if not name or not WANTED_RE.match(name):
            continue
        download(f"{DATAVERSE}/api/access/datafile/{int(df['id'])}",
                 dest / name)
        got.append(name)
    (dest / "ciqual_release.json").write_text(json.dumps({
        "doi": doi, "title": title, "license": license_,
        "version": f"{version.get('versionNumber')}."
                   f"{version.get('versionMinorNumber')}",
        "release_time": version.get("releaseTime"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return got


def from_zip(url: str, dest: Path, keep_zip: bool) -> list[str]:
    got = []
    with tempfile.TemporaryDirectory(prefix="ciqual_dl_") as tmp:
        zip_path = (dest if keep_zip else Path(tmp)) / "ciqual.zip"
        download(url, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                # flatten: only the member's basename is used, so '../' or
                # absolute paths inside the archive cannot escape dest
                name = Path(info.filename.replace("\\", "/")).name
                if info.is_dir() or not name.lower().endswith((".xml", ".pdf")) \
                        or name in (".", ".."):
                    continue
                target = (dest / name).resolve()
                if target.parent != dest.resolve():
                    continue
                with zf.open(info) as src, open(target, "wb") as fh:
                    shutil.copyfileobj(src, fh)
                got.append(name)
    return got


def main() -> None:
    ap = argparse.ArgumentParser(description="Download the ANSES CIQUAL XML tables")
    ap.add_argument("--output", type=Path, default=Path("source"),
                    help="base source folder; files go to <output>/ciqual "
                         "(default: ./source -> ./source/ciqual)")
    ap.add_argument("--doi", default=None,
                    help=f"Dataverse DOI of the release (default: newest "
                         f"found, else {KNOWN_DOI})")
    ap.add_argument("--url", default=None,
                    help="download a zip of the CIQUAL XML files from this "
                         "URL instead of the Dataverse API")
    ap.add_argument("--keep-zip", action="store_true",
                    help="with --url: keep the downloaded .zip")
    args = ap.parse_args()

    dest = args.output if args.output.name == "ciqual" else args.output / "ciqual"
    dest.mkdir(parents=True, exist_ok=True)

    try:
        if args.url:
            files = from_zip(args.url, dest, args.keep_zip)
        else:
            doi = args.doi or latest_doi()
            files = from_dataverse(doi, dest)
    except zipfile.BadZipFile:
        sys.exit("error: --url did not return a zip file - pass the link to "
                 "the CIQUAL XML zip, or omit --url to use the Dataverse API")
    except Exception as e:                      # noqa: BLE001 - network/API
        sys.exit(f"error: download failed: {e}\nCheck your connection, or "
                 f"open https://doi.org/10.57745/RDMHWY in a browser and pass "
                 f"a newer release with --doi.")

    xml = [f for f in files if f.lower().endswith(".xml")]
    needed = ("alim_", "alim_grp_", "compo_", "const_")
    missing = [p for p in needed
               if not any(f.startswith(p) and (p != "alim_" or not
                                               f.startswith("alim_grp_"))
                          for f in xml)]
    if missing:
        sys.exit(f"error: release lacks required XML file(s): "
                 f"{', '.join(p + '*.xml' for p in missing)} - got {files}")
    print(f"downloaded {len(files)} file(s) -> {dest}")
    for f in sorted(files):
        print(f"  {f}")
    print("\nRemember to attribute ANSES (Licence Ouverte / Etalab 2.0): "
          "\"Anses. 2025. Table de composition nutritionnelle des aliments "
          "Ciqual\".")


if __name__ == "__main__":
    main()
