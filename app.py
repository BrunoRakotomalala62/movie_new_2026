"""
movieapi — API REST de recherche / téléchargement de films
==========================================================
Source : Internet Archive (https://archive.org) — API publique, œuvres libres
         de droits ou sous licence ouverte. Aucune clé requise.

Routes (identiques au besoin exprimé) :
    GET /recherche?film=fall&uid=123
    GET /stream?film=1&uid=123&qualite=360|480|720|1080

`film` sur /stream accepte :
    - un index numérique (1 = 1er résultat de la dernière /recherche du même uid)
    - un identifier Internet Archive directement (ex: night_of_the_living_dead)

`uid` sert de clé de session pour retrouver les résultats d'une recherche
précédente (cache mémoire, TTL). Ce N'EST PAS un mécanisme d'authentification :
à définir (clé API ? quota ?) si tu veux t'en servir comme contrôle d'accès.
"""

import asyncio
import re
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

app = FastAPI(
    title="movieapi",
    description="Recherche et téléchargement de films libres — source Internet Archive.",
    version="1.0.0",
)

# CORS ouvert : pratique en dev ; à restreindre en prod.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

SEARCH_URL = "https://archive.org/advancedsearch.php"
META_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"
DETAILS_URL = "https://archive.org/details/{identifier}"
IMG_URL = "https://archive.org/services/img/{identifier}"
IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif)$", re.IGNORECASE)


async def _meilleure_image(identifier: str) -> Optional[str]:
    """Cherche la plus grande image (jpg/png) dans les fichiers de l'item."""
    try:
        r = await _http().get(META_URL.format(identifier=identifier), timeout=6)
        r.raise_for_status()
        fichiers = (r.json() or {}).get("files") or []
    except Exception:
        return None
    images = [
        f for f in fichiers
        if IMAGE_EXT.search(f.get("name", ""))
        and not f.get("private")
        and "__ia_thumb" not in f.get("name", "")
        and "_thumb" not in f.get("name", "").lower()
    ]
    if not images:
        return None
    # préférer jpg/png ; gif seulement en dernier recours (souvent animés/lourds)
    non_gif = [f for f in images if not f.get("name", "").lower().endswith(".gif")]
    images = non_gif or images
    plus_grande = max(images, key=lambda f: int(f.get("size") or 0))
    return DOWNLOAD_URL.format(identifier=identifier, filename=quote(plus_grande["name"]))

QUALITES_VALIDES = {"360", "480", "720", "1080"}
RESULTATS_PAR_PAGE = 25
CACHE_TTL = 30 * 60          # 30 min
CACHE_MAX_UID = 500          # garde-fou mémoire

VIDEO_EXT = re.compile(r"\.(mp4|m4v|webm|ogv|ogg|avi|mkv|mov|mpe?g)$", re.I)
SOUS_TITRE_EXT = re.compile(r"\.(srt|vtt|sub|ass)$", re.I)

# Index uid -> { "ts": float, "resultats": [...] }
_cache: dict[str, dict[str, Any]] = {}
_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": "movieapi/1.0 (demo; contact: tekenespa@gmail.com)"},
        follow_redirects=True,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client:
        await _client.aclose()


def _http() -> httpx.AsyncClient:
    if _client is None:  # sécurité si startup non déclenché
        raise HTTPException(503, "Client HTTP non initialisé.")
    return _client


# --------------------------------------------------------------------------
# Cache des résultats de recherche (par uid)
# --------------------------------------------------------------------------
def _cache_put(uid: str, resultats: list[dict]) -> None:
    now = time.time()
    # purge TTL
    for k in [k for k, v in _cache.items() if now - v["ts"] > CACHE_TTL]:
        _cache.pop(k, None)
    # purge FIFO si trop d'entrées
    if len(_cache) >= CACHE_MAX_UID and uid not in _cache:
        plus_vieux = min(_cache, key=lambda k: _cache[k]["ts"])
        _cache.pop(plus_vieux, None)
    _cache[uid] = {"ts": now, "resultats": resultats}


def _cache_get(uid: str) -> Optional[list[dict]]:
    entree = _cache.get(uid)
    if not entree:
        return None
    if time.time() - entree["ts"] > CACHE_TTL:
        _cache.pop(uid, None)
        return None
    return entree["resultats"]


# --------------------------------------------------------------------------
# Estimation de qualité
# --------------------------------------------------------------------------
def _hauteur_texte(nom: str) -> Optional[int]:
    """Extrait une hauteur (en px) depuis un nom de fichier, si présente."""
    m = re.search(r"(\d{3,4})\s*[xX]\s*(\d{3,4})", nom)
    if m:
        return int(m.group(2))
    m = re.search(r"(\d{3,4})\s*p", nom, re.I)
    if m:
        return int(m.group(1))
    return None


def _qualite_estimee(fichier: dict) -> tuple[Optional[int], bool]:
    """
    Retourne (hauteur_px, approximatif).
    Si le nom du fichier porte une résolution explicite -> précis.
    Sinon on retombe sur les conventions de dérivation d'Internet Archive.
    """
    nom = fichier.get("name", "")
    h = _hauteur_texte(nom)
    if h:
        return h, False

    fmt = (fichier.get("format") or "").lower()
    nom_bas = nom.lower()

    # conventions historiques des fichiers dérivés d'IA
    if "512kb" in nom_bas or "512kb" in fmt or "mpeg4" in fmt and "h.264" not in fmt:
        return 360, True          # ~360p
    if "256kb" in nom_bas or "256kb" in fmt:
        return 240, True
    if "h.264" in fmt or "h264" in nom_bas:
        return 480, True          # dérivé standard IA, ~480p
    if "matroska" in fmt or "hd" in nom_bas:
        return 720, True
    return None, True


# Préférence de conteneur/codec : plus le rang est bas, mieux c'est.
# Évite de servir un AVI/Cinepack obèse alors qu'un MP4 h.264 existe à résolution égale.
_FORMATS = [
    (("h.264", "h264", "mp4"), 0),
    (("webm", "vp8", "vp9"), 1),
    (("ogg", "ogv"), 2),
    (("matroska", "mkv"), 3),
    (("mpeg4",), 4),
    (("mpeg2", "mpeg", "mpg"), 5),
    (("cinepack", "avi"), 6),
]


def _rang_format(fichier: dict) -> int:
    indice = f"{fichier.get('format', '')} {fichier.get('name', '')}".lower()
    for motif, rang in _FORMATS:
        if any(p in indice for p in motif):
            return rang
    return 7


def _nearest_qualite(cible: int, candidats: list[dict]) -> Optional[dict]:
    """
    Choisit le fichier vidéo dont la qualité estimée est la plus proche de `cible`.
    Ordre de priorité : résolution >= cible (mieux vaut un peu plus grand), puis
    conteneur efficace (MP4 h.264 > WebM > ... > AVI), puis fichier le plus léger.
    """
    scored = []
    for f in candidats:
        h, approx = _qualite_estimee(f)
        if h is None:
            continue
        ecart = abs(h - cible) + (0 if h >= cible else 1000)  # pénalise le sous-dim.
        scored.append((ecart, _rang_format(f), int(f.get("size") or 0), f))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return scored[0][3]


def _lister_fichiers(meta: dict) -> tuple[list[dict], list[dict]]:
    fichiers = meta.get("files") or []
    videos = [
        f for f in fichiers
        if VIDEO_EXT.search(f.get("name", "")) and not f.get("private")
    ]
    sous_titres = [
        f for f in fichiers
        if SOUS_TITRE_EXT.search(f.get("name", "")) and not f.get("private")
    ]
    return videos, sous_titres


def _url_directe(identifier: str, nom_fichier: str) -> str:
    return DOWNLOAD_URL.format(identifier=identifier, filename=quote(nom_fichier))


def _presentation_video(identifier: str, f: dict) -> dict:
    h, approx = _qualite_estimee(f)
    return {
        "fichier": f.get("name"),
        "format": f.get("format"),
        "taille_octets": int(f["size"]) if f.get("size") else None,
        "taille_lisible": _taille_lisible(int(f["size"])) if f.get("size") else None,
        "hauteur_estimee_px": h,
        "qualite_approximative": approx,
        "url_directe": _url_directe(identifier, f.get("name", "")),
    }


def _taille_lisible(octets: int) -> str:
    for unite in ("o", "Ko", "Mo", "Go"):
        if octets < 1024:
            return f"{octets:.0f} {unite}" if unite == "o" else f"{octets:.1f} {unite}"
        octets /= 1024
    return f"{octets:.1f} To"


# --------------------------------------------------------------------------
# GET /recherche
# --------------------------------------------------------------------------
@app.get("/recherche")
async def recherche(
    film: str = Query(..., min_length=2, description="Titre recherché, ex: fall"),
    uid: str = Query(..., min_length=1, description="Identifiant de session côté client"),
    page: int = Query(1, ge=1),
    limite: int = Query(RESULTATS_PAR_PAGE, ge=1, le=50),
):
    async def _chercher(q: str) -> dict:
        params = {
            "q": q,
            "fl[]": ["identifier", "title", "year", "downloads", "mediatype", "creator"],
            "sort[]": "downloads desc",
            "rows": limite,
            "page": page,
            "output": "json",
        }
        try:
            r = await _http().get(SEARCH_URL, params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Source indisponible : {e}")

    # 1) titre exact ; 2) titre avec joker (saisie partielle : "fal" -> fal*) ;
    # 3) recherche élargie tous champs
    mode = "titre"
    data = await _chercher(f'title:("{film}") AND mediatype:(movies)')
    if (data.get("response") or {}).get("numFound", 0) == 0:
        mode = "titre_partiel"
        joker = " ".join(f"{m}*" for m in film.split())
        data = await _chercher(f'title:({joker}) AND mediatype:(movies)')
    if (data.get("response") or {}).get("numFound", 0) == 0:
        mode = "etendu"
        data = await _chercher(f'({film}) AND mediatype:(movies)')

    docs = (data.get("response") or {}).get("docs") or []
    total = (data.get("response") or {}).get("numFound", 0)

    resultats = []
    for i, d in enumerate(docs, start=(page - 1) * limite + 1):
        resultats.append({
            "film": i,                      # index utilisé par /stream
            "identifier": d.get("identifier"),
            "titre": d.get("title"),
            "annee": d.get("year"),
            "createur": d.get("creator"),
            "telechargements": d.get("downloads"),
            "image": IMG_URL.format(identifier=d.get("identifier")),
            "page": DETAILS_URL.format(identifier=d.get("identifier")),
        })

    # grandes images en parallèle (repli : vignette standard)
    grandes = await asyncio.gather(
        *(_meilleure_image(r["identifier"]) for r in resultats),
        return_exceptions=True,
    )
    for r, g in zip(resultats, grandes):
        if isinstance(g, str) and g:
            r["image"] = g

    _cache_put(uid, resultats)

    return JSONResponse({
        "requete": film,
        "mode": mode,
        "uid": uid,
        "page": page,
        "total": total,
        "resultats": resultats,
        "suite": f"/recherche?film={quote(film)}&uid={quote(uid)}&page={page + 1}"
                 if len(resultats) == limite else None,
    })


# --------------------------------------------------------------------------
# GET /stream
# --------------------------------------------------------------------------
@app.get("/stream")
async def stream(
    film: str = Query(..., description="Index numérique (1..n) ou identifier Internet Archive"),
    uid: str = Query(..., min_length=1),
    qualite: str = Query("720", description="360 | 480 | 720 | 1080"),
    redirection: int = Query(0, ge=0, le=1, description="1 = redirige directement vers le fichier vidéo"),
    q: Optional[str] = Query(None, description="Requête de secours : si le cache uid est vide (serverless), relance la recherche avec ce titre"),
):
    if qualite not in QUALITES_VALIDES:
        raise HTTPException(400, f"qualite invalide : {qualite}. Valeurs : {sorted(QUALITES_VALIDES)}")

    # 1) résolution de `film` -> identifier
    identifier: Optional[str] = None
    titre_connu: Optional[str] = None

    if film.isdigit():
        resultats = _cache_get(uid)
        if resultats is None and q:
            # Secours serverless : le cache mémoire ne survit pas toujours entre
            # deux invocations (Vercel). On relance la recherche de façon déterministe.
            reponse = await recherche(film=q, uid=uid, page=1, limite=RESULTATS_PAR_PAGE)
            import json as _json
            resultats = _json.loads(reponse.body).get("resultats") or []
        if resultats is None:
            raise HTTPException(
                404,
                "Aucune recherche en cache pour cet uid. Appelle d'abord /recherche?film=...&uid=... "
                "(sur Vercel, ajoute &q=<titre> à /stream en secours).",
            )
        idx = int(film)
        if not (1 <= idx <= len(resultats)):
            raise HTTPException(404, f"film={idx} hors bornes (1..{len(resultats)}) pour cet uid.")
        identifier = resultats[idx - 1]["identifier"]
        titre_connu = resultats[idx - 1].get("titre")
    else:
        identifier = film

    # 2) métadonnées de l'item
    try:
        r = await _http().get(META_URL.format(identifier=identifier))
        r.raise_for_status()
        meta = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Source indisponible : {e}")

    if not meta or meta.get("is_dark"):
        raise HTTPException(404, f"Item introuvable ou non public : {identifier}")

    videos, sous_titres = _lister_fichiers(meta)
    if not videos:
        raise HTTPException(404, f"Aucun fichier vidéo exploitable pour : {identifier}")

    cible = int(qualite)
    choisi = _nearest_qualite(cible, videos)

    if choisi is None:
        # aucun indice de résolution : on prend le plus gros fichier
        choisi = max(videos, key=lambda f: int(f.get("size") or 0))

    details_choisi = _presentation_video(identifier, choisi)
    autres = [
        _presentation_video(identifier, f) for f in videos if f is not choisi
    ]
    autres.sort(key=lambda v: (-(v["hauteur_estimee_px"] or 0),))
    details_choisi["qualite_exacte"] = details_choisi["hauteur_estimee_px"] == cible

    if redirection:
        return RedirectResponse(details_choisi["url_directe"], status_code=302)

    return JSONResponse({
        "film": film,
        "uid": uid,
        "titre": titre_connu or (meta.get("metadata") or {}).get("title"),
        "identifier": identifier,
        "qualite_demandee": qualite,
        "qualite_servie_px": details_choisi["hauteur_estimee_px"],
        "qualite_exacte": details_choisi["qualite_exacte"],
        "approximation": bool(details_choisi["qualite_approximative"]),
        "fichier": details_choisi,
        "sous_titres": [
            {
                "fichier": s.get("name"),
                "langue": s.get("language") or s.get("title"),
                "url_directe": _url_directe(identifier, s.get("name", "")),
            }
            for s in sous_titres
        ],
        "autres_qualites": autres,
        "page": DETAILS_URL.format(identifier=identifier),
        "note": (
            "Internet Archive ne publie pas systématiquement un fichier par palier "
            "360/480/720/1080 : la qualité servie est la plus proche disponible. "
            "`approximation: true` signale une estimation, pas une mesure."
        ),
    })


@app.get("/health")
async def health():
    return {"status": "ok", "source": "archive.org", "qualites": sorted(QUALITES_VALIDES)}
