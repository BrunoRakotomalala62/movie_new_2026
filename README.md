# movieapi

API REST de **recherche** et **téléchargement** de films.

Source : [Internet Archive](https://archive.org) — API publique, **aucune clé**, contenu
libre de droits ou sous licence ouverte (domaine public, Creative Commons).

## Lancement

```bash
pip install -r requirements.txt
./run.sh
# -> http://127.0.0.1:8077/docs   (Swagger auto-généré)
```

## Routes

### `GET /recherche`

Recherche un film par titre. Les résultats sont mémorisés sous ton `uid` pendant 30 min,
ce qui permet ensuite de désigner un résultat par son numéro (`film=1`).

```
GET /recherche?film=fall&uid=123
GET /recherche?film=nosferatu&uid=123&page=2&limite=10
```

```json
{
  "requete": "nosferatu",
  "uid": "123",
  "page": 1,
  "total": 311,
  "resultats": [
    {
      "film": 1,
      "identifier": "Nosferatu_most_complete_version_93_mins.",
      "titre": "Nosferatu, eine Symphonie des Grauens (A Symphony of Horror)",
      "annee": 1922,
      "createur": "F. W. Murnau",
      "telechargements": 41233,
      "page": "https://archive.org/details/Nosferatu_most_complete_version_93_mins."
    }
  ],
  "suite": "/recherche?film=nosferatu&uid=123&page=2"
}
```

### `GET /stream`

Résout un résultat et renvoie le fichier vidéo correspondant à la qualité demandée.

`film` accepte **un index numérique** (issu de la dernière `/recherche` du même `uid`)
**ou un identifier** Internet Archive.

```
GET /stream?film=1&uid=123&qualite=720
GET /stream?film=2&uid=123&qualite=1080
GET /stream?film=Nosferatu_most_complete_version_93_mins.&uid=123&qualite=480
GET /stream?film=1&uid=123&qualite=720&redirection=1   # 302 direct vers le fichier
```

```json
{
  "film": "1",
  "uid": "123",
  "titre": "Nosferatu, eine Symphonie des Grauens (A Symphony of Horror)",
  "identifier": "Nosferatu_most_complete_version_93_mins.",
  "qualite_demandee": "720",
  "qualite_servie_px": 360,
  "qualite_exacte": false,
  "approximation": true,
  "fichier": {
    "fichier": "Nosferatu_1922_Symphony_of_Horror_512kb.mp4",
    "format": "512Kb MPEG4",
    "taille_octets": 395416378,
    "taille_lisible": "377.1 Mo",
    "hauteur_estimee_px": 360,
    "qualite_approximative": true,
    "url_directe": "https://archive.org/download/.../Nosferatu_1922_Symphony_of_Horror_512kb.mp4"
  },
  "sous_titres": [],
  "autres_qualites": [ /* les autres fichiers vidéo de l'item */ ],
  "page": "https://archive.org/details/Nosferatu_most_complete_version_93_mins.",
  "note": "Internet Archive ne publie pas systématiquement un fichier par palier 360/480/720/1080..."
}
```

### `GET /health`

`{"status": "ok", "source": "archive.org", "qualites": ["360","480","720","1080"]}`

## Comment la qualité est choisie

Internet Archive ne garantit pas un fichier par palier. L'API :

1. lit la résolution dans le nom du fichier quand elle est explicite (`..._720p_...`) → mesure fiable ;
2. sinon applique les conventions de dérivation d'IA (`512kb` ≈ 360p, `h.264` ≈ 480p) → **estimation** ;
3. départage les ex æquo par conteneur (MP4 h.264 > WebM > Ogg > Matroska > MPEG > AVI) puis par poids ;
4. préfère une résolution **≥** à celle demandée plutôt qu'en dessous.

Deux drapeaux sont exposés pour que le client sache à quoi il a affaire :

| Champ | Sens |
|---|---|
| `approximation` | la résolution est **estimée**, pas mesurée |
| `qualite_exacte` | la résolution servie correspond exactement à celle demandée |

Quand `qualite_exacte` est `false`, `autres_qualites` liste tout ce qui existe pour l'item.

## Codes d'erreur

| Code | Cas |
|---|---|
| `400` | `qualite` hors de `{360,480,720,1080}` |
| `404` | aucune recherche en cache pour cet `uid`, index hors bornes, item inexistant/privé, aucun fichier vidéo |
| `502` | Internet Archive injoignable |

## Limites & points à trancher

- **`uid` n'est pas de l'authentification.** C'est une simple clé de session pour le cache
  mémoire des recherches. À définir si tu veux t'en servir comme contrôle d'accès
  (clé API ? quota ? rate-limit ?) — sinon, autant le retirer.
- **Cache mémoire** : perdu au redémarrage, non partagé entre workers. Pour de la prod,
  passer sur Redis.
- **Dernière recherche gagnante** : deux `/recherche` simultanées avec le même `uid` →
  c'est la dernière qui fait foi pour `/stream`.
- **Qualité non garantie** : dépend de ce qu'Internet Archive expose pour chaque item.
- **Pas de transcodage** : l'API ne fabrique pas de fichier, elle route vers celui qui existe.

## Brancher une autre source

`_lister_fichiers`, `_qualite_estimee` et `_nearest_qualite` sont isolés : remplacer
l'appel à `archive.org/metadata` par ta source suffit à adapter l'API, sans toucher au
contrat des routes.
