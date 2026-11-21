"""Single sign-on (OIDC), SCIM provisioning, enforced SSO and mandatory two-step sign-in.

The identity provider is mocked, but ID tokens are real RS256 JWTs, so signature, issuer, audience, expiry
and nonce checks run for real. Nothing may sign anyone in to another organisation."""
import base64
import hashlib
import time
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.cache import cache
from django.test import TestCase, override_settings
from oauth2_provider.models import AccessToken, Grant
from rest_framework.test import APIClient

from core.tests.test_tenancy import make_user
from custom_authentication.models import Client, CustomUser, OrganisationPolicy, OrganisationSSO
from custom_authentication.scim import hash_token

ISSUER = "https://idp.firm.test"
SPA = "https://app.test/sign_in"
META = {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/keys"}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(b"browser-verifier-" * 4).digest()).rstrip(b"=").decode()


def id_token(email, nonce, aud="app-123", iss=ISSUER, key=KEY, **extra):
    now = int(time.time())
    return jwt.encode({"iss": iss, "aud": aud, "sub": email, "email": email, "nonce": nonce, "iat": now,
                       "exp": now + 300, **extra}, key, algorithm="RS256")


@override_settings(SPA_REDIRECT_URIS=[SPA])
class SSOTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="SSO firm")
        cls.other = Client.objects.create(name="Other SSO firm")
        cls.lawyer = make_user("lawyer@firm.test", cls.firm, groups=["Client"])
        cls.admin = make_user("admin@firm.test", cls.firm, groups=["Admin"])
        cls.stranger = make_user("intruder@firm.test", cls.other, groups=["Client"])   # same domain, other organisation
        cls.lawyer.set_password("pw-Test-12345"); cls.lawyer.save()
        cls.admin.set_password("pw-Test-12345"); cls.admin.save()

    def setUp(self):
        cache.clear()
        self.cfg = OrganisationSSO.objects.create(client=self.firm, enabled=True, issuer_url=ISSUER, oidc_client_id="app-123",
                                                  client_secret="s3cret", email_domains="firm.test")
        get = mock.patch("custom_authentication.sso.requests.get",
                         return_value=SimpleNamespace(json=lambda: META, raise_for_status=lambda: None))
        get.start()
        self.addCleanup(get.stop)
        keys = mock.patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=SimpleNamespace(key=KEY.public_key()))
        keys.start()
        self.addCleanup(keys.stop)

    def start(self, email="lawyer@firm.test"):
        r = APIClient().post("/api/v1/auth/sso/start/", {"email": email, "code_challenge": CHALLENGE,
                                                        "code_challenge_method": "S256", "redirect_uri": SPA}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        q = parse_qs(urlparse(r.json()["authorize_url"]).query)
        self.assertEqual(q["client_id"], ["app-123"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        return q["state"][0], q["nonce"][0]

    def callback(self, state, token, status=200):
        response = SimpleNamespace(status_code=status, json=lambda: {"id_token": token})
        with mock.patch("custom_authentication.sso.requests.post", return_value=response) as post:
            r = APIClient().get(f"/api/v1/auth/sso/callback/?code=idp-code&state={state}")
        self.assertEqual(r.status_code, 302)
        return parse_qs(urlparse(r["Location"]).query), post

    def test_discover(self):
        c = APIClient()
        self.assertTrue(c.post("/api/v1/auth/sso/discover/", {"email": "x@firm.test"}, format="json").json()["sso"])
        self.assertFalse(c.post("/api/v1/auth/sso/discover/", {"email": "x@else.test"}, format="json").json()["sso"])

    def test_successful_sign_in_issues_a_pkce_bound_code(self):
        state, nonce = self.start()
        q, post = self.callback(state, id_token("lawyer@firm.test", nonce))
        self.assertIn("code", q, q)
        grant = Grant.objects.get(code=q["code"][0])
        self.assertEqual((grant.user, grant.code_challenge), (self.lawyer, CHALLENGE))
        self.assertIn("code_verifier", post.call_args.kwargs["data"])          # our leg uses PKCE too
        # The state is single use.
        q2, _ = self.callback(state, id_token("lawyer@firm.test", nonce))
        self.assertIn("expired", q2["error"][0])

    def test_never_signs_in_to_another_organisation(self):
        state, nonce = self.start("intruder@firm.test")
        q, _ = self.callback(state, id_token("intruder@firm.test", nonce))
        self.assertIn("different organisation", q["error"][0])
        self.assertFalse(Grant.objects.filter(user=self.stranger).exists())

    def test_forged_or_mismatched_tokens_are_refused(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for bad in (lambda n: id_token("lawyer@firm.test", "wrong-nonce"),
                    lambda n: id_token("lawyer@firm.test", n, aud="someone-else"),
                    lambda n: id_token("lawyer@firm.test", n, iss="https://evil.test"),
                    lambda n: id_token("lawyer@firm.test", n, key=other_key),
                    lambda n: id_token("lawyer@firm.test", n, email_verified=False),
                    lambda n: id_token("someone@other-domain.test", n)):
            state, nonce = self.start()
            q, _ = self.callback(state, bad(nonce))
            self.assertIn("error", q)
        self.assertFalse(Grant.objects.exists())

    def test_auto_provision_creates_a_standard_account_in_the_organisation(self):
        state, nonce = self.start("newcomer@firm.test")
        q, _ = self.callback(state, id_token("newcomer@firm.test", nonce))
        self.assertIn("no account", q["error"][0].lower().replace("do not have an account", "no account"))
        self.cfg.auto_provision, self.cfg.default_role = True, "Client"
        self.cfg.save()
        state, nonce = self.start("newcomer@firm.test")
        q, _ = self.callback(state, id_token("newcomer@firm.test", nonce))
        user = CustomUser.objects.get(email="newcomer@firm.test")
        self.assertEqual(user.client, self.firm)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["Client"])

    def password(self, email):
        return APIClient().post("/api/v1/auth/browser/authorize/", {
            "email": email, "password": "pw-Test-12345", "code_challenge": CHALLENGE, "code_challenge_method": "S256",
            "redirect_uri": SPA}, format="json")

    @mock.patch("custom_authentication.browser_auth.send_signin_code_task.delay")
    def test_enforced_sso_refuses_passwords_but_admins_keep_a_coded_route(self, _send):
        self.assertEqual(self.password("lawyer@firm.test").status_code, 200)
        self.cfg.enforce_sso = True
        self.cfg.save()
        r = self.password("lawyer@firm.test")
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.json()["sso"])
        r = self.password("admin@firm.test")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["requires_2fa"])

    @mock.patch("custom_authentication.browser_auth.send_signin_code_task.delay")
    def test_organisation_can_require_two_step_sign_in(self, send):
        self.assertIn("code", self.password("lawyer@firm.test").json())
        OrganisationPolicy.objects.update_or_create(client=self.firm, defaults={"require_2fa": True})
        r = self.password("lawyer@firm.test")
        self.assertTrue(r.json()["requires_2fa"])
        send.assert_called()

    def test_admin_settings(self):
        api = APIClient()
        api.force_authenticate(self.lawyer)
        self.assertEqual(api.get("/api/v1/auth/admin/sso/").status_code, 403)
        api.force_authenticate(self.admin)
        body = api.get("/api/v1/auth/admin/sso/").json()
        self.assertTrue(body["client_secret_configured"])
        self.assertNotIn("s3cret", str(body))
        r = api.patch("/api/v1/auth/admin/sso/", {"default_role": "Admin"}, format="json")
        self.assertEqual(r.status_code, 400)                               # never auto-create administrators
        OrganisationSSO.objects.create(client=self.other, email_domains="other.test")
        r = api.patch("/api/v1/auth/admin/sso/", {"email_domains": "firm.test,other.test"}, format="json")
        self.assertEqual(r.status_code, 400)                               # a domain belongs to one organisation
        r = api.patch("/api/v1/auth/admin/sso/", {"issuer_url": "http://insecure.test"}, format="json")
        self.assertEqual(r.status_code, 400)


class SCIMTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="SCIM firm")
        cls.other = Client.objects.create(name="Other SCIM firm")
        cls.admin = make_user("admin@scim.test", cls.firm, groups=["Admin"])
        cls.member = make_user("member@scim.test", cls.firm, groups=["Client"])
        cls.foreign = make_user("foreign@otherscim.test", cls.other, groups=["Client"])

    def setUp(self):
        cache.clear()
        api = APIClient()
        api.force_authenticate(self.admin)
        self.token = api.post("/api/v1/auth/admin/sso/scim-token/").json()["token"]
        OrganisationSSO.objects.filter(client=self.firm).update(email_domains="scim.test")

    def scim(self, token=None):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {token or self.token}")
        return c

    def test_token_is_required_and_stored_hashed(self):
        self.assertEqual(APIClient().get("/api/v1/scim/v2/Users").status_code, 401)
        self.assertEqual(self.scim("scim_wrong-token-value-0000000000000").get("/api/v1/scim/v2/Users").status_code, 401)
        self.assertEqual(OrganisationSSO.objects.get(client=self.firm).scim_token_hash, hash_token(self.token))

    def test_lists_only_its_own_organisation(self):
        r = self.scim().get("/api/v1/scim/v2/Users")
        emails = {u["userName"] for u in r.json()["Resources"]}
        self.assertEqual(emails, {"admin@scim.test", "member@scim.test"})
        r = self.scim().get('/api/v1/scim/v2/Users?filter=userName eq "foreign@otherscim.test"')
        self.assertEqual(r.json()["totalResults"], 0)
        self.assertEqual(self.scim().get(f"/api/v1/scim/v2/Users/{self.foreign.pk}").status_code, 404)
        self.assertEqual(self.scim().delete(f"/api/v1/scim/v2/Users/{self.foreign.pk}").status_code, 404)
        self.foreign.refresh_from_db()
        self.assertTrue(self.foreign.is_active)

    def test_create_update_and_deprovision(self):
        r = self.scim().post("/api/v1/scim/v2/Users", {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                             "userName": "new@scim.test", "name": {"givenName": "Nia", "familyName": "Ng"}, "active": True},
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        uid = r.json()["id"]
        user = CustomUser.objects.get(pk=uid)
        self.assertEqual((user.client, user.first_name), (self.firm, "Nia"))
        self.assertEqual(self.scim().post("/api/v1/scim/v2/Users", {"userName": "new@scim.test"}, format="json").status_code, 409)
        self.assertEqual(self.scim().post("/api/v1/scim/v2/Users", {"userName": "x@elsewhere.test"}, format="json").status_code, 400)
        # Deactivation disables the account and ends its sessions at once.
        from oauth2_provider.models import Application
        app = Application.objects.create(user=user, client_type="confidential", authorization_grant_type="password", name="t")
        AccessToken.objects.create(user=user, application=app, token="tok-123", expires="2099-01-01T00:00:00Z", scope="read")
        r = self.scim().patch(f"/api/v1/scim/v2/Users/{uid}", {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                              "Operations": [{"op": "replace", "path": "active", "value": False}]}, format="json")
        self.assertEqual(r.status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(AccessToken.objects.filter(user=user).exists())
        self.assertEqual(self.scim().delete(f"/api/v1/scim/v2/Users/{self.member.pk}").status_code, 204)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertTrue(CustomUser.objects.filter(pk=self.member.pk).exists())   # never erased
