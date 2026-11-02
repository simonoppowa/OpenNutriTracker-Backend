#!/usr/bin/env python3
"""Download the Danish Food Composition Database ("Frida", DTU National
Food Institute) dataset workbook into a source folder.

The data browser lives at https://frida.fooddata.dk (now served from
https://fcdb.fooddata.dk); its "Download dataset" link points at DTU
Data, DTU's Figshare repository. Each release is its own Figshare
article (v5.5 = article 29500682, v6.1 = article 32312844, ...), so this
script asks the public Figshare API for the newest DTU Data article titled
"... Danish Food Composition Database ..." that ships a *Dataset*.xlsx,
downloads that workbook, and checks its MD5 against the one Figshare
publishes:

    source/frida/
      FCDB_6.1_Dataset.xlsx                (Readme, Data_Table,
                                            Data_Normalised, Food,
                                            FoodGroup, Parameter, Source)
      FCDB_6.1_Documentation_English.pdf   (only with --docs)

Then convert:
    python frida_to_ont_csv.py --output ../../out/frida   # reads source/frida

Usage:
    python download_frida.py                      # -> ./source/frida
    python download_frida.py --output source      # also -> ./source/frida
    python download_frida.py --docs               # + English documentation PDF
    python download_frida.py --article 32312844   # pin a release
    python download_frida.py --url "https://ndownloader.figshare.com/files/65016537"

No session, form, or token is involved: Figshare serves the files
anonymously. If the API lookup fails (network policy, Figshare change),
open https://doi.org/10.11583/DTU.32312844 in a browser, download
FCDB_*_Dataset.xlsx by hand into source/frida/ (or pass the file's link
with --url), or point the converter at it with --data. --url also takes a
zip of the files (Figshare's "Download all"); its .xlsx/.pdf members are
extracted flat, by basename only, so no member can escape the folder.

License: the dataset (.xlsx/.ods) is CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/); the documentation PDFs are
(c) Technical University of Denmark. Attribution, per the workbook's
Readme sheet:
    The Danish Food Composition Database (https://fcdb.fooddata.dk),
    version 6.1, May 2026, The Food Institute, Technical University of
    Denmark. https://doi.org/10.11583/DTU.32312844
(version and date change with each release.)

Standard library only (urllib, json, hashlib, zipfile).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

API = "https://api.figshare.com/v2"
SEARCH_TERM = "Danish Food Composition Database"
DTU_DOI_PREFIX = "10.11583/DTU."             # DTU Data's DOI namespace
# fallback when the search API is unavailable: FCDB 6.1 (May 2026)
PINNED_ARTICLE = 32312844
DATASET_RE = re.compile(r"dataset.*\.xlsx$", re.IGNORECASE)
DOCS_RE = re.compile(r"documentation.*english.*\.pdf$", re.IGNORECASE)
UA = {"User-Agent": "Mozilla/5.0 (ont-frida-downloader)",
      "Accept": "application/json"}


def api_get(url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA, **({"Content-Type": "application/json"} if data else {}))
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def find_latest_article() -> int | None:
    """Newest DTU Data article about the Danish FCDB with an .xlsx dataset."""
    hits = api_get(f"{API}/articles/search",
                   {"search_for": SEARCH_TERM, "page_size": 100})
    cands = [a for a in hits
             if str(a.get("doi", "")).startswith(DTU_DOI_PREFIX)
             and SEARCH_TERM.lower() in str(a.get("title", "")).lower()]
    for art in sorted(cands, key=lambda a: a.get("published_date", ""),
                      reverse=True):
        meta = api_get(f"{API}/articles/{art['id']}")
        if any(DATASET_RE.search(f.get("name", "")) for f in meta.get("files", [])):
            return int(art["id"])
    return None


def safe_name(name: str) -> str:
    """Basename only - a server-supplied name never picks the directory."""
    base = Path(name.replace("\\", "/")).name
    if base in ("", ".", ".."):
        raise ValueError(f"unsafe file name {name!r}")
    return base


def download(url: str, dest: Path, md5: str | None = None) -> None:
    digest = hashlib.md5()
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=180) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total:
                    print(f"  {done >> 20:,} / {total >> 20:,} MiB "
                          f"({done * 100 // total}%)", end="\r", flush=True)
    print()
    if md5 and digest.hexdigest() != md5:
        dest.unlink(missing_ok=True)
        sys.exit(f"error: MD5 mismatch for {dest.name} (got "
                 f"{digest.hexdigest()}, Figshare says {md5}) - the download "
                 f"is corrupt; run the script again.")


def extract(zip_path: Path, dest_dir: Path) -> list[str]:
    """Extract .xlsx / .pdf members flat into dest_dir (basename only, so
    '../' or absolute member paths cannot escape it)."""
    out = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                name = safe_name(info.filename)
            except ValueError:
                continue
            if not name.lower().endswith((".xlsx", ".pdf")):
                continue
            target = (dest_dir / name).resolve()
            if target.parent != dest_dir.resolve():
                continue
            with zf.open(info) as src, open(target, "wb") as fh:
                shutil.copyfileobj(src, fh)
            out.append(name)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Download the Danish Food Composition Database (Frida)")
    ap.add_argument("--output", type=Path, default=Path("source"),
                    help="base source folder; files go to <output>/frida "
                         "(default: ./source -> ./source/frida)")
    ap.add_argument("--article", type=int, default=None,
                    help="DTU Data / Figshare article id of the release to "
                         "fetch (default: newest found via the API)")
    ap.add_argument("--url", default=None,
                    help="download this .xlsx (or a zip of the release files) "
                         "directly, skipping the API lookup")
    ap.add_argument("--docs", action="store_true",
                    help="also fetch the English documentation PDF")
    args = ap.parse_args()

    dest = args.output if args.output.name == "frida" else args.output / "frida"
    dest.mkdir(parents=True, exist_ok=True)

    if args.url:
        with tempfile.TemporaryDirectory(prefix="frida_dl_") as tmp:
            tmp_file = Path(tmp) / "download"
            print(f"downloading {args.url}")
            try:
                download(args.url, tmp_file)
            except OSError as e:
                sys.exit(f"error: download failed: {e}")
            if not zipfile.is_zipfile(tmp_file):
                sys.exit("error: --url did not return an .xlsx or a zip")
            with zipfile.ZipFile(tmp_file) as zf:
                is_xlsx = "[Content_Types].xml" in zf.namelist()
            if is_xlsx:
                name = safe_name(args.url.split("?")[0].split("/")[-1])
                if not name.lower().endswith(".xlsx"):
                    name = "FCDB_Dataset.xlsx"
                shutil.move(str(tmp_file), dest / name)
                files = [name]
            else:
                files = extract(tmp_file, dest)
        if not any(f.lower().endswith(".xlsx") for f in files):
            sys.exit("error: no .xlsx found in the download")
    else:
        try:
            article = args.article
            if article is None:
                print("looking up the newest release on DTU Data (Figshare) ...")
                try:
                    article = find_latest_article()
                except OSError as e:
                    print(f"  note: search failed ({e}); using pinned "
                          f"article {PINNED_ARTICLE}", file=sys.stderr)
                article = article or PINNED_ARTICLE
            meta = api_get(f"{API}/articles/{article}")
        except OSError as e:
            sys.exit(f"error: could not query the Figshare API: {e}\n"
                     "Download FCDB_*_Dataset.xlsx by hand from "
                     "https://doi.org/10.11583/DTU.32312844 into "
                     f"{dest}/, or pass its link with --url.")
        lic = (meta.get("license") or {}).get("name", "?")
        print(f"release: {meta.get('title')}  (doi {meta.get('doi')}, "
              f"published {str(meta.get('published_date', ''))[:10]}, "
              f"license {lic})")
        wanted = [f for f in meta.get("files", [])
                  if DATASET_RE.search(f.get("name", ""))
                  or (args.docs and DOCS_RE.search(f.get("name", "")))]
        if not any(DATASET_RE.search(f["name"]) for f in wanted):
            sys.exit(f"error: article {article} has no *Dataset*.xlsx - pass "
                     f"--article <id> of a Danish FCDB release or --url.")
        files = []
        for f in wanted:
            name = safe_name(f["name"])
            print(f"{name} ({f.get('size', 0) >> 20} MiB)")
            try:
                download(f["download_url"], dest / name, f.get("computed_md5"))
            except OSError as e:
                sys.exit(f"error: download of {name} failed: {e}")
            files.append(name)

    print(f"saved {len(files)} file(s) -> {dest}")
    for f in sorted(files):
        print(f"  {f}")
    print("\nRemember to attribute 'The Danish Food Composition Database, "
          "National Food Institute, Technical University of Denmark' "
          "(CC BY 4.0) when using this data.")


if __name__ == "__main__":
    main()
