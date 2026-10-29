#!/usr/bin/env python3
"""Build the viewer's offline gazetteer (public/places.tsv) from GeoNames.

Source: GeoNames cities15000 (all cities with population >= 15000),
https://download.geonames.org/export/dump/cities15000.zip
Licence: CC-BY 4.0 -- "Contains data from GeoNames (https://www.geonames.org/)".

The viewer never calls an online geocoder; it lazily loads this static file
on the first place-name query (src/goto.js parses and searches it).

Usage (stdlib only):
    curl -LO https://download.geonames.org/export/dump/cities15000.zip
    python scripts/build_places.py cities15000.zip public/places.tsv

Output: UTF-8 TSV, one place per line, after a "#" header comment line:
    name <TAB> asciiname (empty when equal to name) <TAB> arabic names ("|"-separated)
         <TAB> country code <TAB> lat <TAB> lon <TAB> population
         <TAB> Latin aliases ("|"-separated; Saudi places and cities >= 1M only)
Only alternate names written purely in the basic Arabic alphabet are kept
(U+0621-U+0652 plus spaces/hyphens); Persian/Urdu-specific letters are dropped
to keep the file small. Arabic duplicates after normalisation are removed.
"""
import io
import re
import sys
import zipfile

ARABIC_ONLY = re.compile(r"^[\u0621-\u0652\u0640 \-]+$")
HAS_ARABIC_LETTER = re.compile(r"[\u0621-\u064A]")
LATIN_ALIAS = re.compile(r"^[A-Za-z][A-Za-z' .\-]{2,40}$")
MAX_ARABIC = 4
MAX_ALIASES = 32
# English/Latin exonyms ("Mecca", "Medina", "Jiddah") are kept only where they
# matter most -- Saudi places and big cities -- to keep the file small.
ALIAS_COUNTRIES = {"SA"}
ALIAS_MIN_POPULATION = 1_000_000


def normalize_arabic(text):
    """Must match normalizeText() in src/goto.js for the Arabic subset."""
    text = re.sub(r"[\u064B-\u0652\u0640]", "", text)          # harakat, tatweel
    text = re.sub(r"[\u0622\u0623\u0625\u0671]", "\u0627", text)  # alef forms
    text = text.replace("\u0629", "\u0647").replace("\u0649", "\u064A")
    text = text.replace("\u0624", "\u0648").replace("\u0626", "\u064A")
    return re.sub(r"[\s\-]+", " ", text).strip()


def clean(text):
    return text.replace("\t", " ").replace("|", " ").strip()


def rows(lines):
    for line in lines:
        cols = line.rstrip("\n").split("\t")
        if len(cols) < 15:
            continue
        name, ascii_name, alternates = clean(cols[1]), clean(cols[2]), cols[3]
        lat, lon = float(cols[4]), float(cols[5])
        country, population = cols[8], int(cols[14] or 0)
        arabic, seen = [], set()
        aliases, alias_seen = [], {name.casefold(), ascii_name.casefold()}
        want_aliases = country in ALIAS_COUNTRIES or population >= ALIAS_MIN_POPULATION
        for alt in alternates.split(","):
            alt = alt.strip()
            if (want_aliases and len(aliases) < MAX_ALIASES and LATIN_ALIAS.match(alt)
                    and not alt.isupper() and alt.casefold() not in alias_seen):
                alias_seen.add(alt.casefold())
                aliases.append(clean(alt))
                continue
            if len(arabic) >= MAX_ARABIC or not alt or not HAS_ARABIC_LETTER.search(alt) or not ARABIC_ONLY.match(alt):
                continue
            key = normalize_arabic(alt)
            if key and key not in seen:
                seen.add(key)
                arabic.append(clean(alt))
            if len(arabic) >= MAX_ARABIC and (not want_aliases or len(aliases) >= MAX_ALIASES):
                break
        yield (name, "" if ascii_name == name else ascii_name, "|".join(arabic), country,
               f"{lat:.4f}".rstrip("0").rstrip("."), f"{lon:.4f}".rstrip("0").rstrip("."), str(population),
               "|".join(aliases))


def main(source, target):
    if source.endswith(".zip"):
        with zipfile.ZipFile(source) as archive:
            text = archive.read("cities15000.txt").decode("utf-8")
    else:
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
    places = sorted(rows(io.StringIO(text)), key=lambda row: -int(row[6]))
    with open(target, "w", encoding="utf-8", newline="\n") as out:
        out.write("# GeoNames cities15000 (CC-BY 4.0, https://www.geonames.org/) "
                  "name\tascii\tarabic\tcc\tlat\tlon\tpop\taliases\n")
        for row in places:
            out.write("\t".join(row) + "\n")
    print(f"{len(places)} places -> {target}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
