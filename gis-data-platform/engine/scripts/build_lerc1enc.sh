#!/bin/sh
# Build the Linux/macOS `lerc1enc` binary (Lerc1 "CntZImage" encoder).
# Source: GDAL frmts/mrf/LERCV1 (Esri Lerc1, Apache-2.0) - see lercv1/NOTICE
# Equivalent of scripts/build_lerc1enc.bat (MSVC) for POSIX toolchains.
set -eu
cd "$(dirname "$0")"
CXX="${CXX:-c++}"
"$CXX" -O2 -std=c++17 -I lercv1 lerc1enc.cpp lercv1/Lerc1Image.cpp -o lerc1enc
echo "built $(pwd)/lerc1enc"
