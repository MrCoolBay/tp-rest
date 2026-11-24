import json
import os
import secrets
from typing import Annotated
from urllib.parse import quote

import requests
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

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
