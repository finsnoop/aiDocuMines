# File document operations

The file context menu uses `FileFolderLink.id` for `/api/v1/documents/files/<id>/...`. Core download, table rows and processing APIs use `core.File.id`. The table action-context endpoint returns both identities so a numerically different link cannot silently select another document.

## Completion and stored outputs

Rename, copy, duplicate, move, archive and ZIP return HTTP 202 with `task_id`. Poll `/api/v1/documents/tasks/<task_id>/` for `pending`, `success` plus `result`, or `failure` plus a safe error. A queue acknowledgement is not completion. Task results require the initiating active user, original tenant and current source action permission. Workers reload the actor and recheck source/destination permissions.

Copies and duplicates have independent stored bytes, requester-owned File/Run/Storage records, source provenance and a link in the authorised destination folder. ZIP similarly registers a downloadable archive in the requester's tree and preserves every selected file even when names collide. Removing a folder link does not delete bytes still referenced by another location or legacy File record. Moves keep the link, project and service consistent; moving a shared owner's document requires sharing authority, rather than silently extending access to a different matter.

## Access rules

Tenant boundaries apply to owners, explicit shares and old sharing records. A foreign-tenant legacy grant cannot make a file readable. Users without an organisation can access their own documents only. Platform superuser access remains intentional.

`EffectiveAccess` has dedicated `can_duplicate`, `can_zip`, `can_protect` and `can_restore` flags. Endpoints, workers and task polling use these flags. Older `FileAccessEntry` shares retain their read/write contract: read allows ZIP, and write allows duplicate/protect/restore. Duplicate also needs management access to its destination folder. The table action context reports these capabilities individually. Guest users can read authorised files and cannot mutate them.

Unshare revokes explicit file grants, legacy direct shares and the public token. Inherited matter membership, tenant administration and platform administration are separate access paths and remain effective.

## Public links and passwords

Public sharing returns an unguessable token and a usable reader URL. Anonymous metadata and content endpoints are `/api/v1/documents/public/share/<token>/` and its `content/` child. Protected links require `X-Share-Password` on both requests. Archive, revocation and an unavailable source all prevent public access. Private storage paths and password hashes are not returned.

Protect requires an actual password of 8?128 characters and stores a Django password hash. It protects public-link access; it does not encrypt the stored document or change authorised workspace access. A hint alone cannot set protection. Legacy hint-only jobs fail clearly, and existing hint-only protected links fail closed until a password is set again. Apply migration `document_operations.0008_filefolderlink_password_hash` before deploying this API.

Version restore copies real saved bytes, preserves the replaced document as a new version, clears stale extracted text and updates the File/Storage path. It rechecks source and actor before publishing. Rename refuses a source edited after the request was queued and does not overwrite a filename collision.

## Verification

`core.tests.test_document_operation_execution` uses OAuth-authenticated HTTP requests, real temporary files and eager execution of the actual Celery operation functions. It covers persisted output bytes/records and subsequent downloads, tenant/role/share revocation, dedicated grants, public password access, version restore and failure reporting. Provider services are not involved in these file-operation tests.

Run together with the existing endpoint security regressions:

```sh
python manage.py test core.tests.test_document_operation_execution core.tests.test_security_audit.DocumentOperationTests --noinput
```
