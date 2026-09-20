# Portainer Static Buckets

A small, self-hosted, **S3-like static-site bucket host** for a Raspberry Pi.
It has a private dashboard to create named buckets and upload files. Each
bucket serves `index.html`, or a friendly placeholder until that file exists.
This is for static HTML and assets, **not an S3-compatible API**—the smaller
scope makes it practical and safe for domain placeholders.

## Deploy in Portainer

1. Ensure the NGINX stack uses an external Docker network named `proxy`. If it
   uses another name, update `networks.proxy.name` in `docker-compose.yml`. If
   NGINX does not run in Docker, remove that network section and change the
   port mapping to `"8181:8080"` (or a free Pi port).
2. In Portainer, create a stack from this folder as a Git repository/build
   context and set its `ADMIN_TOKEN` environment variable to a long unique
   secret. Generate one with `openssl rand -hex 32`.
3. Deploy. The dashboard is at `http://PI-IP:8181/`. It requires the token for
   every management action. For a hardened setup, put the dashboard behind an
   authenticated HTTPS NGINX admin route and change the port binding to
   `127.0.0.1:8181:8080` (or omit `ports` when NGINX is on the shared network).

The named Docker volume `static-buckets-data` contains every site and survives
container upgrades. Back it up before deleting the stack with **Delete volumes**.

## Publish a bucket

1. Open the dashboard and paste the admin token.
2. Create a bucket such as `acme-holding` (3–63 lowercase letters, numbers,
   and hyphens).
3. Upload `index.html`, then upload CSS, JavaScript, and images. Enter a folder
   such as `assets` for nested files.
4. It is immediately served at `http://HOST:8181/b/acme-holding/`, as well as
   through any NGINX mapping you add.

To attach a custom domain, include
`nginx/static-bucket-domain.conf.example` in that domain's NGINX server block,
replacing `placeholder` with the bucket name. To serve below a path instead,
use `nginx/static-bucket-path.conf.example`. Create a bucket first: the NGINX
route will then display its placeholder instantly, and turn into a site once
`index.html` is uploaded.

Do not point a public NGINX domain at the dashboard. The `X-Static-Bucket`
header is the trusted switch selecting a public bucket; only your reverse proxy
should set it. The dashboard API requires `ADMIN_TOKEN`; bucket content is
intentionally public when reached through NGINX.

## No Git repository?

Portainer's web compose editor cannot build a multi-file image by itself. Copy
this folder to the Pi and run once inside it:

```sh
docker build -t local/static-buckets:1.0 .
```

Then remove the `build: .` line from the compose file, paste the rest into
Portainer's Web editor, set `ADMIN_TOKEN`, and deploy. A small private Git
repository for this folder is usually the easiest update path.

## Optional API

With `TOKEN` set locally:

```sh
curl -H "X-Admin-Token: $TOKEN" http://PI-IP:8181/api/buckets
curl -X POST -H "X-Admin-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"my-site"}' http://PI-IP:8181/api/buckets
curl -X POST -H "X-Admin-Token: $TOKEN" \
  -F file=@index.html http://PI-IP:8181/api/buckets/my-site/upload
```
