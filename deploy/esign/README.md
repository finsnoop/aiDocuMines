# Open-source e-signature services

aiDocuMines can send contracts for signature with five services. DocuSign and Adobe Acrobat Sign use each person's
own account (Integrations). **DocuSeal, Documenso and SignuDoc** are connected once per organisation by its
administrator (Administration → Settings → E-signature services). People choose among the services that are
connected, so signing keeps working when one of them is down or switched off.

You can use the providers' cloud services, or run them yourself with this folder.

| Service | Licence | Notes |
|---|---|---|
| DocuSeal | AGPL-3.0 | Sending a contract PDF through the API needs **DocuSeal Pro or DocuSeal cloud**. The free self-hosted edition's API only sends from templates made in DocuSeal, and the platform says so when it refuses. |
| Documenso | AGPL-3.0 | Full API. Seals signed PDFs with your certificate and appends a signing certificate page. |
| SignuDoc | MIT | Full API and signed webhooks. Young project (2026): pin a reviewed commit. PDFs up to 4.5 MB through this connection. |

**AGPL:** run DocuSeal and Documenso unmodified as separate services, as here. If you change their code and offer it
to others as a service, the licence requires publishing those changes.

## Run them

1. Copy `esign.env.example` to `esign.env` and fill it in. Pin the image tags and the SignuDoc commit to versions
   you have tested (`DOCUSEAL_TAG`, `DOCUMENSO_TAG`, `SIGNUDOC_REF`); never use `latest`.
2. Find the Docker network the API containers use (`docker network ls`) and set `AIDOC_NETWORK` to it.
3. Start only what you need:
   `docker compose --env-file esign.env up -d docuseal` (or `documenso`, `signudoc`, or all three).
4. Put each service behind your reverse proxy (for example CapRover) on its own **https** address, pointing at
   the host port (`DOCUSEAL_PORT` 3101, `DOCUMENSO_PORT` 3102, `SIGNUDOC_PORT` 3103). Signers open these pages.
5. In each service, create an API token (see the hints on the settings screen) and, where offered, a webhook with
   the address the platform shows you.
6. In aiDocuMines, Administration → Settings → E-signature services: enter the API address, the token and the
   webhook secret, then **Test connection**.

The API can reach the services privately as `http://docuseal:3000/api`, `http://documenso:3000/api/v2` and
`http://signudoc:3000/api/v1` (plain http is only accepted for these internal names, set by `ESIGN_INTERNAL_HOSTS`).
Set `ESIGN_PUBLIC_API_URL` on the API to its public https address so services can notify it when a document is
signed; without it, the platform checks open requests every 30 minutes.

## Documenso signing certificate

Documenso seals each signed PDF with a certificate in a `.p12` file. For production, use a certificate from a
trusted provider. To try it out you can create a self-signed one:

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=Datasqan e-signature"
openssl pkcs12 -export -out cert.p12 -inkey key.pem -in cert.pem -passout pass:CHANGE_ME
```

## Keeping them safe

- Update deliberately: read the release notes, test on staging, then change the pinned tag or commit.
- Watch each project's security advisories (GitHub "Watch → Security alerts").
- Back up the `docuseal-data`, `documenso-db` and `signudoc-db` volumes with the platform's other backups.
- Signed PDFs and certificates are also stored in aiDocuMines next to the contract, so signed records survive even
  if a service is retired.
