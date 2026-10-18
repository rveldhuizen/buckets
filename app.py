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

@app.delete("/api/buckets/<name>/files/<path:relative_path>")
def delete_bucket_file(name, relative_path):
    admin_required()
    directory = bucket_dir(name)

    if not directory.is_dir():
        abort(404)

    target = (directory / relative_path).resolve()

    # Prevent paths such as ../../something outside the bucket.
    if target == directory or directory not in target.parents:
        abort(404)

    # Files only: folders are intentionally not removed by this endpoint.
    if not target.is_file():
        abort(404)

    target.unlink()
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
<style>
:root{color-scheme:light;--background:#f7f8fa;--surface:#fff;--foreground:#17191c;--muted:#707780;--border:#e3e6e9;--accent:#285f50;--accent-hover:#204d41;--soft:#f1f4f2;--danger:#b42318;--radius:7px}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(ellipse at 75% 0%,#eaf1ee 0,transparent 34rem),var(--background);color:var(--foreground);font:14px/1.5 "Avenir Next",Avenir,"Segoe UI",sans-serif;min-height:100vh}
button,input{font:inherit}
button{cursor:pointer}
.app-shell{min-height:100vh}
.sidebar{position:fixed;inset:0 auto 0 0;width:238px;padding:25px 16px 18px;background:#fff;border-right:1px solid var(--border);display:flex;flex-direction:column;z-index:2}
.brand{display:flex;align-items:center;gap:11px;padding:0 9px 27px;color:inherit;text-decoration:none;font-size:15px;font-weight:650;letter-spacing:.01em}
.brand-mark{width:29px;height:29px;border-radius:7px;background:var(--accent);color:#fff;display:grid;place-items:center;font-size:15px;font-weight:700}
.nav-label{padding:0 10px;color:#9298a0;font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}
.nav-item{margin-top:8px;padding:9px 10px;border-radius:6px;display:flex;align-items:center;gap:10px;color:#444b52;text-decoration:none;font-weight:550}
.nav-item.active{background:#edf3f0;color:var(--accent)}
.nav-icon{width:17px;height:17px;display:grid;place-items:center;font-size:16px;line-height:1}
.sidebar-foot{margin-top:auto;padding:14px 10px 2px;border-top:1px solid var(--border);color:var(--muted);font-size:12px}
.sidebar-foot strong{display:block;color:#343a40;font-size:12px;font-weight:600}
.main{margin-left:238px;max-width:1500px;padding:0 44px 56px}
.topbar{min-height:93px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:20px}
.breadcrumb{color:var(--muted);font-size:12px}
h1,h2,h3,p{margin:0}
h1{margin-top:3px;font-size:20px;line-height:1.3;font-weight:650;letter-spacing:0}
.private-label{display:flex;align-items:center;gap:7px;color:#60676e;font-size:11px;font-weight:650;letter-spacing:.06em}
.private-dot,.status-dot{width:7px;height:7px;border-radius:50%;background:#3f8b69;display:inline-block}
.content{max-width:1080px;margin:33px auto 0}
.intro{margin-bottom:23px}
.intro p{margin-top:5px;color:var(--muted);font-size:13px}
.auth-strip{padding:14px 16px;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);display:flex;align-items:center;justify-content:space-between;gap:20px}
.auth-copy strong{display:block;font-size:13px;font-weight:600}
.auth-copy small{display:block;margin-top:2px;color:var(--muted);font-size:12px}
.auth-controls{display:flex;align-items:center;gap:8px}
input{height:36px;padding:0 10px;border:1px solid #d9dde1;border-radius:5px;background:#fff;color:var(--foreground);outline:none;min-width:0}
input:focus{border-color:#6f9b8b;box-shadow:0 0 0 3px #285f501c}
#token{width:min(260px,38vw)}
.button{min-height:35px;padding:0 13px;border:1px solid transparent;border-radius:5px;font-weight:600;transition:background .15s,border-color .15s}
.button-primary{background:var(--accent);color:#fff}
.button-primary:hover{background:var(--accent-hover)}
.button-secondary{background:#fff;border-color:var(--border);color:#333a40}
.button-secondary:hover{background:#f7f8f9}
.section-heading{margin:31px 0 13px;display:flex;justify-content:space-between;align-items:flex-end;gap:14px}
.section-heading h2{font-size:16px;font-weight:650}
.section-heading p{margin-top:3px;color:var(--muted);font-size:12px}
.count-label{color:var(--muted);font-size:12px;white-space:nowrap}
.create-form{display:flex;gap:8px;margin-bottom:13px}
#name{width:260px}
.message{min-height:0;margin:0;color:#9a342c;font-size:13px}
.message:not(:empty){margin:10px 0;padding:9px 11px;border:1px solid #f1d6d2;border-radius:5px;background:#fff8f7}
.buckets{display:grid;gap:12px;margin-top:10px}
.bucket-card{min-width:0;padding:17px 18px;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);box-shadow:0 1px 2px #11111108}
.bucket-head{display:flex;align-items:flex-start;justify-content:space-between;gap:15px;padding-bottom:14px;border-bottom:1px solid #edf0f2}
.bucket-name{display:flex;align-items:center;gap:9px;min-width:0}
.bucket-name h3{font-size:14px;font-weight:650;overflow-wrap:anywhere}
.published,.placeholder{padding:3px 7px;border-radius:4px;font-size:10px;font-weight:650;white-space:nowrap}
.published{background:#eaf4ee;color:#33704f}
.placeholder{background:#f2f3f4;color:#687078}
.bucket-link{display:inline-block;margin-top:4px;color:#666e75;font-size:12px;text-decoration:none;overflow-wrap:anywhere}
.bucket-link:hover{color:var(--accent);text-decoration:underline}
.remove-button{padding:5px 8px;border:1px solid var(--border);border-radius:5px;background:#fff;color:#6b4140;font-size:11px;font-weight:550;white-space:nowrap}
.remove-button:hover{border-color:#e6c4c1;background:#fff9f8;color:var(--danger)}
.bucket-tools{display:flex;align-items:center;gap:8px;margin:14px 0}
.bucket-tools input[type=file]{width:min(330px,48%);padding:4px;font-size:12px}
.bucket-tools input[type=file]::file-selector-button{height:25px;margin-right:8px;padding:0 8px;border:0;border-radius:3px;background:#f0f2f3;color:#343a40;font:inherit;cursor:pointer}
.bucket-tools input[name=path]{width:190px}
.file-tree{padding-top:12px;border-top:1px solid #edf0f2}
.file-tree-title{margin-bottom:7px;color:#5c646b;font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
.files{margin:0;padding:0;list-style:none}
.files .files{margin:3px 0 3px 13px;padding-left:12px;border-left:1px solid #e5e8e9}
.files li{margin:0;padding:3px 0}
.folder>details>summary{cursor:pointer;color:#42494f;font-size:12px;list-style:none}
.folder>details>summary::-webkit-details-marker{display:none}
.folder>details>summary::before{content:"▸";display:inline-block;width:16px;color:#89918f}
.folder>details[open]>summary::before{content:"▾"}
.file-row{display:flex;align-items:center;gap:8px;min-height:27px;color:#4e565d;font-size:12px}
.file-row a{color:#30383e;text-decoration:none;overflow-wrap:anywhere}
.file-row a:hover{color:var(--accent);text-decoration:underline}
.file-size{color:#93999e;font-size:11px;white-space:nowrap}
.delete-button{margin-left:auto;padding:3px 7px;border:1px solid transparent;border-radius:4px;background:transparent;color:#8c5551;font-size:11px}
.delete-button:hover{border-color:#f0d8d5;background:#fff8f7;color:var(--danger)}
.empty-state{padding:3px 0;color:#92989d;font-size:12px}
@media(max-width:850px){.sidebar{width:190px}.main{margin-left:190px;padding-right:25px;padding-left:25px}}
@media(max-width:640px){.sidebar{position:static;width:auto;height:auto;padding:13px 17px;border-right:0;border-bottom:1px solid var(--border)}.brand{padding:0}.nav-label,.nav-item,.sidebar-foot{display:none}.main{margin-left:0;padding:0 17px 35px}.topbar{min-height:75px}.content{margin-top:22px}.auth-strip{align-items:flex-start;flex-direction:column;gap:11px}.auth-controls{width:100%}#token{width:100%;flex:1}.bucket-card{padding:14px}.bucket-tools{align-items:stretch;flex-direction:column}.bucket-tools input[type=file],.bucket-tools input[name=path]{width:100%}.bucket-head{align-items:flex-start}.remove-button{font-size:10px}.section-heading{align-items:flex-start;flex-direction:column;gap:5px}.create-form input{flex:1;width:auto}h1{font-size:18px}}
</style>
<div class="app-shell">
  <aside class="sidebar">
    <a class="brand" href="/" aria-label="Static Buckets home"><span class="brand-mark">B</span><span>Static Buckets</span></a>
    <div class="nav-label">Workspace</div>
    <a class="nav-item active" href="#buckets" aria-current="page"><span class="nav-icon" aria-hidden="true">▦</span>Buckets</a>
    <div class="sidebar-foot"><strong>Private dashboard</strong>Token-protected access</div>
  </aside>
  <main class="main">
    <header class="topbar">
      <div><div class="breadcrumb">Workspace / Buckets</div><h1>Bucket management</h1></div>
      <div class="private-label"><span class="private-dot"></span>PRIVATE</div>
    </header>
    <div class="content">
      <section class="intro"><p>Create a bucket, then upload <code>index.html</code> and its assets. Use its public path below or configure an NGINX domain route.</p></section>
      <section class="auth-strip" aria-label="Admin access">
        <div class="auth-copy"><strong>Admin access</strong><small>Enter your token to manage buckets and their files.</small></div>
        <div class="auth-controls"><label for="token" hidden>Admin token</label><input id="token" type="password" autocomplete="current-password" placeholder="Admin token"><button class="button button-secondary" onclick="load()">Unlock</button></div>
      </section>
      <section id="buckets-section">
        <div class="section-heading" id="buckets">
          <div><h2>Your buckets</h2><p>Sites and files hosted by this instance.</p></div>
          <span class="count-label" id="bucket-count">Locked</span>
        </div>
        <form class="create-form" onsubmit="createBucket(event)"><input id="name" placeholder="project-name" aria-label="New bucket name" pattern="[a-z0-9][a-z0-9-]{1,61}[a-z0-9]" required><button class="button button-primary" type="submit">Create bucket</button></form>
        <p id="message" class="message" role="status" aria-live="polite"></p>
        <div id="bucket-list" class="buckets"></div>
      </section>
    </div>
  </main>
</div>
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
    return '<p class="empty-state">This bucket is empty.</p>';
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
              <summary>${esc(item.name)}</summary>
              ${renderFiles(bucket, item.children)}
            </details>
          </li>`;
      }

      return `
          <li class="file">
            <div class="file-row"><a href="${url}" target="_blank" rel="noopener">${esc(item.name)}</a>
            <small class="file-size">${formatSize(item.size)}</small>
            <button
              type="button"
              class="delete-button"
              onclick='deleteFile(${JSON.stringify(bucket)}, ${JSON.stringify(item.path)})'
            >
              Delete
            </button></div>
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
        <article class="bucket-card">
          <div class="bucket-head">
            <div>
              <div class="bucket-name"><span class="status-dot"></span><h3>${esc(bucket.name)}</h3><span class="${bucket.hasIndex ? 'published' : 'placeholder'}">${bucket.hasIndex ? 'Published' : 'Placeholder'}</span></div>
              <a class="bucket-link" href="/b/${encodeURIComponent(bucket.name)}/" target="_blank" rel="noopener">/b/${esc(bucket.name)}/ ↗</a>
            </div>
            <button class="remove-button" onclick="removeBucket('${esc(bucket.name)}')">Remove empty bucket</button>
          </div>
          <form class="bucket-tools" onsubmit="upload(event,'${esc(bucket.name)}')">
            <input name="file" type="file" aria-label="Choose a file to upload to ${esc(bucket.name)}" required>
            <input name="path" placeholder="Folder (optional)" aria-label="Upload destination folder">
            <button class="button button-secondary" type="submit">Upload file</button>
          </form>
          <div class="file-tree"><div class="file-tree-title">Files</div>${renderFiles(bucket.name, files)}</div>
        </article>`;
    }));

    document.querySelector('#bucket-list').innerHTML = renderedBuckets.length ? renderedBuckets.join('') : '<p class="empty-state">No buckets yet. Create one to get started.</p>';
    const publishedCount = bs.filter(bucket => bucket.hasIndex).length;
    document.querySelector('#bucket-count').textContent = `${bs.length} bucket${bs.length === 1 ? '' : 's'} · ${publishedCount} published`;
  } catch (e) {
    msg(e.message);
  }
}
async function createBucket(e){e.preventDefault();try{await api('/api/buckets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.querySelector('#name').value})});e.target.reset();load()}catch(e){msg(e.message)}}
async function upload(e,n){e.preventDefault();try{let f=new FormData(e.target);await api('/api/buckets/'+n+'/upload',{method:'POST',body:f});msg('Uploaded.');load()}catch(e){msg(e.message)}}
async function removeBucket(n){if(!confirm('Remove empty bucket '+n+'?'))return;try{await api('/api/buckets/'+n,{method:'DELETE'});load()}catch(e){msg(e.message)}}
async function deleteFile(bucket, path) {
  if (!confirm(`Delete "${path}"? This cannot be undone.`)) return;

  try {
    const encodedPath = path
      .split('/')
      .map(encodeURIComponent)
      .join('/');

    await api(
      '/api/buckets/' + encodeURIComponent(bucket) + '/files/' + encodedPath,
      { method: 'DELETE' }
    );

    msg(`Deleted ${path}`);
    load();
  } catch (e) {
    msg(e.message);
  }
}
document.querySelector('#token').value=localStorage.staticBucketsToken||'';
</script>'''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
