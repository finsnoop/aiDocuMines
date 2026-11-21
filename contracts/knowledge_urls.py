"""Routes for /api/v1/knowledge/ (contracts/knowledge.py). Switchable per organisation ("Playbooks & benchmarks")."""
from django.urls import path

from . import knowledge as k

urlpatterns = [
    path("playbooks/", k.PlaybooksView.as_view(), name="knowledge-playbooks"),
    path("playbooks/upload/", k.PlaybookUploadView.as_view(), name="knowledge-playbook-upload"),
    path("playbooks/<str:pk>/", k.PlaybookView.as_view(), name="knowledge-playbook"),
    path("playbooks/<str:pk>/export/", k.PlaybookExportView.as_view(), name="knowledge-playbook-export"),
    path("playbooks/<str:pk>/file/", k.PlaybookFileView.as_view(), name="knowledge-playbook-file"),
    path("playbooks/<str:pk>/positions/", k.PositionsView.as_view(), name="knowledge-positions"),
    path("playbooks/<str:pk>/positions/<int:position_id>/", k.PositionsView.as_view(), name="knowledge-position"),
    path("playbooks/<str:pk>/benchmarks/", k.PlaybookBenchmarksView.as_view(), name="knowledge-playbook-benchmarks"),
    path("tags/", k.TagsView.as_view(), name="knowledge-tags"),
    path("benchmarks/", k.BenchmarksView.as_view(), name="knowledge-benchmarks"),
    path("benchmarks/from-document/", k.SetAsBenchmarkView.as_view(), name="knowledge-set-as-benchmark"),
    path("benchmarks/tree/", k.BenchmarkTreeView.as_view(), name="knowledge-benchmark-tree"),
    path("categories/", k.CategoriesView.as_view(), name="knowledge-categories"),
    path("categories/<str:pk>/", k.CategoryView.as_view(), name="knowledge-category"),
    path("benchmarks/<str:pk>/", k.BenchmarkView.as_view(), name="knowledge-benchmark"),
    path("benchmarks/<str:pk>/terms/", k.TermsView.as_view(), name="knowledge-terms"),
    path("benchmarks/<str:pk>/terms/<int:term_id>/", k.TermsView.as_view(), name="knowledge-term"),
    path("benchmarks/<str:pk>/suggest-terms/", k.SuggestTermsView.as_view(), name="knowledge-suggest-terms"),
    path("links/", k.LinksView.as_view(), name="knowledge-links"),
    path("links/<str:pk>/", k.LinkView.as_view(), name="knowledge-link"),
    path("applicable/", k.ApplicableView.as_view(), name="knowledge-applicable"),
    path("analyses/", k.AnalysesView.as_view(), name="knowledge-analyses"),
    path("analyses/matter/", k.MatterAnalysesView.as_view(), name="knowledge-matter-analyses"),
    path("analyses/<str:pk>/", k.AnalysisView.as_view(), name="knowledge-analysis"),
]
