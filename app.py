"""Private multi-bucket static-site host for a small Docker deployment."""
import hmac
import os
import re
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

DATA_DIR = Path(os.getenv("DATA_DIR", "/data/buckets")).resolve()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "100"))
BUCKET_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")

if not ADMIN_TOKEN or ADMIN_TOKEN == "replace-with-a-long-random-secret":
    raise RuntimeError("ADMIN_TOKEN must be set to a long, unique secret")
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def valid_bucket(name):
    return bool(BUCKET_RE.fullmatch(name))


def bucket_dir(name):
    if not valid_bucket(name):
        abort(404)
    result = (DATA_DIR / name).resolve()
    if result.parent != DATA_DIR:
        abort(404)
    return result


def admin_required():
    supplied = request.headers.get("X-Admin-Token", "")
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[7:]
    if not hmac.compare_digest(supplied, ADMIN_TOKEN):
        abort(401)


def bucket_placeholder(name):
    escaped = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Response(
        f'''<!doctype html><meta charset="utf-8"><title>{escaped}</title>
<style>body{{font:16px system-ui,sans-serif;max-width:42rem;margin:12vh auto;padding:0 1.5rem;color:#172033}}code{{background:#eef2f7;padding:.15rem .35rem;border-radius:.25rem}}</style>
<h1>{escaped}</h1><p>This placeholder bucket is ready.</p><p>Upload an <code>index.html</code> file to publish this site.</p>''',
        mimetype="text/html",
    )


def serve_bucket(name, path=""):
    root = bucket_dir(name)
    if not root.is_dir():
        abort(404)
    candidate = (root / Path(path)).resolve()
    if candidate != root and root not in candidate.parents:
        abort(404)
    if candidate.is_dir():
        candidate = candidate / "index.html"
    if candidate.is_file():
        return send_from_directory(root, str(candidate.relative_to(root)), conditional=True)
    if not path or path.endswith("/"):
        return bucket_placeholder(name)
    abort(404)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/buckets")
def list_buckets():
    admin_required()
    buckets = [
        {"name": entry.name, "hasIndex": (entry / "index.html").is_file()}
        for entry in sorted(DATA_DIR.iterdir())
        if entry.is_dir() and valid_bucket(entry.name)
    ]
    return jsonify(buckets)


@app.post("/api/buckets")
def create_bucket():
    admin_required()
    name = str((request.get_json(silent=True) or {}).get("name", ""))
    if not valid_bucket(name):
        return jsonify(error="Use 3–63 lowercase letters, numbers, or hyphens; start and end with a letter or number."), 400
    directory = bucket_dir(name)
    if directory.exists():
        return jsonify(error="Bucket already exists"), 409
    directory.mkdir()
    return jsonify(name=name), 201


@app.delete("/api/buckets/<name>")
def delete_bucket(name):
    admin_required()
    directory = bucket_dir(name)
    if not directory.is_dir():
        abort(404)
    try:
        directory.rmdir()  # Never erase a site with a one-click operation.
    except OSError:
        return jsonify(error="Bucket is not empty. Delete its files from the dashboard first."), 409
    return "", 204

@app.get("/api/buckets/<name>/files")
def list_bucket_files(name):
    admin_required()
    directory = bucket_dir(name)

    if not directory.is_dir():
        abort(404)

    def walk(current):
        items = []

        for entry in sorted(
            current.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower())
        ):
            relative = entry.relative_to(directory).as_posix()

            if entry.is_dir():
                items.append({
                    "name": entry.name,
                    "path": relative,
                    "type": "directory",
                    "children": walk(entry),
                })
            elif entry.is_file():
                items.append({
                    "name": entry.name,
                    "path": relative,
                    "type": "file",
                    "size": entry.stat().st_size,
                })

        return items

    return jsonify(walk(directory))


@app.post("/api/buckets/<name>/upload")
def upload(name):
    admin_required()
    directory = bucket_dir(name)
    if not directory.is_dir():
        abort(404)
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify(error="Choose one file"), 400
    target_path = request.form.get("path", "").strip("/")
    raw_pieces = [p for p in target_path.split("/") if p]
    pieces = [secure_filename(p) for p in raw_pieces]
    if target_path and (not pieces or any(not p for p in pieces)):
        return jsonify(error="Invalid destination path"), 400
    filename = secure_filename(uploaded.filename)
    if not filename:
        return jsonify(error="Invalid filename"), 400
    destination = directory.joinpath(*pieces, filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    uploaded.save(destination)
    return jsonify(path=str(destination.relative_to(directory))), 201


@app.errorhandler(RequestEntityTooLarge)
def too_large(_error):
    return jsonify(error=f"File exceeds the {MAX_UPLOAD_MB} MB upload limit"), 413


@app.get("/b/<name>/", defaults={"path": ""})
@app.get("/b/<name>/<path:path>")
def path_bucket(name, path):
    return serve_bucket(name, path)


@app.route("/", defaults={"path": ""}, methods=["GET", "HEAD"])
@app.route("/<path:path>", methods=["GET", "HEAD"])
def domain_bucket(path):
    # NGINX sets this trusted routing header per public domain.
    name = request.headers.get("X-Static-Bucket")
    if name:
        return serve_bucket(name, path)
    return Response(ADMIN_HTML, mimetype="text/html") if not path else abort(404)


ADMIN_HTML = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Static Buckets</title>
<style>body{font:16px system-ui,sans-serif;max-width:800px;margin:3rem auto;padding:0 1rem;color:#172033}input,button{font:inherit;padding:.55rem;margin:.25rem 0}input{min-width:16rem}button{cursor:pointer}li{margin:.5rem 0;padding:.6rem;background:#f4f7fa;border-radius:.35rem}small{color:#566}
.files {
  margin: .5rem 0 0;
  padding-left: 1.25rem;
  background: transparent;
}

.files li {
  margin: .2rem 0;
  padding: .2rem .4rem;
  background: transparent;
}

.file-tree {
  margin: .75rem 0;
  padding: .6rem;
  background: #eef2f7;
  border-radius: .35rem;
}

.file small {
  margin-left: .4rem;
}</style>
<h1>Static Buckets</h1><p>Create a bucket, then upload <code>index.html</code> and its assets. Use its public path shown below, or configure an NGINX domain route.</p>
<p><label>Admin token <input id="token" type="password" autocomplete="current-password"></label> <button onclick="load()">Unlock</button></p>
<form onsubmit="createBucket(event)"><input id="name" placeholder="project-name" pattern="[a-z0-9][a-z0-9-]{1,61}[a-z0-9]" required><button>Create bucket</button></form>
<ul id="buckets"></ul><p id="message"></p>
<script>
const t=()=>document.querySelector('#token').value; const msg=x=>document.querySelector('#message').textContent=x;
async function api(url,opts={}){opts.headers={...(opts.headers||{}),'X-Admin-Token':t()};let r=await fetch(url,opts);if(!r.ok)throw Error((await r.json().catch(()=>({error:r.statusText}))).error);return r.status===204?null:r.json()}
const esc = value => String(value)
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#039;');

const formatSize = bytes => {
  if (bytes < 1024) return bytes + ' B';

  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;

  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }

  return value.toFixed(value < 10 ? 1 : 0) + ' ' + units[unit];
};

function renderFiles(bucket, items) {
  if (!items.length) {
    return '<small>This bucket is empty.</small>';
  }

  return `<ul class="files">${
    items.map(item => {
      const url = `/b/${encodeURIComponent(bucket)}/${item.path
        .split('/')
        .map(encodeURIComponent)
        .join('/')}`;

      if (item.type === 'directory') {
        return `
          <li class="folder">
            <details>
              <summary>📁 ${esc(item.name)}</summary>
              ${renderFiles(bucket, item.children)}
            </details>
          </li>`;
      }

      return `
        <li class="file">
          📄 <a href="${url}" target="_blank" rel="noopener">${esc(item.name)}</a>
          <small>${formatSize(item.size)}</small>
        </li>`;
    }).join('')
  }</ul>`;
}

async function load() {
  try {
    const bs = await api('/api/buckets');
    localStorage.staticBucketsToken = t();

    const renderedBuckets = await Promise.all(bs.map(async bucket => {
      const files = await api(
        '/api/buckets/' + encodeURIComponent(bucket.name) + '/files'
      );

      return `
        <li>
          <b>${esc(bucket.name)}</b>
          ${bucket.hasIndex ? '✓ published' : 'placeholder'}
          — <a href="/b/${encodeURIComponent(bucket.name)}/" target="_blank">/b/${esc(bucket.name)}/</a>

          <form onsubmit="upload(event,'${esc(bucket.name)}')">
            <input name="file" type="file" required>
            <input name="path" placeholder="folder (optional)">
            <button>Upload</button>
          </form>

          <div class="file-tree">
            <b>Files</b>
            ${renderFiles(bucket.name, files)}
          </div>

          <button onclick="removeBucket('${esc(bucket.name)}')">
            Remove empty bucket
          </button>
        </li>`;
    }));

    document.querySelector('#buckets').innerHTML = renderedBuckets.join('');
  } catch (e) {
    msg(e.message);
  }
}
async function createBucket(e){e.preventDefault();try{await api('/api/buckets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.querySelector('#name').value})});e.target.reset();load()}catch(e){msg(e.message)}}
async function upload(e,n){e.preventDefault();try{let f=new FormData(e.target);await api('/api/buckets/'+n+'/upload',{method:'POST',body:f});msg('Uploaded.');load()}catch(e){msg(e.message)}}
async function removeBucket(n){if(!confirm('Remove empty bucket '+n+'?'))return;try{await api('/api/buckets/'+n,{method:'DELETE'});load()}catch(e){msg(e.message)}}
document.querySelector('#token').value=localStorage.staticBucketsToken||'';
</script>'''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
