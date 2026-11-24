import json
import os
import secrets
from typing import Annotated, Literal
from urllib.parse import quote

import requests
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

app = FastAPI(title="API d'administration de conteneurs", version="1.0.0")
bearer = HTTPBearer(auto_error=False)
ETIQUETTE = "adminapi.managed"
ERREURS = {
    401: {"description": "Token absent ou inconnu"},
    403: {"description": "Rôle insuffisant"},
    404: {"description": "Conteneur inconnu ou non géré"},
    409: {"description": "Conflit avec l'état actuel"},
}


class Conteneur(BaseModel):
    id: str
    name: str
    image: str
    state: str


class ConteneurACreer(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$")
    image: Literal["nginx:alpine", "redis:alpine", "httpd:alpine"]


def configuration(nom):
    valeur = os.environ.get(nom, "").strip()
    if not valeur:
        raise HTTPException(503, f"Configuration manquante : {nom}")
    return valeur


def verifier_role(jeton: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if jeton:
        for variable, role in (("API_TOKEN_ADMIN", "admin"), ("API_TOKEN_READER", "lecteur")):
            attendu = configuration(variable)
            if secrets.compare_digest(jeton.credentials.encode(), attendu.encode()):
                return role
    raise HTTPException(401, "Token absent ou inconnu", headers={"WWW-Authenticate": "Bearer"})


def verifier_admin(role: Annotated[str, Depends(verifier_role)]):
    if role != "admin":
        raise HTTPException(403, "Le rôle admin est nécessaire")


def docker(methode, chemin, **options):
    try:
        reponse = requests.request(
            methode, configuration("DOCKER_API_URL").rstrip("/") + chemin,
            timeout=15, **options,
        )
    except requests.RequestException:
        raise HTTPException(502, "API Docker injoignable") from None
    if reponse.status_code == 404:
        raise HTTPException(404, "Ressource Docker inconnue")
    if reponse.status_code in (304, 409):
        raise HTTPException(409, "Conflit avec l'état Docker")
    if not reponse.ok:
        raise HTTPException(502, f"Erreur Docker ({reponse.status_code})")
    return reponse


def lister_conteneurs():
    filtres = json.dumps({"label": [f"{ETIQUETTE}=true"]})
    donnees = docker("GET", "/containers/json", params={"all": "true", "filters": filtres}).json()
    return [
        Conteneur(id=c["Id"], name=c["Names"][0].lstrip("/"), image=c["Image"], state=c["State"])
        for c in donnees if (c.get("Labels") or {}).get(ETIQUETTE) == "true"
    ]


def lire_conteneur(identifiant):
    c = docker("GET", f"/containers/{quote(identifiant, safe='')}/json").json()
    if (c["Config"].get("Labels") or {}).get(ETIQUETTE) != "true":
        raise HTTPException(404, "Conteneur inconnu ou non géré")
    return Conteneur(
        id=c["Id"], name=c["Name"].lstrip("/"),
        image=c["Config"]["Image"], state=c["State"]["Status"],
    )


def netbox(methode, chemin, **options):
    url = configuration("NETBOX_URL").rstrip("/") + "/api" + chemin
    entetes = {"Authorization": f"Bearer {configuration('NETBOX_TOKEN')}", "Accept": "application/json"}
    try:
        reponse = requests.request(methode, url, headers=entetes, timeout=10, **options)
    except requests.RequestException:
        raise HTTPException(502, "NetBox injoignable") from None
    if not reponse.ok:
        raise HTTPException(502, f"Erreur NetBox ({reponse.status_code})")
    return reponse


def cluster_netbox():
    chemin = "/virtualization/clusters/"
    clusters = netbox("GET", chemin, params={"name": "docker-local"}).json()["results"]
    if clusters:
        return clusters[0]["id"]
    types = netbox("GET", "/virtualization/cluster-types/", params={"slug": "docker"}).json()["results"]
    if types:
        type_id = types[0]["id"]
    else:
        type_id = netbox("POST", "/virtualization/cluster-types/",
                         json={"name": "Docker", "slug": "docker"}).json()["id"]
    return netbox("POST", chemin, json={"name": "docker-local", "type": type_id}).json()["id"]


def synchroniser(conteneur, supprimer=False):
    chemin = "/virtualization/virtual-machines/"
    cluster_id = cluster_netbox()
    machines = netbox("GET", chemin, params={"name": conteneur.name, "cluster_id": cluster_id}).json()["results"]
    if supprimer:
        for machine in machines:
            netbox("DELETE", f"{chemin}{machine['id']}/")
        return
    corps = {
        "name": conteneur.name, "cluster": cluster_id,
        "status": "active" if conteneur.state == "running" else "offline",
    }
    if machines:
        netbox("PATCH", f"{chemin}{machines[0]['id']}/", json=corps)
    else:
        netbox("POST", chemin, json=corps)


@app.get("/health", summary="Vérifie que l'API répond")
def sante():
    return {"status": "ok"}


@app.get("/containers", response_model=list[Conteneur], responses={401: ERREURS[401]},
         dependencies=[Depends(verifier_role)], summary="Liste les conteneurs gérés")
def lister():
    return lister_conteneurs()


@app.get("/containers/{identifiant}", response_model=Conteneur,
         responses={401: ERREURS[401], 404: ERREURS[404]},
         dependencies=[Depends(verifier_role)], summary="Détail d'un conteneur géré")
def lire(identifiant: str):
    return lire_conteneur(identifiant)


@app.post("/containers", status_code=201, response_model=Conteneur,
          responses={code: ERREURS[code] for code in (401, 403, 409)},
          dependencies=[Depends(verifier_admin)], summary="Crée et démarre un conteneur")
def creer(corps: ConteneurACreer):
    resultat = docker("POST", "/containers/create", params={"name": corps.name},
                      json={"Image": corps.image, "Labels": {ETIQUETTE: "true"}}).json()
    identifiant = resultat["Id"]
    docker("POST", f"/containers/{identifiant}/start")
    conteneur = lire_conteneur(identifiant)
    synchroniser(conteneur)
    return conteneur


def changer_etat(identifiant, action):
    conteneur = lire_conteneur(identifiant)
    if (action == "start" and conteneur.state == "running") or (
        action == "stop" and conteneur.state != "running"
    ):
        raise HTTPException(409, "Le conteneur est déjà dans cet état")
    docker("POST", f"/containers/{conteneur.id}/{action}", params={"t": 1})
    conteneur = lire_conteneur(conteneur.id)
    synchroniser(conteneur)
    return conteneur


@app.post("/containers/{identifiant}/stop", response_model=Conteneur,
          responses=ERREURS, dependencies=[Depends(verifier_admin)],
          summary="Arrête un conteneur géré")
def arreter(identifiant: str):
    return changer_etat(identifiant, "stop")


@app.post("/containers/{identifiant}/start", response_model=Conteneur,
          responses=ERREURS, dependencies=[Depends(verifier_admin)],
          summary="Démarre un conteneur géré")
def demarrer(identifiant: str):
    return changer_etat(identifiant, "start")


@app.delete("/containers/{identifiant}", status_code=204,
            responses={code: ERREURS[code] for code in (401, 403, 404)},
            dependencies=[Depends(verifier_admin)], summary="Supprime un conteneur géré")
def supprimer(identifiant: str):
    conteneur = lire_conteneur(identifiant)
    docker("DELETE", f"/containers/{conteneur.id}", params={"force": "true"})
    synchroniser(conteneur, supprimer=True)
    return Response(status_code=204)
