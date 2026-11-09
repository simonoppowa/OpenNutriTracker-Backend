#!/usr/bin/env python3
"""Download the Fineli open-data package (Finnish food composition
database, THL) and extract its CSV files into a source folder.

Fineli is published by the Finnish Institute for Health and Welfare (THL)
as open data under CC BY 4.0 (https://fineli.fi/fineli/en/avoin-data).
The page offers several zip packages of semicolon-separated CSVs; the one
fineli_to_ont_csv.py wants is "basic package 2" - the ~4.2k foods with
the full ~74-component set (energy, macros, fatty acids incl. trans,
vitamins A/D/B6/B12, niacin, minerals) plus household portions
(foodaddunit.csv) and Finnish / Swedish / English names. The richer
package is picked automatically: every candidate zip is opened and the
one whose component.csv lists the most components wins.

    source/fineli/
      food.csv, foodname_EN.csv, foodname_FI.csv, foodname_SV.csv,
      foodname_TX.csv, component.csv, component_value.csv,
      foodaddunit.csv, foodunit_EN.csv, fuclass_EN.csv, ...
      descript.txt                       (THL's data description + licence)

Then convert:
    python fineli_to_ont_csv.py --output ../../out/fineli   # reads source/fineli

Usage:
    python download_fineli.py                      # -> ./source/fineli
    python download_fineli.py --output source      # also -> ./source/fineli
    python download_fineli.py --url https://fineli.fi/fineli/content/file/49
    python download_fineli.py --zip ~/Downloads/Fineli_Rel20__74_ravintotekijaa.zip

fineli.fi sits behind a bot-protection challenge that may refuse scripted
requests outright (HTTP 403 "Just a moment..."). This script does NOT try
to get around it: when that happens it stops with an error telling you to
download the package in a browser from https://fineli.fi/fineli/en/avoin-data
and hand the saved zip over with --zip.

Attribution required when using the data (CC BY 4.0):
    Fineli. Finnish food composition database. Release 20.0 (or the
    release in descript.txt). Finnish Institute for Health and Welfare
    (THL), Helsinki. https://fineli.fi - licensed CC BY 4.0.

Standard library only (urllib, zipfile).
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin

BASE = "https://fineli.fi"
OPEN_DATA_PAGE = BASE + "/fineli/en/avoin-data"
# package links on the open-data page look like /fineli/content/file/<n>
LINK_RE = re.compile(r'href="([^"]*(?:/content/file/\d+|\.zip)[^"]*)"',
                     re.IGNORECASE)
# known package ids, tried when the page itself cannot be parsed:
# 49 = basic package 2 (74 components), 47 = basic package 1 (55)
FALLBACK_FILES = [BASE + "/fineli/content/file/49",
                  BASE + "/fineli/content/file/47"]
UA = {"User-Agent": "ont-fineli-downloader (+https://github.com/simonoppowa/OpenNutriTracker)"}
REQUIRED = {"food.csv", "component.csv", "component_value.csv",
            "foodname_en.csv"}
KEEP_SUFFIXES = (".csv", ".txt")
MANUAL_HINT = (
    "Open https://fineli.fi/fineli/en/avoin-data in a browser, download the\n"
    "package with the most components (basic package 2, ~74 components),\n"
    "and run:  python download_fineli.py --zip <the downloaded .zip>")


class Blocked(Exception):
    """fineli.fi answered with a bot-protection challenge."""


def fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read(4096).decode("utf-8", "replace")
        if e.code in (403, 429, 503) and (
                "Just a moment" in body or "challenge" in body.lower()
                or e.headers.get("cf-mitigated") or e.headers.get("cf-ray")):
            raise Blocked(f"{url}: HTTP {e.code} bot-protection challenge")
        raise


def package_links(html: str) -> list[str]:
    seen, out = set(), []
    for href in LINK_RE.findall(html):
        url = urljoin(OPEN_DATA_PAGE, href.replace("&amp;", "&"))
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Refuse zips with absolute or parent-relative paths, then return the
    files worth keeping (.csv / .txt). Extraction is flattened to the base
    name, so nothing can land outside the destination folder."""
    bad = [i.filename for i in zf.infolist()
           if i.filename.startswith(("/", "\\")) or ":" in i.filename
           or ".." in PurePosixPath(i.filename.replace("\\", "/")).parts]
    if bad:
        sys.exit(f"error: zip contains unsafe paths: {bad[:3]} - refusing "
                 f"to extract it")
    return [i for i in zf.infolist() if not i.is_dir()
            and PurePosixPath(i.filename.replace("\\", "/")).name.lower()
            .endswith(KEEP_SUFFIXES)]


def component_count(data: bytes) -> int:
    """Components listed in a candidate package, or -1 if the bytes are not
    a Fineli nutrient package (not a zip, or missing core files)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return -1
    with zf:
        names = {PurePosixPath(n.replace("\\", "/")).name.lower(): n
                 for n in zf.namelist()}
        if not REQUIRED <= names.keys():
            return -1
        lines = zf.read(names["component.csv"]).splitlines()
        return max(len([ln for ln in lines if ln.strip()]) - 1, 0)


def extract(data: bytes, dest: Path) -> list[str]:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = safe_members(zf)          # exits before touching disk
        dest.mkdir(parents=True, exist_ok=True)
        for info in members:
            name = PurePosixPath(info.filename.replace("\\", "/")).name
            with zf.open(info) as src, open(dest / name, "wb") as fh:
                shutil.copyfileobj(src, fh)
            out.append(name)
    return out


def pick_from_site() -> bytes:
    """Read the open-data page, try every package link (plus the known
    fallbacks) and return the zip with the most components."""
    candidates: list[str] = []
    try:
        html = fetch(OPEN_DATA_PAGE, timeout=60).decode("utf-8", "replace")
        candidates = package_links(html)
        print(f"open-data page: {len(candidates)} package link(s)")
    except Blocked:
        raise
    except Exception as e:
        print(f"  note: could not read {OPEN_DATA_PAGE} ({e}); "
              f"trying the known package ids", file=sys.stderr)
    candidates += [u for u in FALLBACK_FILES if u not in candidates]

    best, best_n = None, -1
    for url in candidates:
        try:
            data = fetch(url)
        except Blocked:
            raise
        except Exception as e:
            print(f"  skip {url}: {e}", file=sys.stderr)
            continue
        n = component_count(data)
        print(f"  {url}: " + (f"{n} components" if n >= 0
                              else "not a Fineli nutrient package"))
        if n > best_n:
            best, best_n = data, n
    if best is None or best_n < 0:
        sys.exit("error: no Fineli nutrient package found on fineli.fi.\n"
                 + MANUAL_HINT)
    return best


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Download + extract the Fineli open-data package")
    ap.add_argument("--output", type=Path, default=Path("source"),
                    help="base source folder; files go to <output>/fineli "
                         "(default: ./source -> ./source/fineli)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--url", default=None,
                     help="download the package zip from this URL directly")
    src.add_argument("--zip", type=Path, default=None,
                     help="use an already-downloaded package zip (e.g. "
                          "saved from a browser when fineli.fi blocks "
                          "scripts)")
    ap.add_argument("--keep-zip", action="store_true",
                    help="keep a copy of the zip as <output>/fineli/fineli.zip")
    args = ap.parse_args()

    try:
        if args.zip:
            if not args.zip.is_file():
                sys.exit(f"error: {args.zip} not found")
            data = args.zip.read_bytes()
        elif args.url:
            print(f"downloading {args.url} ...")
            data = fetch(args.url)
        else:
            print(f"looking for packages on {OPEN_DATA_PAGE} ...")
            data = pick_from_site()
    except Blocked as e:
        sys.exit(f"error: fineli.fi refused the scripted download ({e}).\n"
                 + MANUAL_HINT)
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"error: could not download from fineli.fi: {e}\n"
                 + MANUAL_HINT)

    n = component_count(data)
    if n < 0:
        sys.exit("error: that file is not a Fineli nutrient package (need a "
                 "zip with food.csv, component.csv, component_value.csv and "
                 "foodname_EN.csv).\n" + MANUAL_HINT)

    # always land in <output>/fineli (accepts --output source OR source/fineli)
    dest = args.output if args.output.name == "fineli" else args.output / "fineli"
    files = extract(data, dest)
    if args.keep_zip:
        (dest / "fineli.zip").write_bytes(data)

    release = ""
    desc = dest / "descript.txt"
    if desc.exists():
        m = re.search(rb"Release\.?\s+([\d.]+)", desc.read_bytes())
        release = m.group(1).decode() if m else ""
    print(f"extracted {len(files)} file(s) -> {dest}  "
          f"({n} components{', release ' + release if release else ''})")
    print("\nRemember to attribute THL (Fineli, CC BY 4.0) when using this data.")


if __name__ == "__main__":
    main()
