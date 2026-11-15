"""
Load test: people using aiDocuMines through the browser (data from `manage.py seed_scale`).

Each simulated person is a real seeded user (bearer token) from one of the organisations and moves
through the app the way the frontend does, pausing between clicks. AI chat answers are not included:
their speed is the AI provider's, not the platform's.

    TOKENS=tokens.json locust -f locustfile.py --host http://scale-web:8020 --headless \
        -u 300 -r 10 -t 5m --csv out/run
"""
import json
import os
import random

from locust import FastHttpUser, between, task

USERS = json.load(open(os.environ.get("TOKENS", "tokens.json"), encoding="utf-8"))


class Person(FastHttpUser):
    wait_time = between(2, 8)          # thinking time between clicks

    def on_start(self):
        me = random.choice(USERS)
        self.headers = {"Authorization": f"Bearer {me['token']}", "Accept": "application/json"}
        # What the app loads when someone opens it
        for path in ("/api/v1/auth/whoami/", "/api/v1/auth/user/profile/", "/api/v1/system-settings/verticals/"):
            self.client.get(path, headers=self.headers, name="open app: " + path)

    def get(self, path, name=None):
        self.client.get(path, headers=self.headers, name=name or path)

    @task(3)
    def home(self):
        self.get("/api/v1/home-analytics/business/")

    @task(6)
    def documents_table(self):
        page = random.choice([1, 1, 1, 2, 3])
        self.get(f"/api/v1/documents/table/?page={page}", name="/api/v1/documents/table/?page=N")

    @task(2)
    def documents_search(self):
        word = random.choice(["Agreement", "Invoice", "Lease", "Policy", "Report"])
        self.get(f"/api/v1/documents/table/?q={word}", name="/api/v1/documents/table/?q=WORD")

    @task(4)
    def folder_tree(self):
        self.get("/api/v1/documents/folders/")

    @task(1)
    def shared_with_me(self):
        self.get("/api/v1/documents/files/shared/")

    @task(2)
    def matters(self):
        self.get("/api/v1/core/projects/")

    @task(1)
    def workflows(self):
        self.get("/api/v1/core/bulk/catalog/")

    @task(1)
    def insights(self):
        self.get("/api/v1/service-analytics/overview/")
        self.get("/api/v1/service-analytics/recent-activity/")

    @task(1)
    def assistant_history(self):
        self.get("/api/v1/grid-documents-interrogation/topics/")
