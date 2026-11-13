#!/bin/bash
set -euo pipefail

echo "🚀 Starting System Services..."
echo "🧭 SERVICE_NAME=${SERVICE_NAME:-}"

########################################
# Load .env (ONLY fill missing vars; do NOT override docker/compose env)
########################################
if [ -f "/app/.env" ]; then
    echo "✅ .env found (will only fill missing vars)"
    while IFS='=' read -r key value; do
        # Skip blanks and comments (also handles leading whitespace before #)
        [[ -z "${key:-}" || "$key" =~ ^[[:space:]]*# ]] && continue

        # Trim whitespace around key
        key="$(printf '%s' "$key" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

        # Remove possible Windows CR at end of value
        value="${value%$'\r'}"

        # Trim whitespace around value (NO xargs)
        value="$(printf '%s' "${value:-}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

        # Strip surrounding double quotes if present
        value="${value#\"}"; value="${value%\"}"

        # Only set if not already present in environment
        if [ -z "${!key+x}" ]; then
            export "$key=$value"
        fi
    done < /app/.env
else
    echo "⚠️ .env file not found, relying on container env"
fi

########################################
# Validate required env
########################################
required_vars=(
  POSTGRES_USER
  POSTGRES_PASSWORD
  POSTGRES_DB
  POSTGRES_HOST
  POSTGRES_PORT
)

for var in "${required_vars[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo "❌ Missing required env var: $var"
        exit 1
    fi
done

########################################
# Wait for PostgreSQL
########################################
echo "🔍 Waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
until PGPASSWORD="$POSTGRES_PASSWORD" \
      psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -p "$POSTGRES_PORT" -d "$POSTGRES_DB" -c '\q' >/dev/null 2>&1; do
    sleep 2
done
echo "✅ PostgreSQL is ready!"

########################################
# DB PREPARE (INIT CONTAINER)
########################################
if [ "${SERVICE_NAME:-}" = "db_prepare" ]; then
    echo "🛠️ Running database preparation..."

    python manage.py migrate --noinput

    echo "🔐 Seeding RBAC permissions and roles..."
    python manage.py seed_rbac

    echo "👤 Ensuring superuser exists..."
    python manage.py shell <<EOF
import json
from django.contrib.auth import get_user_model
from oauth2_provider.models import Application
from oauth2_provider.generators import generate_client_secret
from custom_authentication.models import Client

User = get_user_model()
email = "$DJANGO_SUPERUSER_EMAIL"
password = "$DJANGO_SUPERUSER_PASSWORD"
outfile = "/app/logs/.superuser_secrets.json"

client, _ = Client.objects.get_or_create(
    name="AI DocuMines",
    defaults={"industry": "AI", "use_case": "Bootstrap"}
)

if not User.objects.filter(email=email).exists():
    user = User.objects.create_superuser(
        email=email,
        password=password,
        client=client
    )
    superadmin_group, _ = Group.objects.get_or_create(name="SuperAdmin")
    user.groups.add(superadmin_group)
else:
    user = User.objects.get(email=email)
user.is_2fa_enabled = True
user.two_factor_enabled = True
user.save()
secret = generate_client_secret()
app = Application.objects.create(
    user=user,
    client_type=Application.CLIENT_CONFIDENTIAL,
    authorization_grant_type=Application.GRANT_PASSWORD,
    name="System Admin",
    client_secret=secret
)
with open(outfile, "w") as f:
    json.dump({
        "email": email,
        "password": password,
        "client_id": app.client_id,
        "client_secret": secret
    }, f, indent=2)
print("✅ Superuser created")
EOF

    touch /app/logs/migrations_complete
    echo "✅ DB preparation finished — exiting."
    exit 0
fi

########################################
# WEB (GUNICORN)
########################################
if [ "${SERVICE_NAME:-}" = "web" ]; then
    echo "📂 Collecting static files..."
    python manage.py collectstatic --noinput

    # ── Auto-indexing ───────────────────────────────────────────────────
    echo "🔍 Checking for unindexed files..."
    python manage.py shell -c "
import os, django
os.environ['DJANGO_SETTINGS_MODULE'] = 'aiDocuMines.settings'
django.setup()
from core.models import File
from document_search.models import VectorChunk
if File.objects.exists() and not VectorChunk.objects.exists():
    from document_search.tasks import bulk_reindex
    r = bulk_reindex()
    print(f'📥 Auto-indexing queued: {r}')
else:
    print(f'✅ Files: {File.objects.count()}, VectorChunks: {VectorChunk.objects.count()} — no indexing needed.')
" 2>&1 | grep -v -E "(UserWarning|PyMilvusDeprecation|pk_resources|RequestDeprecat|tika|nlp_engine|Created NLP|registry|Loaded recognizer|Recognizer not|spaCy|140 object)" | grep -v "^\s*$" | tail -5

    echo "🌍 Starting Gunicorn..."
    exec gunicorn aiDocuMines.wsgi:application \
        --bind 0.0.0.0:8020 \
        --timeout 120 \
        --log-level debug
fi

########################################
# CELERY
########################################
if [ "${SERVICE_NAME:-}" = "celery" ]; then
    echo "🚀 Starting Celery worker..."
    exec celery -A aiDocuMines worker --loglevel=info --concurrency=4
fi

if [ "${SERVICE_NAME:-}" = "celery_beat" ]; then
    echo "🚀 Starting Celery beat..."
    exec celery -A aiDocuMines beat \
        --loglevel=info \
        --scheduler django_celery_beat.schedulers:DatabaseScheduler
fi

########################################
# FILE MONITOR
########################################
if [ "${SERVICE_NAME:-}" = "file_monitor" ]; then
    echo "👁️ Starting Supervisor..."
    exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
fi

########################################
# OLLAMA
########################################
if [ "${SERVICE_NAME:-}" = "ollama" ]; then
    echo "📦 Pulling Ollama models..."
    ollama pull mistral || true
    ollama pull llama3 || true
    ollama pull deepseek-coder:6.7b || true
    echo "✅ Ollama ready — exiting."
    exit 0
fi

########################################
# SAFETY NET
########################################
echo "❌ Unknown SERVICE_NAME='${SERVICE_NAME:-}'"
exit 1

