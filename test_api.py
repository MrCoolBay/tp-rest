import os
import unittest
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from main import app

ID = "a" * 64
ENV = {
    "DOCKER_API_URL": "http://docker.test:2375",
    "NETBOX_URL": "http://netbox.test:8080",
    "NETBOX_TOKEN": "netbox-test",
    "API_TOKEN_ADMIN": "admin-test",
    "API_TOKEN_READER": "lecteur-test",
    "WEBHOOK_SECRET": "webhook-test",
}


def reponse(corps=None, statut=200):
    return Mock(status_code=statut, ok=statut < 400, json=Mock(return_value=corps))


def detail(gere=True, etat="running"):
    return {
        "Id": ID, "Name": "/web1", "State": {"Status": etat},
        "Config": {"Image": "nginx:alpine", "Labels": {"adminapi.managed": "true"} if gere else {}},
    }


class TestsAPI(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app)
        self.client.headers["Authorization"] = "Bearer admin-test"
        self.requetes = patch("main.requests.request")
        self.http = self.requetes.start()
        self.addCleanup(self.requetes.stop)

    def test_sante_et_docs_sans_token(self):
        self.client.headers.clear()
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        for chemin in ("/docs", "/openapi.json"):
            self.assertEqual(self.client.get(chemin).status_code, 200)
        self.http.assert_not_called()

    def test_token_absent_inconnu_ou_mauvais_schema(self):
        for autorisation in ("", "Bearer faux", "Basic admin-test"):
            self.client.headers["Authorization"] = autorisation
            self.assertEqual(self.client.get("/containers").status_code, 401)
        self.http.assert_not_called()

    def test_lecture_lecteur(self):
        self.client.headers["Authorization"] = "Bearer lecteur-test"
        self.http.return_value = reponse(detail())
        resultat = self.client.get(f"/containers/{ID}")
        self.assertEqual(resultat.status_code, 200)
        self.assertEqual(resultat.json(), {
            "id": ID, "name": "web1", "image": "nginx:alpine", "state": "running",
        })

    def test_non_gere_invisible(self):
        self.http.return_value = reponse(detail(gere=False))
        self.assertEqual(self.client.get(f"/containers/{ID}").status_code, 404)

    def test_liste_filtree(self):
        self.http.return_value = reponse([
            {"Id": ID, "Names": ["/web1"], "Image": "nginx:alpine", "State": "running",
             "Labels": {"adminapi.managed": "true"}},
            {"Id": "b" * 64, "Labels": {}},
        ])
        resultat = self.client.get("/containers")
        self.assertEqual(len(resultat.json()), 1)
        self.assertIn("adminapi.managed=true", self.http.call_args.kwargs["params"]["filters"])

    def test_ecritures_interdites_au_lecteur(self):
        self.client.headers["Authorization"] = "Bearer lecteur-test"
        for methode, chemin in (("POST", "/containers"), ("DELETE", f"/containers/{ID}"),
                                ("POST", f"/containers/{ID}/start"), ("POST", f"/containers/{ID}/stop")):
            self.assertEqual(self.client.request(methode, chemin).status_code, 403)
        self.http.assert_not_called()

    def test_validation_creation(self):
        for corps in ({"name": "web1"}, {"name": "Web 1", "image": "nginx:alpine"},
                      {"name": "a", "image": "nginx:alpine"},
                      {"name": "web1", "image": "ubuntu:latest"},
                      {"name": "a" * 32, "image": "nginx:alpine"}):
            self.assertEqual(self.client.post("/containers", json=corps).status_code, 422)
        self.http.assert_not_called()

    @patch("main.synchroniser")
    def test_creation(self, sync):
        self.http.side_effect = [reponse({"Id": ID}, 201), reponse(statut=204), reponse(detail())]
        resultat = self.client.post("/containers", json={"name": "web1", "image": "nginx:alpine"})
        self.assertEqual(resultat.status_code, 201)
        self.assertEqual(resultat.json()["id"], ID)
        self.assertEqual(self.http.call_args_list[0].kwargs["json"]["Labels"], {"adminapi.managed": "true"})
        sync.assert_called_once()

    def test_nom_deja_pris(self):
        self.http.return_value = reponse(statut=409)
        self.assertEqual(self.client.post("/containers", json={"name": "web1", "image": "nginx:alpine"}).status_code, 409)
        self.assertEqual(self.http.call_count, 1)

    def test_ecritures_non_gere_interdites(self):
        self.http.return_value = reponse(detail(gere=False))
        for methode, chemin in (("DELETE", f"/containers/{ID}"),
                                ("POST", f"/containers/{ID}/start"), ("POST", f"/containers/{ID}/stop")):
            self.http.reset_mock()
            self.assertEqual(self.client.request(methode, chemin).status_code, 404)
            self.assertEqual(self.http.call_count, 1)
            self.assertEqual(self.http.call_args.args[0], "GET")

    def test_conflits_etat(self):
        for action, etat in (("start", "running"), ("stop", "exited")):
            self.http.return_value = reponse(detail(etat=etat))
            self.http.reset_mock()
            self.assertEqual(self.client.post(f"/containers/{ID}/{action}").status_code, 409)
            self.assertEqual(self.http.call_count, 1)

    @patch("main.synchroniser")
    def test_changements_etat(self, sync):
        for action, avant, apres in (("stop", "running", "exited"), ("start", "exited", "running")):
            self.http.side_effect = [reponse(detail(etat=avant)), reponse(statut=204), reponse(detail(etat=apres))]
            resultat = self.client.post(f"/containers/{ID}/{action}")
            self.assertEqual(resultat.status_code, 200)
            self.assertEqual(resultat.json()["state"], apres)
            self.assertEqual(sync.call_args.args[0].state, apres)

    @patch("main.synchroniser")
    def test_suppression_sans_corps(self, sync):
        self.http.side_effect = [reponse(detail()), reponse(statut=204)]
        resultat = self.client.delete(f"/containers/{ID}")
        self.assertEqual(resultat.status_code, 204)
        self.assertEqual(resultat.content, b"")
        self.assertTrue(sync.call_args.kwargs["supprimer"])
        self.assertEqual(self.http.call_args.kwargs["params"], {"force": "true"})

    def test_synchronisation_netbox(self):
        from main import Conteneur, synchroniser
        conteneur = Conteneur(id=ID, name="web1", image="nginx:alpine", state="running")
        self.http.side_effect = [reponse({"results": [{"id": 3}]}), reponse({"results": []}), reponse({"id": 9}, 201)]
        synchroniser(conteneur)
        self.assertEqual(self.http.call_args.kwargs["json"], {"name": "web1", "cluster": 3, "status": "active"})
        self.assertEqual(self.http.call_args.kwargs["headers"]["Authorization"], "Bearer netbox-test")
        conteneur.state = "exited"
        self.http.side_effect = [reponse({"results": [{"id": 3}]}), reponse({"results": [{"id": 9}]}), reponse()]
        synchroniser(conteneur)
        self.assertEqual(self.http.call_args.args[:2], ("PATCH", "http://netbox.test:8080/api/virtualization/virtual-machines/9/"))
        self.assertEqual(self.http.call_args.kwargs["json"]["status"], "offline")

    def test_netbox_indisponible(self):
        from main import netbox
        from fastapi import HTTPException
        self.http.side_effect = requests.Timeout()
        with self.assertRaises(HTTPException) as erreur:
            netbox("GET", "/status/")
        self.assertEqual(erreur.exception.status_code, 502)

    def test_docker_indisponible(self):
        self.http.side_effect = requests.ConnectionError()
        self.assertEqual(self.client.get("/containers").status_code, 502)

    def test_configuration_absente(self):
        with patch.dict(os.environ, {"DOCKER_API_URL": ""}):
            self.assertEqual(self.client.get("/containers").status_code, 503)
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
