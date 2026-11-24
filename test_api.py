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

    def test_docker_indisponible(self):
        self.http.side_effect = requests.ConnectionError()
        self.assertEqual(self.client.get("/containers").status_code, 502)

    def test_configuration_absente(self):
        with patch.dict(os.environ, {"DOCKER_API_URL": ""}):
            self.assertEqual(self.client.get("/containers").status_code, 503)
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
