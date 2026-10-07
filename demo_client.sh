#!/usr/bin/env bash
# Démo bout en bout : recherche puis téléchargement du résultat n°1.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8077}"
FILM="${1:-nosferatu}"
QUALITE="${2:-720}"

curl -s "$BASE/recherche?film=$FILM&uid=123" | python3 -m json.tool | head -25
echo
curl -s "$BASE/stream?film=1&uid=123&qualite=$QUALITE" | python3 -m json.tool
