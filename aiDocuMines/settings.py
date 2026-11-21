import os
from pathlib import Path
from dotenv import load_dotenv
from corsheaders.defaults import default_headers

# Load environment variables
# A local demo stack (LOCAL_AUDIT, set in .env.audit) must never pick up a developer's copy of the
# production .env from the project folder: real mail, payment and storage credentials would go live.
_LOAD_DOTENV = not os.getenv("LOCAL_AUDIT")
if _LOAD_DOTENV:
    load_dotenv()

os.environ.setdefault("ELASTIC_CLIENT_APIVERSIONING", "true")

BASE_DIR = Path(__file__).resolve().parent.parent

MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
MEDIA_URL = '/media/'

LOGS_DIR = os.path.join(BASE_DIR, 'logs')
if not os.path.exists(LOGS_DIR):
    os.makedirs(LOGS_DIR)

FIELD_ENCRYPTION_KEY = os.getenv("FIELD_ENCRYPTION_KEY")


# Explicitly tell dotenv where to find the file
if _LOAD_DOTENV:
    load_dotenv(dotenv_path=os.path.join(BASE_DIR, '.env'))

# SECURITY: debug is OFF unless a developer turns it on, and a real deployment refuses to
# start with a missing or placeholder secret key (sessions, signed download links and
# password-reset signatures are only as secret as this value).
DEBUG = os.getenv("DEBUG", "False").strip().lower() in ("1", "true", "yes")
SECRET_KEY = os.getenv("SECRET_KEY", "")
_INSECURE_KEYS = {"", "django-insecure-default", "changeme", "change-me", "secret"}
if SECRET_KEY in _INSECURE_KEYS:
    if DEBUG:
        SECRET_KEY = "django-insecure-development-only"
    else:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured(
            "SECRET_KEY is not set. Put a long random value in the server's .env "
            "(python -c \"import secrets; print(secrets.token_urlsafe(64))\")."
        )
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "aidocumines.com,41.76.109.131,127.0.0.1,localhost,0.0.0.0").split(",")

AUTH_USER_MODEL = "custom_authentication.CustomUser"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework.authtoken",
    "django_extensions",
    "drf_yasg",
    "django_celery_beat",
    "oauth2_provider",
    "corsheaders",
    "core.apps.CoreConfig",
    "insights_hub",
    "file_monitor",
    "document_ocr",
    "document_anonymizer",
    "document_translation",
    "custom_authentication",
    "grid_documents_interrogation",
    "document_operations",
    "file_system",
    "system_settings",
    "document_search",
    "document_structures",
    "file_elasticsearch",
    "platform_data_insights",
    "integrations",
    "cost_centre",
    "home_dash_analytics",
    "email_management",
    "private_equity",
    "class_actions",
    "labor_employment",
    "ip_litigation",
    "regulatory_compliance",
    "service_analytics",
    "enquiries",
    "document_classification",

    # New services - May 2025
    "document_redlining",
    "document_workflows",
    "document_automation",
    "document_versioning",
    'contracts',
    'addins',
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "custom_authentication.middleware.APICallLoggingMiddleware",
    "custom_authentication.middleware.RBACMiddleware",
    "oauth2_provider.middleware.OAuth2TokenMiddleware",
    "home_dash_analytics.agents.usage.AIActorMiddleware",   # AI usage is recorded against the signed-in user
    "system_settings.middleware.VerticalAccessMiddleware",  # switched-off industry verticals refuse requests
    "custom_authentication.middleware.ClientIdentityBindingMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "aiDocuMines.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, 'templates')],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "aiDocuMines.wsgi.application"

ONLYOFFICE = {
    "DOC_SERVER_URL": os.getenv("ONLYOFFICE_URL", "https://onlyoffice.aidocumines.com").rstrip("/"),
    "DOC_SERVER_PUBLIC_URL": os.getenv("ONLYOFFICE_PUBLIC_URL", "https://onlyoffice.aidocumines.com").rstrip("/"),
    "JWT_SECRET": os.getenv("ONLYOFFICE_JWT_SECRET", ""),
    "DOWNLOAD_TTL": int(os.getenv("ONLYOFFICE_DOWNLOAD_TTL", "300")),
    "HTTP_CONNECT_TIMEOUT": int(os.getenv("ONLYOFFICE_HTTP_CONNECT_TIMEOUT", "5")),
    "HTTP_READ_TIMEOUT": int(os.getenv("ONLYOFFICE_HTTP_READ_TIMEOUT", "240")),
}


# Internal API base used in signed URLs that DS calls back to
API_BASE_URL = os.getenv("API_BASE_URL", "http://aidocumines_web").rstrip("/")



OAUTH2_PROVIDER_APPLICATION_MODEL = 'oauth2_provider.Application'
OAUTH2_PROVIDER_ID_TOKEN_MODEL = 'oauth2_provider.IDToken'
OAUTH2_PROVIDER_ACCESS_TOKEN_MODEL = 'oauth2_provider.AccessToken'
OAUTH2_PROVIDER_REFRESH_TOKEN_MODEL = 'oauth2_provider.RefreshToken'


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "aidocumines_api_layer_database"),
        "USER": os.getenv("POSTGRES_USER", "aidocumines_administrator"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),  # no built-in password: it comes from the environment
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        # Reuse connections, but check them first so a restarted database does not leave
        # workers holding dead connections; give up on an unreachable database quickly.
        # Close the connection after each request by default. Reuse (seconds) is only safe where
        # threads are pooled (gunicorn gthread, set in docker-compose.yml for the web service).
        # Under `manage.py runserver` every request is a new thread and reused connections pile up
        # until PostgreSQL refuses everyone ("too many clients"), sign-in included.
        "CONN_MAX_AGE": int(os.getenv("POSTGRES_CONN_MAX_AGE", "0")),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"connect_timeout": int(os.getenv("POSTGRES_CONNECT_TIMEOUT", "5"))},
    }
}

# Existing development databases can explicitly use POSTGRES_SSLMODE=prefer.
DATABASES["default"]["OPTIONS"]["sslmode"] = os.getenv("POSTGRES_SSLMODE", "require")
if os.getenv("POSTGRES_SSLROOTCERT"):
    DATABASES["default"]["OPTIONS"]["sslrootcert"] = os.environ["POSTGRES_SSLROOTCERT"]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "oauth2_provider.backends.OAuth2Backend",
]

OAUTH2_PROVIDER = {
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
    "SCOPES": {
        "openid": "OpenID Connect scope",
        "profile": "Access your profile info",
        "email": "Access your email",
        "read": "Read Scope", 
        "write": "Write Scope"},
    "TOKEN_MODEL": "oauth2_provider.models.AccessToken",
    "OIDC_ENABLED": True,
    "OIDC_RSA_PRIVATE_KEY": Path(os.getenv("OIDC_RSA_PRIVATE_KEY_PATH", str(BASE_DIR / "oidc-private-key.pem"))).read_text(),
    "RESOURCE_SERVER_INTROSPECTION_URL": "https://aidocumines-api.aidocumines.com/o/introspect/",
    "RESOURCE_SERVER_INTROSPECTION_TOKEN": os.getenv("INTROSPECTION_TOKEN", ""),
}


REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "custom_authentication.authentication.ClientBoundOAuth2Authentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": [
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    # Per-IP limits for sign-in, sign-up, password reset and token refresh ("auth"), and for
    # any other endpoint that opts into the standard anonymous/user throttles.
    "DEFAULT_THROTTLE_RATES": {
        "auth": os.getenv("THROTTLE_AUTH", "20/min"),              # per account (address + email)
        "auth_network": os.getenv("THROTTLE_AUTH_NETWORK", "600/min"),  # per address: an office of hundreds
        "anon": os.getenv("THROTTLE_ANON", "120/min"),
        "user": os.getenv("THROTTLE_USER", "1200/min"),
    },
    # Behind the platform's reverse proxy the caller's address is the last forwarded hop.
    "NUM_PROXIES": int(os.getenv("NUM_PROXIES", "1")),
}
if not DEBUG:
    # The browsable API is a development tool; production answers JSON only.
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = ["rest_framework.renderers.JSONRenderer"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
AUTH_MAX_FAILED_LOGINS = int(os.getenv("AUTH_MAX_FAILED_LOGINS", "10"))   # per account
AUTH_LOCKOUT_SECONDS = int(os.getenv("AUTH_LOCKOUT_SECONDS", "900"))

# API documentation (Swagger / ReDoc) describes every endpoint; it is public only when asked for.
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "true" if DEBUG else "false").strip().lower() in ("1", "true", "yes")


'''
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "oauth2_provider.contrib.rest_framework.OAuth2Authentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}
'''

SWAGGER_SETTINGS = {
    "USE_SESSION_AUTH": False,
    "SECURITY_DEFINITIONS": {
        "oauth2": {
            "type": "oauth2",
            "tokenUrl": "http://aidocumines.com:8020/o/token/",
            "flow": "application",
            "scopes": {"read": "Read Scope", "write": "Write Scope"},
        }
    },
    "SECURITY": [{"oauth2": ["read", "write"]}],
}

# Elasticsearch DSL config (modern elasticsearch-dsl)
ELASTICSEARCH_DSL = {
    "default": {
        "hosts": os.getenv("ELASTICSEARCH_URL", "http://aidocumines_elasticsearch:9200"),
        "http_auth": (os.getenv("ELASTICSEARCH_USER", "elastic"), os.getenv("ELASTICSEARCH_PASSWORD", "")),
        # Fail fast and retry instead of hanging requests when Elasticsearch is busy.
        "timeout": int(os.getenv("ELASTICSEARCH_TIMEOUT", "10")),
        "retry_on_timeout": True,
        "max_retries": 3,
    }
}


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        # Console Handler for standard logs
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
        },
        # File Handler for Celery logs (optional)
        "file": {
            "level": "INFO",
            "class": "logging.FileHandler",
            "filename": "celery_logs.log",  # specify the file where Celery logs will be written
        },
    },
    "loggers": {
        # Default Django logger
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": True,
        },
        # Celery logger
        "celery": {
            "handlers": ["console", "file"],  # Celery logs go to both console and file
            "level": "INFO",  # Celery logs at INFO level or higher
            "propagate": False,  # Prevent logs from propagating to the root logger
        },
        # Other loggers, e.g., for your application
        "myapp": {
            "handlers": ["console"],
            "level": "DEBUG",  # Or set to INFO depending on your app's needs
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}

NEXTCLOUD_ADMIN_PASS = os.getenv('NEXTCLOUD_ADMIN_PASS', 'default_admin_password')

# caching backed by the same Redis you already run for Celery
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.getenv("REDIS_URL", "redis://redis:6379/1"),  # use DB-1
        "TIMEOUT": 60 * 60 * 6,      # 6-hour TTL – tune to taste
        "KEY_PREFIX": "ds",          # document-search
        # A slow or dead Redis must fail a cache call in seconds, not hang the request.
        "OPTIONS": {
            "socket_connect_timeout": float(os.getenv("REDIS_CONNECT_TIMEOUT", "2")),
            "socket_timeout": float(os.getenv("REDIS_SOCKET_TIMEOUT", "3")),
            "health_check_interval": 30,
        },
    }
}

OIDC_ISS_ENDPOINT = "https://aidocumines-api-layer.aidocumines.com/o"

CORS_ALLOWED_ORIGINS = [
    "http://aidocumines.com",
    "https://aidocumines.com",
    "https://aidocumines.aidocumines.com",
    "http://41.76.109.131:8020",
    "http://41.76.109.131:4200",
    "http://localhost:4200",
    "http://127.0.0.1:3000",
    "https://aidocumines-frontend.aidocumines.com",
    "https://ai-docu-mines-frontend.vercel.app",
    "https://aidocumines.apps.datasqan.com",
    "https://aidocumines-api-layer.apps.datasqan.com",
    "https://aidocumines-backend.apps.datasqan.com",
]
# Per-environment additions, e.g. CORS_EXTRA_ORIGINS="http://localhost:4300,https://staging.example.com"
CORS_ALLOWED_ORIGINS += [o.strip() for o in os.getenv("CORS_EXTRA_ORIGINS", "").split(",") if o.strip()]

SESSION_COOKIE_SECURE = True  # Optional but recommended
CSRF_COOKIE_SECURE = True     # Optional but recommended
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# TLS ends at the reverse proxy (CapRover / Azure Front Door), which tells us the original scheme.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
if not DEBUG:
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", str(180 * 24 * 3600)))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("SECURE_HSTS_INCLUDE_SUBDOMAINS", "false").lower() == "true"

# CORS_ALLOW_ALL_ORIGINS = True

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = list(default_headers) + [
    "x-client-id",
    "x-client-secret",
    "x-role",
    "x-user-id",
    "x-share-password",
]
CORS_ALLOW_METHODS = ["GET", "POST", "OPTIONS", "PUT", "PATCH", "DELETE"]
# Let the browser read download filenames on cross-origin file responses.
CORS_EXPOSE_HEADERS = ["Content-Disposition"]

FILE_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
STATICFILES_DIRS = [os.path.join(BASE_DIR, "static")]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Use a dedicated queue so tasks are not consumed/discarded by foreign
# workers sharing this Redis broker (e.g. crypto/ecobserve cel_workers).
CELERY_TASK_DEFAULT_QUEUE = "ai_documines"

# Reliability (Redis broker):
#  * visibility_timeout must exceed the longest task, or Redis re-delivers a running
#    task to a second worker and it runs twice (bulk OCR/translation can take hours).
#  * results live for a week so job status survives overnight (default is one day).
#  * acks_late + prefetch 1: a task lost with a crashed worker is re-run, and long
#    tasks do not sit reserved behind each other.
#  * every socket has a timeout and publishing gives up after a few seconds: when Redis is
#    down or stalled, a web request that queues a task gets an error at once instead of
#    hanging (hung requests used to occupy every web worker, which is what blocked sign-in).
#  * a task whose worker process is killed (out of memory) is NOT put back on the queue:
#    re-queuing it made the same document kill every worker again, forever, also after
#    restarts. It is recorded as failed and the stale-work sweeper reports it to the user.
#  * every task has a time limit, and worker processes are recycled before they bloat.
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": int(os.getenv("CELERY_VISIBILITY_TIMEOUT", str(12 * 3600))),
    "socket_keepalive": True,
    "socket_timeout": float(os.getenv("CELERY_SOCKET_TIMEOUT", "5")),
    "socket_connect_timeout": float(os.getenv("CELERY_CONNECT_TIMEOUT", "3")),
    "retry_on_timeout": True,
    "health_check_interval": 30,
}
CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {
    "retry_policy": {"timeout": 5, "max_retries": 2, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5},
    "retry_on_timeout": True,
    "health_check_interval": 30,
    "socket_timeout": float(os.getenv("CELERY_SOCKET_TIMEOUT", "5")),
    "socket_connect_timeout": float(os.getenv("CELERY_CONNECT_TIMEOUT", "3")),
}
CELERY_REDIS_SOCKET_TIMEOUT = float(os.getenv("CELERY_SOCKET_TIMEOUT", "5"))
CELERY_REDIS_SOCKET_CONNECT_TIMEOUT = float(os.getenv("CELERY_CONNECT_TIMEOUT", "3"))
CELERY_REDIS_RETRY_ON_TIMEOUT = True
CELERY_BROKER_CONNECTION_TIMEOUT = float(os.getenv("CELERY_CONNECT_TIMEOUT", "3"))
# Publishing from a web request: two quick retries, then fail (never block the request).
CELERY_TASK_PUBLISH_RETRY = True
CELERY_TASK_PUBLISH_RETRY_POLICY = {"max_retries": 2, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5}
CELERY_RESULT_EXPIRES = int(os.getenv("CELERY_RESULT_EXPIRES", str(7 * 24 * 3600)))
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = False
CELERY_TASK_TRACK_STARTED = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_POOL_LIMIT = 20
CELERY_WORKER_CANCEL_LONG_RUNNING_TASKS_ON_CONNECTION_LOSS = True
# Limits (seconds). Soft: the task gets an exception and can record its failure. Hard: killed.
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", str(3 * 3600)))
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", str(3 * 3600 + 600)))
# Recycle worker processes: after N tasks, or once one grows past the memory cap (kilobytes).
CELERY_WORKER_MAX_TASKS_PER_CHILD = int(os.getenv("CELERY_MAX_TASKS_PER_CHILD", "50"))
CELERY_WORKER_MAX_MEMORY_PER_CHILD = int(os.getenv("CELERY_MAX_MEMORY_PER_CHILD_KB", str(1536 * 1024)))
CELERY_WORKER_SEND_TASK_EVENTS = False

# Two lanes. Short interactive work (emails, sign-in codes, chat answers people wait for,
# file moves) must never sit behind hours of OCR or translation, so it has its own queue
# and its own worker. A worker started without CELERY_QUEUES consumes both lanes.
CELERY_FAST_QUEUE = "ai_documines_fast"
CELERY_TASK_ROUTES = {
    "custom_authentication.tasks.*": {"queue": CELERY_FAST_QUEUE},
    "email_management.tasks.*": {"queue": CELERY_FAST_QUEUE},
    "enquiries.tasks.*": {"queue": CELERY_FAST_QUEUE},
    "grid_documents_interrogation.tasks.*": {"queue": CELERY_FAST_QUEUE},
    "document_operations.tasks.*": {"queue": CELERY_FAST_QUEUE},
    "document_search.tasks.semantic_search_task": {"queue": CELERY_FAST_QUEUE},
    "core.maintenance.*": {"queue": CELERY_FAST_QUEUE},
    "integrations.deliver_webhook": {"queue": CELERY_FAST_QUEUE},
    "integrations.run_connector_job": {"queue": CELERY_FAST_QUEUE},   # network-bound transfers: the threaded lane
    "home_dash_analytics.orchestrator": {"queue": CELERY_FAST_QUEUE},
    "home_dash_analytics.resource_agent": {"queue": CELERY_FAST_QUEUE},
    "home_dash_analytics.cost_agent": {"queue": CELERY_FAST_QUEUE},
    "home_dash_analytics.refresh_model_prices": {"queue": CELERY_FAST_QUEUE},
    "core.retention_review": {"queue": CELERY_FAST_QUEUE},
    "contracts.daily": {"queue": CELERY_FAST_QUEUE},
    "contracts.playbook_review": {"queue": CELERY_FAST_QUEUE},
    "contracts.esign_refresh": {"queue": CELERY_FAST_QUEUE},
    "contracts.esign_sweep": {"queue": CELERY_FAST_QUEUE},
}
# Work that has shown no progress for this long is reported as failed instead of "processing" forever.
STALE_RUN_AFTER_SECONDS = int(os.getenv("STALE_RUN_AFTER_SECONDS", str(CELERY_TASK_TIME_LIMIT + 1800)))

# Nightly self-healing of the search indexes (picked up by the database beat scheduler).
from celery.schedules import crontab  # noqa: E402
CELERY_BEAT_SCHEDULE = {
    "repair-search-indexes": {"task": "document_search.repair_search_indexes", "schedule": crontab(hour=2, minute=30)},
    "fail-stale-work": {"task": "core.maintenance.fail_stale_work", "schedule": crontab(minute="*/15")},
    # Agent system (home_dash_analytics/agents): orchestrator, resource and cost agents.
    "agent-orchestrator": {"task": "home_dash_analytics.orchestrator", "schedule": crontab(minute="*/5")},
    "agent-resources": {"task": "home_dash_analytics.resource_agent", "schedule": crontab(minute="*")},
    "agent-costs": {"task": "home_dash_analytics.cost_agent", "schedule": crontab(minute="*/15")},
    "ai-model-prices": {"task": "home_dash_analytics.refresh_model_prices", "schedule": crontab(hour=3, minute=17)},
    "retention-review": {"task": "core.retention_review", "schedule": crontab(hour=2, minute=41)},
    "contracts-daily": {"task": "contracts.daily", "schedule": crontab(hour=6, minute=5)},
    "contracts-esign-sweep": {"task": "contracts.esign_sweep", "schedule": crontab(minute="*/30")},
    "expire-old-tokens": {"task": "core.maintenance.purge_expired_credentials", "schedule": crontab(hour=3, minute=10)},
}

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL")
EMAIL_USE_SSL = False
EMAIL_TIMEOUT = 60


# Exact registered browser callbacks; no wildcard or caller-chosen redirects.
SPA_REDIRECT_URIS = [uri.strip() for uri in os.getenv(
    "SPA_REDIRECT_URIS",
    "https://aidocumines.com/sign_in,https://aidocumines.apps.datasqan.com/sign_in,http://localhost:4200/sign_in"
).split(",") if uri.strip()]

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://aidocumines.com")

EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "10"))

PUBLIC_SHARE_BASE_URL = os.getenv("PUBLIC_SHARE_BASE_URL", os.getenv("FRONTEND_URL", ""))
