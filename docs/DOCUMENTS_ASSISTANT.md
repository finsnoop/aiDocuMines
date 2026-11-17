# Documents library Assistant

The Documents toolbar opens the existing Assistant. Checked documents take precedence over filters, including selections made on other pages. Without a selection, search, ownership, matter, service, type and Locations filters describe the whole matching library, not the visible page. Changing that scope clears the old conversation and cancels its pending answer. Existing typography and colours are retained.

The API resolves document IDs and filters against the signed-in user's tenant and document ACLs. It never treats browser-supplied table rows as document evidence. Empty selections are rejected rather than broadened. Filtered scopes share the table's query helper, exclude archived-only files and deliverables, and deduplicate aliases. Folder filters require access to the root location before descendant traversal.

`POST /api/v1/core/assistant/ask/` accepts one of these contexts:

```json
{"type":"documents","file_ids":[101,102]}
```

```json
{"type":"documents","filters":{"scope":"mine","project_id":"Matter A","service_id":"Legal","extension":"docx","search":"agreement","ordering":"-modified"}}
```

Explicit selections are limited to 200 documents and fail as a whole if any document is unavailable. Filtered reading scans at most 200 files; counts are exact up to 10,000. Responses disclose partial coverage when the file or passage budget is exceeded. Collection-wide financial or risk totals must not be inferred from a relevance-ranked passage subset. Empty scopes and missing readable text do not invoke a model. Configured provider settings, model permissions, usage recording and cited answers remain those of the existing Assistant.

`GET /api/v1/documents/table/<core_file_id>/context/?folder=<folder_uuid>` resolves right-click actions lazily. The returned `node.key`, `node.id` and `node.fileFolderLinkId` identify a folder link; `node.data.file_id` identifies the stored document. Readable files without an authorized link retain Open, Ask, Quick Look, Download and basic Get info, while link-dependent operations are unavailable. Folder paths must not be exposed merely because a file is shared.

The table reuses Sidebar actions for Run, Analyse, Organise, Share, Get info and Archive. Service actions are routed to their originating Documents layout so hidden mounted pages cannot also handle the event. Extract key values opens a cited extraction question in the existing Assistant and waits for Send. Archive updates selection and totals in place.

Deploy both API and frontend changes together, API first. No database migration is required. Regression tests mock external retrieval/model calls; live model and OCR/translation service availability depend on server configuration.
