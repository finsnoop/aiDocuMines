"""SCIM 2.0 provisioning routes (see scim.py)."""
from django.urls import path

from .scim import ServiceProviderConfigView, UserDetailView, UsersView

urlpatterns = [
    path("ServiceProviderConfig", ServiceProviderConfigView.as_view(), name="scim_spc"),
    path("Users", UsersView.as_view(), name="scim_users"),
    path("Users/<str:pk>", UserDetailView.as_view(), name="scim_user"),
]
