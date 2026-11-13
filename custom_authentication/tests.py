from django.test import TestCase
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from oauth2_provider.models import Application
from rest_framework.test import APIClient
from .models import Client

User = get_user_model()


class UserCreationTests(TestCase):
    """Tests for admin user creation endpoints."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="TestOrg")
        self.admin_user = User.objects.create_user(
            email="admin@testorg.com",
            password="TestAdmin#123",
            client=self.client_obj,
            is_staff=True,
            is_superuser=True,
        )
        admin_group, _ = Group.objects.get_or_create(name="Admin")
        self.admin_user.groups.add(admin_group)

    def test_create_user_without_admin_fails(self):
        """Non-admin users should not be able to create users."""
        normal_user = User.objects.create_user(
            email="normal@testorg.com",
            password="NormalUser#123",
            client=self.client_obj,
        )
        guest_group, _ = Group.objects.get_or_create(name="Guest")
        normal_user.groups.add(guest_group)

        c = APIClient()
        c.force_authenticate(normal_user)
        response = c.post(
            "/api/v1/auth/admin/users/create/",
            {"email": "newuser@testorg.com", "contact_name": "New User", "contact_phone": "+1234567890", "organisation": "TestOrg"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_create_user_missing_fields(self):
        """Creating user with missing required fields should fail."""
        c = APIClient()
        c.force_authenticate(self.admin_user)

        response = c.post(
            "/api/v1/auth/admin/users/create/",
            {"email": "incomplete@testorg.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_create_user_duplicate_email_fails(self):
        """Creating user with existing email should fail."""
        c = APIClient()
        c.force_authenticate(self.admin_user)

        response = c.post(
            "/api/v1/auth/admin/users/create/",
            {"email": "admin@testorg.com", "contact_name": "Duplicate", "contact_phone": "+1234567890", "organisation": "TestOrg"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class MultiTenancyTests(TestCase):
    """Tests for multi-tenancy restrictions in user management."""

    def setUp(self):
        self.client_org_a = Client.objects.create(name="OrgA")
        self.client_org_b = Client.objects.create(name="OrgB")
        self.admin_a = User.objects.create_user(
            email="admin@orga.com",
            password="AdminA#123",
            client=self.client_org_a,
            is_staff=True,
        )
        admin_group, _ = Group.objects.get_or_create(name="Admin")
        self.admin_a.groups.add(admin_group)

    def test_admin_cannot_create_user_in_other_organisation(self):
        """Admin should only be able to create users within their own organisation."""
        c = APIClient()
        c.force_authenticate(self.admin_a)

        response = c.post(
            "/api/v1/auth/admin/users/create/",
            {"email": "user@orgb.com", "contact_name": "User B", "contact_phone": "+1234567890", "organisation": "OrgB"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)


class ListUsersTests(TestCase):
    """Tests for listing users."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="TestOrg")
        self.superuser = User.objects.create_user(
            email="super@testorg.com",
            password="Super#123",
            client=self.client_obj,
            is_superuser=True,
        )

    def test_list_users_without_auth_fails(self):
        """Listing users without authentication should fail."""
        from django.test import Client as TestClient
        c = TestClient()
        response = c.get("/api/v1/auth/admin/users/")
        self.assertEqual(response.status_code, 401)

    def test_list_users_requires_admin(self):
        """Listing users requires admin privileges."""
        from django.test import Client as TestClient
        c = TestClient()
        normal_user = User.objects.create_user(
            email="normal@testorg.com",
            password="Normal#123",
            client=self.client_obj,
        )

        response = c.get("/api/v1/auth/admin/users/")
        self.assertEqual(response.status_code, 401)


class SignupTests(TestCase):
    """Tests for user signup endpoint."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="SignupOrg")

    def test_signup_creates_client(self):
        """Signup should create client if it doesn't exist."""
        from django.test import Client as TestClient
        c = TestClient()
        response = c.post(
            "/api/v1/auth/signup/",
            {
                "email": "newuser@signuporg.com",
                "password": "Password#123",
                "organisation": "SignupOrg",
                "contact_name": "New User",
                "contact_phone": "+1234567890",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(User.objects.filter(email="newuser@signuporg.com").exists())

    def test_signup_duplicate_email_fails(self):
        """Signup with duplicate email should fail."""
        from django.test import Client as TestClient
        c = TestClient()
        User.objects.create_user(
            email="existing@signuporg.com",
            password="Password#123",
            client=self.client_obj,
        )
        response = c.post(
            "/api/v1/auth/signup/",
            {
                "email": "existing@signuporg.com",
                "password": "Password#123",
                "organisation": "SignupOrg",
                "contact_name": "New User",
                "contact_phone": "+1234567890",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class UserDisableEnableTests(TestCase):
    """Tests for user disable/enable endpoints."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="TestOrg")
        self.superuser = User.objects.create_user(
            email="super@testorg.com",
            password="Super#123",
            client=self.client_obj,
            is_superuser=True,
        )
        self.target_user = User.objects.create_user(
            email="target@testorg.com",
            password="Target#123",
            client=self.client_obj,
            is_staff=True,
        )

    def test_disable_user_without_auth_fails(self):
        """Disabling user without auth should fail."""
        c = APIClient()
        response = c.post(f"/api/v1/auth/admin/users/{self.target_user.id}/disable/", {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_enable_user_without_auth_fails(self):
        """Enabling user without auth should fail."""
        c = APIClient()
        response = c.post(f"/api/v1/auth/admin/users/{self.target_user.id}/enable/", {}, format="json")
        self.assertEqual(response.status_code, 401)


class UserActivityLogTests(TestCase):
    """Tests for user activity logging."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="TestOrg")
        self.superuser = User.objects.create_user(
            email="super@testorg.com",
            password="Super#123",
            client=self.client_obj,
            is_superuser=True,
        )
        self.target_user = User.objects.create_user(
            email="target@testorg.com",
            password="Target#123",
            client=self.client_obj,
        )

    def test_activity_log_without_auth_fails(self):
        """Getting activity log without auth should fail."""
        from django.test import Client as TestClient
        c = TestClient()
        response = c.get(f"/api/v1/auth/admin/users/{self.target_user.id}/activity/")
        self.assertEqual(response.status_code, 401)