# file_system/urls.py

from django.urls import path
from .views import UserFileTreeView

urlpatterns = [
    # File Tree Structure or Async Generation
    path("users/<int:user_id>/file-tree/", UserFileTreeView.as_view(), name="user-file-tree"),
]

