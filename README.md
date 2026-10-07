# facturi-tools

A small HTTP service, Python standard library plus pymupdf, with four jobs:

- parse ING Romania bank statement PDFs into checked transactions,
- plan file names and folders for supplier invoices,
- match card payments to invoices and report the ones with no invoice,
- upload files to a cloud folder through rclone and confirm the size.

It holds no state and no business data. A workflow tool calls it over HTTP.

## Routes

Every route except `GET /health` needs the header `X-Facturi-Key`. Replies are JSON. Errors look like `{"error": "..."}`.

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/health` | none | `ok`, `version`, `buyerCuiSet`, and `rclone` with `configPresent`, `configDirWritable`, `remoteSet` (booleans only) |
| POST | `/inspect?filename=...` | PDF bytes | SHA-256, page count, `kind` (`statement` or `document`), first text line |
| POST | `/statement/parse` | PDF bytes | statement header, transactions, `verified` (totals and balances re-checked) |
| POST | `/invoice/plan` | JSON `reader`, `sha256`, `original_name` | `status`, `missing`, `buyerVatOk`, `fileName`, `folder`, `path` |
| POST | `/match` | JSON `transactions`, `invoices`, `suppliers`, `statement` (object or null) | `results`, `expectedMissing`, `proposedSuppliers`, `summary` |
| PUT | `/files?path=...` | file bytes | `{"ok": true, "path": ..., "size": ...}` after an upload and a size read-back |
| PUT | `/admin/rclone-config` | rclone config text | `{"ok": true}`. Exists only when `FACTURI_ADMIN=1`, otherwise 404 |

Status codes: 400 bad request, 401 missing or wrong key, 404 unknown route, 413 body over 30 MB, 500 server error.

## Settings

| Name | Meaning |
|---|---|
| `FACTURI_KEY` | Required. Shared secret, at least 24 characters. The service refuses to start without it. |
| `FACTURI_REMOTE` | rclone destination, for example `onedrive:Invoices`. All uploads go under it. |
| `FACTURI_BUYER_CUI` | Buyer tax number. Without digits here, `buyerVatOk` is always null. |
| `FACTURI_ADMIN` | Set to `1` to enable `PUT /admin/rclone-config`. Default off. |
| `PORT` | Listening port. Default 8080. |
| `RCLONE_CONFIG` | Path of the rclone config file. Default in the image: `/config/rclone/rclone.conf`. |
| `RCLONE_BIN` | rclone executable. Default `rclone`. |

## Run with Docker

```
docker build -t facturi-tools .
docker run -d --name facturi-tools -p 8080:8080 \
  -e FACTURI_KEY=<at least 24 random characters> \
  -e FACTURI_REMOTE='onedrive:Invoices' \
  -v facturi-rclone:/config/rclone \
  facturi-tools
```

The container runs as uid 10001. Mount a volume or a folder at `/config/rclone` (the folder, never the single file), because rclone rewrites its config when it refreshes a login token. A host folder must be owned by uid 10001. Without a mount the config sits in an anonymous volume and is lost on redeploy.

To load the rclone config, start once with `-e FACTURI_ADMIN=1`, send it with `PUT /admin/rclone-config`, then restart without `FACTURI_ADMIN`. `GET /health` tells you whether the config is present and writable. The service prints a warning at start for each missing piece and still starts.

## Run locally

Needs Python 3.12, pymupdf and rclone.

```
pip install pymupdf
export FACTURI_KEY=<at least 24 random characters>
export FACTURI_REMOTE='onedrive:Invoices'
export RCLONE_CONFIG="$HOME/.config/rclone/rclone.conf"
python -m facturi_tools.server
```

## Security notes

- One shared key in the `X-Facturi-Key` header, compared in constant time. Put the service behind HTTPS.
- The body limit is 30 MB. The size is checked from the header before any byte is read.
- Idle connections are dropped after 30 seconds.
- The upload path is relative, with no `..`, no backslash, no empty segment. Nothing is written outside `FACTURI_REMOTE`.
- Bodies, keys and file contents are never logged. Access log lines hold method, path without query, and status.
- The admin route is off unless `FACTURI_ADMIN=1`. It writes the config atomically with mode 600 and never echoes it.
- The rclone login can reach everything the cloud account can reach. Use a dedicated account if that matters.
