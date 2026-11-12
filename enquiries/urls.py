from django.urls import path

from .views import EnquiryCreateView

urlpatterns = [
    path("", EnquiryCreateView.as_view(), name="enquiry-create"),
]
