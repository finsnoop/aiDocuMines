# Remove all live platform documents

`manage.py purge_documents` is an offline operator command. It previews by default. Deletion requires both `--execute --all-tenants`. This procedure must run on the VPS against its actual storage and databases; a successful localhost test does not delete VPS data.

## What is removed and retained

The command removes uploaded documents, generated outputs, saved versions, private anonymization mappings, document processing records, workflow execution records, search/query results, connector job results, and cached document intelligence across all organisations. It clears the configured Elasticsearch document index and Milvus document vectors synchronously and verifies those stores are empty. It never deletes every search index or resets Milvus metadata.

Accounts, organisations, roles, settings, matter/deal definitions, workflow definitions/steps, template definitions/fields, integration credentials/configuration, and unrelated account assets remain. Uploaded template blueprint bytes are documents and are removed. Their definitions retain their authenticated owner, but execution returns HTTP 409 until an authorised user attaches another source upload. Business run records needed by an integration configuration are retained, rather than cascading into that configuration.

Backups, remote originals in SharePoint/Nextcloud/other connected sources, unrelated audit/account records and unselected external application databases are outside this local document purge. They are not erased by this command. Do not run `clean_aidocumines_db.sh`; it deletes accounts and authentication data. The separate development script `danger_file_deletion.py` is not the live procedure documented here.

## Stop all writers first

Use the API repository checkout and Compose project that own the running VPS containers. Pull the approved release, build its web image, and stop the watchdog before stopping services. Keep PostgreSQL, Redis, Elasticsearch, Milvus and its etcd running.

```sh
set -e
sudo systemctl stop aidocumines-watchdog.timer aidocumines-watchdog.service
docker compose build web
docker compose stop web celery celery_fast celery_beat file_monitor
docker compose run --rm --no-deps --entrypoint python web manage.py migrate --noinput
```

Also stop any separately deployed Node API, import/export services, automation runners and other upload/connector producers. Block incoming writes through any additional application instances. The command cannot prove that a producer outside this Compose project is stopped. Do not start those writers again until deletion and verification succeed. Previously queued document tasks must be drained or removed through the scoped queue maintenance procedure before workers resume; do not flush a shared Redis database or delete unrelated jobs.

Take any backup you need before execution; this procedure has no automatic restore. Existing backup files are deliberately retained.

## Preview, then execute

```sh
docker compose run --rm --no-deps --entrypoint python web manage.py purge_documents
```

Preview returns a JSON report with `mode: "preview"`, file/row/cache counts, approved storage roots, search-store counts and errors. It never unlinks files, deletes rows, clears cache entries or creates a missing Milvus collection. An unavailable search/cache service, unsafe path or symlink blocks execution. Resolve every reported error first.

The default allowlist is the resolved `MEDIA_ROOT`. Exact recorded document paths must be below that root. The orphan sweep is limited to explicitly named document directories such as `uploads`, `versions`, `service_outputs`, `ocr_cache`, rendered `previews`, bulk exports and demo/scale fixture documents; it does not wipe the whole media directory. Avatars, backups and unrelated assets are retained. A separate owned storage location can be approved explicitly with repeated `--storage-root /absolute/document-root` arguments. Additional roots permit their recorded files only; they are not recursively swept for unregistered files. Symlink paths and broad roots such as `/`, the code checkout or a home directory are refused. The execution uses Linux directory descriptors with `O_NOFOLLOW` when unlinking files.

After reviewing the report, execute the identical scope:

```sh
docker compose run --rm --no-deps --entrypoint python web manage.py purge_documents --execute --all-tenants
```

A successful execution reports `mode: "execute"`, `complete: true`, `remaining_files: 0` and zero remaining documents/vectors in its search results. A preview's `complete: true` means only that preflight succeeded. Filesystem failure retains database rows for retry. A later cache/search failure reports incomplete with a nonzero exit status, even if document rows have already been removed. Keep writers stopped, resolve the error and rerun; do not interpret partial deletion as completion.

## Separate Node document caches

The API command can optionally clear the exact cache tables `files`, `ocr`, `anonymization`, `translation`, plus document-linked `activitylogs`, using an existing configured Django database alias:

```sh
docker compose run --rm --no-deps --entrypoint python web manage.py purge_documents --include-node-caches --node-database default
docker compose run --rm --no-deps --entrypoint python web manage.py purge_documents --execute --all-tenants --include-node-caches --node-database default
```

Use `default` only when those Node tables really share the API database. If Node uses a separate database, configure its verified alias or use a separately reviewed Node-side cache cleanup procedure. Do not pass secrets on the command line or assume missing tables mean a separate database was cleared. A requested but missing/unexpected Node schema blocks this command. Without the option, the report explicitly says `node_cache_scope: "not_requested"`; it does not claim those caches were cleared. Users, two-factor configuration, LLM configuration, dashboard layouts, playbooks, project definitions and backups are not cache tables in the deletion allowlist. No `TRUNCATE ... CASCADE` is used.

The Node repository now provides the tested separate-database command `node scripts/purge_document_caches.mjs` (preview) and `node scripts/purge_document_caches.mjs --execute --all-tenants` (deletion). It uses that service's existing database and TLS configuration, validates the fixed cache-table schema and preserves account/configuration tables. Pull the Node release too, follow `aiDocuMines-Backend/docs/DOCUMENT_CACHE_CLEANUP.md`, and run its post-deletion preview before restarting writers. A successful Django purge alone does not verify a separate Node database.

## Verification and restart

Run the same preview after successful execution and check that source files, derived document rows, selected Node caches and the configured search stores are empty. Retained business runs and template definitions are expected. Record both execution and verification output, including the selected Node cache scope. Repeat every `--storage-root`, `--include-node-caches` and `--node-database` option used for execution in the verification command below.

```sh
docker compose run --rm --no-deps --entrypoint python web manage.py purge_documents
```

Once the chosen scope is verified and queued document work is resolved, restart the stopped services and then the watchdog:

```sh
docker compose start web celery celery_fast celery_beat file_monitor
sudo systemctl start aidocumines-watchdog.timer
```

Restart separately deployed services using their own deployment controls. Resuming a connector may import a new copy from a retained remote original; retaining integration settings does not suppress future imports.

## Local regression coverage

The isolated test suite uses real temporary document bytes, Django test rows and the deployed Django RedisCache backend with a unique prefix. It covers cross-tenant removal, template/workflow/account/config preservation, orphan directories, unsafe paths, symlinks, partial failures, Node table allowlisting and Redis prefix/version isolation. Search adapters are isolated in these synthetic purge tests.

The additional opt-in search test passed against real local Elasticsearch and Milvus services. It seeds private indexes/collections, verifies preview retains their data, deletes that data, verifies zero remaining records, and checks another index/collection remains untouched. It cleans up only its own test resources. These local checks do not delete or verify VPS data.

```sh
python manage.py test core.tests.test_document_purge core.tests.test_document_purge_cache core.tests.test_automation_template_preservation --noinput
RUN_DOCUMENT_SEARCH_E2E=1 python manage.py test core.tests.test_document_purge_search_e2e --noinput
```
