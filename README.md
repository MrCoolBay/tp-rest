# API d'administration de conteneurs

API FastAPI qui gère les conteneurs Docker portant le label `adminapi.managed=true` et synchronise leur état dans NetBox.

## Installation

Toutes les commandes se lancent depuis ce dossier, qui est la racine du dépôt Git.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
docker compose up -d docker-api netbox

docker pull nginx:alpine
docker pull redis:alpine
docker pull httpd:alpine
```

Attendre que NetBox soit prêt sur http://127.0.0.1:8080 (compte de TP : `admin` / `admin`). Créer un token **v2 avec écriture autorisée** dans le profil utilisateur. Copier le token complet au format `nbt_<clé>.<secret>` dans `.env`.

Renseigner les six variables dans `.env` :

| Variable | Utilisation |
| --- | --- |
| `DOCKER_API_URL` | `http://127.0.0.1:2375` pour un lancement Python local |
| `NETBOX_URL` | `http://127.0.0.1:8080` pour un lancement Python local |
| `NETBOX_TOKEN` | Token complet NetBox avec accès en écriture |
| `API_TOKEN_ADMIN` | Secret pour créer, lire, démarrer, arrêter et supprimer |
| `API_TOKEN_READER` | Secret différent, pour la lecture uniquement |
| `WEBHOOK_SECRET` | Troisième secret différent, pour les alertes |

Générer chaque secret avec `.venv/bin/python -c 'import secrets; print(secrets.token_hex(24))'`. Ne pas committer `.env`.

Le cluster `docker-local` et son type `Docker` sont créés automatiquement s'ils manquent.

## Lancer l'API

Avec Docker :

```sh
docker compose up -d --build api
```

Ou en Python local (sans lancer le service `api`) :

```sh
.venv/bin/uvicorn main:app --env-file .env --host 127.0.0.1 --port 8000
```

- Documentation interactive : http://127.0.0.1:8000/docs
- Santé : http://127.0.0.1:8000/health
- Métriques : http://127.0.0.1:8000/metrics

Si le port 8000 est occupé, utiliser `API_PORT=18000 docker compose up -d api` (ou `--port 18000` en Python), puis adapter l'URL des commandes et du navigateur. Le conteneur écoute toujours sur le port interne 8000.

## Routes

Les routes `/containers` nécessitent `Authorization: Bearer <token>`. Le lecteur peut seulement consulter ; les écritures nécessitent le token admin.

| Méthode | Route | Résultat |
| --- | --- | --- |
| GET | `/health` | État de l'API, sans authentification |
| GET | `/containers` | Liste des conteneurs gérés, même arrêtés |
| GET | `/containers/{id}` | Détail d'un conteneur géré |
| POST | `/containers` | Création et démarrage, code 201 |
| POST | `/containers/{id}/stop` | Arrêt, code 200 |
| POST | `/containers/{id}/start` | Démarrage, code 200 |
| DELETE | `/containers/{id}` | Suppression forcée, code 204 sans corps |
| POST | `/webhooks/alerts` | Remédiation, code 202 |
| GET | `/metrics` | Métriques Prometheus, sans authentification |

Exemple de corps de création :

```json
{"name": "web1", "image": "nginx:alpine"}
```

Les images autorisées sont `nginx:alpine`, `redis:alpine` et `httpd:alpine`. Le nom respecte `^[a-z][a-z0-9-]{1,30}$`.

Le webhook attend le header `X-Webhook-Secret` et ce corps :

```json
{"alert": "container_down", "container": "web1"}
```

Il redémarre seulement un conteneur géré qui ne tourne plus, met à jour NetBox et accepte une alerte rejouée sans redémarrage supplémentaire. Le script fourni `surveillant.py` émet ces alertes vers le port 8000 ; pour la démonstration avec ce script, libérer ce port et y lancer l'API.

## Vérifications

Tests unitaires, sans Docker :

```sh
.venv/bin/python -m unittest -v
```

Vérification complète avec les vrais services lancés et `.env` renseigné :

```sh
.venv/bin/python verifier.py
```

Sur un port différent :

```sh
.venv/bin/python verifier.py --api http://127.0.0.1:18000
```

Le vérificateur crée puis nettoie ses conteneurs préfixés `verif-` et leurs machines NetBox.

## Fonctionnement et limites

- Un conteneur sans le label attendu est invisible et renvoie 404 avant toute action.
- Les statuts NetBox sont `active` pour un conteneur démarré et `offline` après arrêt. Une suppression efface sa machine virtuelle dans le cluster `docker-local` uniquement.
- `adminapi_containers{state="..."}` est relu dans Docker à chaque appel. `adminapi_remediations_total` compte les redémarrages effectifs du webhook, pas les alertes rejouées.
- Le compteur est en mémoire et repart à zéro au redémarrage. L'API utilise un seul processus Uvicorn, notamment pour sérialiser les alertes concurrentes.
- Les appels sortants utilisent `requests` avec des délais d'attente. Un service distant indisponible renvoie 502 ; une configuration manquante renvoie 503.
- Docker et NetBox ne partagent pas de transaction : si NetBox échoue après une action Docker, celle-ci peut déjà être effectuée. Il faut vérifier les deux services avant de rejouer une écriture. Une alerte rejouée resynchronise NetBox.
- Le proxy Docker et les identifiants NetBox fournis conviennent seulement à un TP local. Ne pas les exposer sur Internet.

Arrêter l'environnement sans supprimer les données : `docker compose down`.
