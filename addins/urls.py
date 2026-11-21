from django.urls import path

from . import views

urlpatterns = [
    path("outlook/suggest/", views.OutlookSuggestView.as_view(), name="addin-outlook-suggest"),
    path("outlook/file/", views.OutlookFileView.as_view(), name="addin-outlook-file"),
    path("word/save/", views.WordSaveView.as_view(), name="addin-word-save"),
    path("word/clauses/", views.WordClausesView.as_view(), name="addin-word-clauses"),
]
