//! `plexora-file`: byte ranges of the files an opened project reads, for the
//! page's own TIFF reader (plan Phase 6: the desktop adopts the browser node).
//!
//! The website reads a project's image and mask in the page, in workers, at
//! about a third of the desktop's time per tile (P6). The desktop page reads
//! the same files the same way through this scheme, instead of asking Python
//! for every tile, when Settings > Advanced > Browser rendering is on
//! (plexora/browser_rendering.py; off until the website's GA).
//!
//! What it can read, and what it cannot:
//!
//! - Only a file the shell GRANTED. A grant is made by `grant_local_files`
//!   (commands.rs), which the page can call only from this launch's server
//!   page (`check_caller`), and which asks that server -- over loopback, with
//!   the launch token -- which files the named project reads
//!   (`/resource_routing`'s `local`). The server names them only when the
//!   setting is on, and only regular files on this computer's own disks.
//!   The page never names a path here: a URL is `<base>/<grant id>`, the id
//!   128 random bits, so there is no path to traverse, no encoding to abuse
//!   and no way to list anything.
//! - Each grant is one canonical regular file, never a network share -- or
//!   one OME-Zarr store (a folder the server names as one, holding zarr
//!   metadata), whose files are read as `<base>/<grant id>/<key>`: a key is
//!   plain segments (`[A-Za-z0-9._-]`, never `.` or `..`), and the file it
//!   names must resolve, after every link, to a regular file inside the
//!   store's canonical folder. Grants live in memory for this run only, at
//!   most `MAX_GRANTS`.
//! - Only GET and HEAD (and the CORS preflight), only from the app's main
//!   windows, only with the `Origin` of this launch's server, and a GET must
//!   ask for a single byte range no longer than `MAX_RANGE`.
//!
//! With the setting off nothing is ever granted, and every request is a 404.

use std::collections::{HashMap, VecDeque};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tauri::http::{header, Request, Response, StatusCode};
use tauri::{Manager, Runtime, UriSchemeContext, UriSchemeResponder};

use crate::ServerState;

/// The scheme's name, as registered (lib.rs).
pub const SCHEME: &str = "plexora-file";

/// How many files may be granted at once; the oldest grant goes first.
pub const MAX_GRANTS: usize = 256;

/// The longest range one request may ask for: a TIFF tile, a strip, a
/// directory -- never a whole slide.
pub const MAX_RANGE: u64 = 64 * 1024 * 1024;

/// Where the page reaches the scheme. WebView2 (and Android) serve a custom
/// scheme as `http://<scheme>.localhost`; WKWebView and WebKitGTK as itself.
pub fn page_base() -> String {
    if cfg!(any(windows, target_os = "android")) {
        format!("http://{SCHEME}.localhost")
    } else {
        format!("{SCHEME}://localhost")
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Grant {
    pub path: PathBuf,
    pub size: u64,
    /// An OME-Zarr store's folder: its files are read by key.
    pub store: bool,
}

/// Files whose presence makes a folder a zarr store (v2 and v3).
const ZARR_MARKERS: [&str; 3] = [".zattrs", ".zgroup", "zarr.json"];

/// The deepest key a store is read at (an NGFF plate's field's level's chunk
/// of a five-dimensional array is about a dozen segments).
const MAX_KEY_SEGMENTS: usize = 32;

#[derive(Default)]
struct Table {
    by_id: HashMap<String, Grant>,
    order: VecDeque<String>,
}

/// The files the page may read through the scheme, by grant id.
#[derive(Default)]
pub struct Grants(Mutex<Table>);

/// Whether a path names a network location (`\\server\share`, `//server/x`).
fn is_network(path: &Path) -> bool {
    crate::opens::is_network_path(&path.to_string_lossy())
}

impl Grants {
    /// Grant one file: its canonical form, which must be a regular file on a
    /// local disk. Returns `(id, size)`; the same file granted again keeps its
    /// id.
    pub fn grant(&self, path: &Path) -> Result<(String, u64), String> {
        let real = local_canonical(path)?;
        let meta = std::fs::metadata(&real).map_err(|e| format!("{}: {e}", path.display()))?;
        if !meta.is_file() {
            return Err(format!("{} is not a file", path.display()));
        }
        Ok((self.insert(Grant { path: real, size: meta.len(), store: false }), meta.len()))
    }

    /// Grant one OME-Zarr store: its canonical folder, on a local disk,
    /// holding zarr metadata. Returns its id; the same store again keeps it.
    pub fn grant_store(&self, path: &Path) -> Result<String, String> {
        let real = local_canonical(path)?;
        if !real.is_dir() {
            return Err(format!("{} is not a folder", path.display()));
        }
        if !ZARR_MARKERS.iter().any(|marker| real.join(marker).is_file()) {
            return Err(format!("{} is not a zarr store", path.display()));
        }
        Ok(self.insert(Grant { path: real, size: 0, store: true }))
    }

    fn insert(&self, grant: Grant) -> String {
        let mut table = self.0.lock().unwrap();
        if let Some((id, _)) = table.by_id.iter().find(|(_, held)| held.path == grant.path && held.store == grant.store) {
            let id = id.clone();
            table.by_id.insert(id.clone(), grant);
            return id;
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        table.by_id.insert(id.clone(), grant);
        table.order.push_back(id.clone());
        while table.order.len() > MAX_GRANTS {
            if let Some(oldest) = table.order.pop_front() {
                table.by_id.remove(&oldest);
            }
        }
        id
    }

    pub fn lookup(&self, id: &str) -> Option<Grant> {
        self.0.lock().unwrap().by_id.get(id).cloned()
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.0.lock().unwrap().by_id.len()
    }
}

/// `path`'s canonical form, when it is on this computer's own disks.
fn local_canonical(path: &Path) -> Result<PathBuf, String> {
    if !path.is_absolute() || is_network(path) {
        return Err(format!("{} is not a file on this computer's own disks", path.display()));
    }
    let real = std::fs::canonicalize(path).map_err(|e| format!("{}: {e}", path.display()))?;
    // canonicalize spells a local drive \\?\C:\..., and a share \\?\UNC\...
    if real.to_string_lossy().starts_with(r"\\?\UNC\") || is_network(&strip_verbatim(&real)) {
        return Err(format!("{} is on the network", path.display()));
    }
    Ok(real)
}

/// The file a store's `key` names: plain segments only, resolved (every link
/// followed) to a regular file inside the store's canonical folder; None for
/// anything else.
pub fn store_file(root: &Path, key: &str) -> Option<PathBuf> {
    let segments: Vec<&str> = key.split('/').collect();
    if segments.is_empty() || segments.len() > MAX_KEY_SEGMENTS {
        return None;
    }
    let plain = |segment: &&str| {
        !segment.is_empty() && segment.len() <= 255 && *segment != "." && *segment != ".."
            && segment.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'_' || b == b'-')
    };
    if !segments.iter().all(plain) {
        return None;
    }
    let joined = segments.iter().fold(root.to_path_buf(), |path, segment| path.join(segment));
    let real = std::fs::canonicalize(&joined).ok()?;
    (real.starts_with(root) && real.is_file()).then_some(real)
}

/// `\\?\C:\x` as `C:\x`, so the network test sees the path's own form.
fn strip_verbatim(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    match text.strip_prefix(r"\\?\") {
        Some(rest) if !rest.starts_with("UNC\\") => PathBuf::from(rest),
        _ => path.to_path_buf(),
    }
}

/// A `Range` header's one range, as `[start, end]` inclusive, against `size`.
/// `Ok(None)`: no header. `Err`: a range this scheme does not serve (several,
/// a unit other than bytes, past the end, longer than `MAX_RANGE`).
pub fn parse_range(header: Option<&str>, size: u64) -> Result<Option<(u64, u64)>, String> {
    let Some(text) = header.map(str::trim).filter(|t| !t.is_empty()) else { return Ok(None) };
    let spec = text.strip_prefix("bytes=").ok_or("only byte ranges are served")?;
    if spec.contains(',') {
        return Err("one range per request".into());
    }
    let (first, last) = spec.split_once('-').ok_or("not a byte range")?;
    let (first, last) = (first.trim(), last.trim());
    let (start, end) = if first.is_empty() {
        // The last N bytes.
        let suffix: u64 = last.parse().map_err(|_| "not a byte range")?;
        if suffix == 0 || size == 0 {
            return Err("an empty range".into());
        }
        (size.saturating_sub(suffix), size - 1)
    } else {
        let start: u64 = first.parse().map_err(|_| "not a byte range")?;
        let end = if last.is_empty() {
            size.saturating_sub(1)
        } else {
            last.parse::<u64>().map_err(|_| "not a byte range")?.min(size.saturating_sub(1))
        };
        (start, end)
    };
    if size == 0 || start >= size || end < start {
        return Err("the range is outside the file".into());
    }
    if end - start + 1 > MAX_RANGE {
        return Err(format!("a range may be at most {} MiB", MAX_RANGE / 1024 / 1024));
    }
    Ok(Some((start, end)))
}

/// Who may read: this launch's server page, in one of the app's main windows.
pub fn allowed_caller(origin: Option<&str>, server_origin: Option<&str>, webview_label: &str) -> bool {
    if !webview_label.starts_with("main") {
        return false;
    }
    match (origin, server_origin) {
        (Some(origin), Some(server)) => {
            let ours = url::Url::parse(server).map(|u| u.origin().ascii_serialization()).ok();
            ours.as_deref() == Some(origin.trim_end_matches('/'))
        }
        _ => false,
    }
}

type Body = Vec<u8>;

fn respond(status: StatusCode, origin: &str, extra: &[(header::HeaderName, String)], body: Body) -> Response<Body> {
    let mut builder = Response::builder()
        .status(status)
        .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, origin)
        .header(header::VARY, "Origin")
        .header(header::CACHE_CONTROL, "no-store")
        .header(header::X_CONTENT_TYPE_OPTIONS, "nosniff")
        .header(header::ACCEPT_RANGES, "bytes")
        .header(
            header::ACCESS_CONTROL_EXPOSE_HEADERS,
            "Content-Range, Content-Length, Accept-Ranges",
        );
    for (name, value) in extra {
        builder = builder.header(name, value);
    }
    builder.body(body).unwrap_or_else(|_| Response::new(Vec::new()))
}

fn refuse(status: StatusCode, origin: &str, why: &str) -> Response<Body> {
    respond(
        status,
        origin,
        &[(header::CONTENT_TYPE, "text/plain; charset=utf-8".into())],
        why.as_bytes().to_vec(),
    )
}

fn read_range(path: &Path, start: u64, end: u64) -> std::io::Result<Vec<u8>> {
    let mut file = std::fs::File::open(path)?;
    file.seek(SeekFrom::Start(start))?;
    let mut bytes = vec![0u8; (end - start + 1) as usize];
    file.read_exact(&mut bytes)?;
    Ok(bytes)
}

/// The answer to one request, for the grant table and the caller's facts as
/// they are. The file is read here: call it off the main thread.
pub fn answer(grants: &Grants, request: &Request<Vec<u8>>, server_origin: Option<&str>, webview_label: &str) -> Response<Body> {
    let origin_header = request
        .headers()
        .get(header::ORIGIN)
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);
    let echo = server_origin
        .and_then(|s| url::Url::parse(s).ok())
        .map(|u| u.origin().ascii_serialization())
        .unwrap_or_else(|| "null".into());
    if !allowed_caller(origin_header.as_deref(), server_origin, webview_label) {
        return refuse(StatusCode::FORBIDDEN, &echo, "only this app's own page reads its files");
    }
    let method = request.method().as_str().to_ascii_uppercase();
    if method == "OPTIONS" {
        return respond(
            StatusCode::NO_CONTENT,
            &echo,
            &[
                (header::ACCESS_CONTROL_ALLOW_METHODS, "GET, HEAD".into()),
                (header::ACCESS_CONTROL_ALLOW_HEADERS, "Range".into()),
                (header::ACCESS_CONTROL_MAX_AGE, "600".into()),
            ],
            Vec::new(),
        );
    }
    if method != "GET" && method != "HEAD" {
        return refuse(StatusCode::METHOD_NOT_ALLOWED, &echo, "files are only read");
    }
    let asked = request.uri().path().trim_matches('/');
    let (id, key) = asked.split_once('/').unwrap_or((asked, ""));
    let Some(grant) = (!id.is_empty() && id.len() <= 64 && id.bytes().all(|b| b.is_ascii_hexdigit()))
        .then(|| grants.lookup(id))
        .flatten()
    else {
        return refuse(StatusCode::NOT_FOUND, &echo, "no such file was granted");
    };
    // A store's file by its key; a file's grant takes no key.
    let path = match (grant.store, key.is_empty()) {
        (false, true) => grant.path.clone(),
        (true, false) => match store_file(&grant.path, key) {
            Some(path) => path,
            None => return refuse(StatusCode::NOT_FOUND, &echo, "no such file in the store"),
        },
        _ => return refuse(StatusCode::NOT_FOUND, &echo, "no such file was granted"),
    };
    // The size now, not at the grant: a file rewritten in place is read as it is.
    let size = match std::fs::metadata(&path) {
        Ok(meta) if meta.is_file() => meta.len(),
        _ => return refuse(StatusCode::NOT_FOUND, &echo, "the file has gone"),
    };
    let octets = (header::CONTENT_TYPE, "application/octet-stream".to_string());
    if method == "HEAD" {
        return respond(StatusCode::OK, &echo, &[octets, (header::CONTENT_LENGTH, size.to_string())], Vec::new());
    }
    let range_header = request.headers().get(header::RANGE).and_then(|v| v.to_str().ok());
    let (start, end, status) = match parse_range(range_header, size) {
        Ok(Some((start, end))) => (start, end, StatusCode::PARTIAL_CONTENT),
        Ok(None) if size > 0 && size <= MAX_RANGE => (0, size - 1, StatusCode::OK),
        Ok(None) if size == 0 => {
            return respond(StatusCode::OK, &echo, &[octets, (header::CONTENT_LENGTH, "0".into())], Vec::new())
        }
        Ok(None) => {
            return respond(
                StatusCode::RANGE_NOT_SATISFIABLE,
                &echo,
                &[(header::CONTENT_RANGE, format!("bytes */{size}"))],
                b"ask for a byte range".to_vec(),
            )
        }
        Err(why) => {
            return respond(
                StatusCode::RANGE_NOT_SATISFIABLE,
                &echo,
                &[(header::CONTENT_RANGE, format!("bytes */{size}"))],
                why.into_bytes(),
            )
        }
    };
    match read_range(&path, start, end) {
        Ok(bytes) => {
            let mut extra = vec![octets, (header::CONTENT_LENGTH, bytes.len().to_string())];
            if status == StatusCode::PARTIAL_CONTENT {
                extra.push((header::CONTENT_RANGE, format!("bytes {start}-{end}/{size}")));
            }
            respond(status, &echo, &extra, bytes)
        }
        Err(error) => refuse(StatusCode::INTERNAL_SERVER_ERROR, &echo, &error.to_string()),
    }
}

/// The scheme's handler (registered in lib.rs): the read runs on a blocking
/// thread, never on the window's.
pub fn handle<R: Runtime>(ctx: UriSchemeContext<'_, R>, request: Request<Vec<u8>>, responder: UriSchemeResponder) {
    let app = ctx.app_handle().clone();
    let label = ctx.webview_label().to_string();
    tauri::async_runtime::spawn_blocking(move || {
        let server = app.state::<ServerState>().0.ready().map(|ready| ready.origin);
        let response = answer(&app.state::<Grants>(), &request, server.as_deref(), &label);
        responder.respond(response);
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("plexora-scheme-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    const SERVER: &str = "http://127.0.0.1:8420";

    fn get(id: &str, range: Option<&str>, origin: Option<&str>) -> Request<Vec<u8>> {
        let mut builder = Request::builder().method("GET").uri(format!("{SCHEME}://localhost/{id}"));
        if let Some(range) = range {
            builder = builder.header(header::RANGE, range);
        }
        if let Some(origin) = origin {
            builder = builder.header(header::ORIGIN, origin);
        }
        builder.body(Vec::new()).unwrap()
    }

    #[test]
    fn ranges_parse_as_one_byte_range() {
        assert_eq!(parse_range(None, 100), Ok(None));
        assert_eq!(parse_range(Some("bytes=0-9"), 100), Ok(Some((0, 9))));
        assert_eq!(parse_range(Some("bytes=90-"), 100), Ok(Some((90, 99))));
        assert_eq!(parse_range(Some("bytes=-10"), 100), Ok(Some((90, 99))));
        // Past the end is clipped, as HTTP says.
        assert_eq!(parse_range(Some("bytes=95-200"), 100), Ok(Some((95, 99))));
        assert!(parse_range(Some("bytes=100-"), 100).is_err());
        assert!(parse_range(Some("bytes=0-1,4-5"), 100).is_err());
        assert!(parse_range(Some("items=0-1"), 100).is_err());
        assert!(parse_range(Some("bytes=5-1"), 100).is_err());
        assert!(parse_range(Some(&format!("bytes=0-{}", MAX_RANGE)), MAX_RANGE * 2).is_err());
        assert!(parse_range(Some(&format!("bytes=0-{}", MAX_RANGE - 1)), MAX_RANGE * 2).is_ok());
    }

    #[test]
    fn only_the_servers_page_in_a_main_window_may_read() {
        assert!(allowed_caller(Some(SERVER), Some(SERVER), "main"));
        assert!(allowed_caller(Some(SERVER), Some(&format!("{SERVER}/")), "main-2"));
        assert!(!allowed_caller(Some(SERVER), Some(SERVER), "splash"));
        assert!(!allowed_caller(None, Some(SERVER), "main"));
        assert!(!allowed_caller(Some("http://127.0.0.1:8421"), Some(SERVER), "main"));
        assert!(!allowed_caller(Some("http://localhost:8420"), Some(SERVER), "main"));
        assert!(!allowed_caller(Some("null"), Some(SERVER), "main"));
        assert!(!allowed_caller(Some(SERVER), None, "main"));
    }

    #[test]
    fn a_granted_file_is_read_by_range_and_nothing_else_is() {
        let dir = scratch("read");
        let image = dir.join("image.ome.tif");
        let other = dir.join("secret.txt");
        std::fs::write(&image, (0u8..=255).collect::<Vec<_>>()).unwrap();
        std::fs::write(&other, b"key").unwrap();
        let grants = Grants::default();
        let (id, size) = grants.grant(&image).unwrap();
        assert_eq!(size, 256);
        assert_eq!(id.len(), 32);
        // The same file again keeps its id.
        assert_eq!(grants.grant(&dir.join(".").join("image.ome.tif")).unwrap().0, id);

        let ok = answer(&grants, &get(&id, Some("bytes=10-19"), Some(SERVER)), Some(SERVER), "main");
        assert_eq!(ok.status(), StatusCode::PARTIAL_CONTENT);
        assert_eq!(ok.body(), &(10u8..20).collect::<Vec<_>>());
        assert_eq!(ok.headers()[header::CONTENT_RANGE], "bytes 10-19/256");
        assert_eq!(ok.headers()[header::ACCESS_CONTROL_ALLOW_ORIGIN], SERVER);

        // A small file whole, without a range.
        let whole = answer(&grants, &get(&id, None, Some(SERVER)), Some(SERVER), "main");
        assert_eq!(whole.status(), StatusCode::OK);
        assert_eq!(whole.body().len(), 256);

        // Anything not granted -- an id never minted, a path, a traversal.
        for asked in ["0123456789abcdef0123456789abcdef", "C:/Windows/win.ini", "..%2Fsecret.txt", ""] {
            let refused = answer(&grants, &get(asked, Some("bytes=0-1"), Some(SERVER)), Some(SERVER), "main");
            assert_eq!(refused.status(), StatusCode::NOT_FOUND, "{asked}");
        }
        // The right id from the wrong page, or the wrong window.
        let stranger = answer(&grants, &get(&id, Some("bytes=0-1"), Some("http://127.0.0.1:9999")), Some(SERVER), "main");
        assert_eq!(stranger.status(), StatusCode::FORBIDDEN);
        let no_origin = answer(&grants, &get(&id, Some("bytes=0-1"), None), Some(SERVER), "main");
        assert_eq!(no_origin.status(), StatusCode::FORBIDDEN);
        let splash = answer(&grants, &get(&id, Some("bytes=0-1"), Some(SERVER)), Some(SERVER), "splash");
        assert_eq!(splash.status(), StatusCode::FORBIDDEN);
        // A range past the end.
        let past = answer(&grants, &get(&id, Some("bytes=300-"), Some(SERVER)), Some(SERVER), "main");
        assert_eq!(past.status(), StatusCode::RANGE_NOT_SATISFIABLE);
        // Writes are refused.
        let post = Request::builder().method("POST").uri(format!("{SCHEME}://localhost/{id}"))
            .header(header::ORIGIN, SERVER).body(Vec::new()).unwrap();
        assert_eq!(answer(&grants, &post, Some(SERVER), "main").status(), StatusCode::METHOD_NOT_ALLOWED);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn folders_relative_paths_and_shares_are_never_granted() {
        let dir = scratch("refuse");
        let grants = Grants::default();
        assert!(grants.grant(&dir).is_err(), "a folder");
        assert!(grants.grant(Path::new("relative/image.tif")).is_err());
        assert!(grants.grant(Path::new(r"\\fileserver\lab\image.tif")).is_err());
        assert!(grants.grant(Path::new("//fileserver/lab/image.tif")).is_err());
        assert!(grants.grant(&dir.join("missing.tif")).is_err());
        assert_eq!(grants.len(), 0);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_granted_store_is_read_by_key_inside_it_and_nowhere_else() {
        let dir = scratch("store");
        let store = dir.join("image.ome.zarr");
        std::fs::create_dir_all(store.join("0").join("c").join("0")).unwrap();
        std::fs::write(store.join("zarr.json"), b"{}").unwrap();
        std::fs::write(store.join("0").join("c").join("0").join("1"), (0u8..100).collect::<Vec<_>>()).unwrap();
        std::fs::write(dir.join("secret.txt"), b"key").unwrap();
        let grants = Grants::default();
        let id = grants.grant_store(&store).unwrap();
        assert_eq!(grants.grant_store(&store.join(".")).unwrap(), id, "the same store keeps its id");

        let read = |key: &str, range: Option<&str>| {
            answer(&grants, &get(&format!("{id}/{key}"), range, Some(SERVER)), Some(SERVER), "main")
        };
        let whole = read("zarr.json", None);
        assert_eq!(whole.status(), StatusCode::OK);
        assert_eq!(whole.body(), b"{}");
        let chunk = read("0/c/0/1", Some("bytes=-10"));
        assert_eq!(chunk.status(), StatusCode::PARTIAL_CONTENT);
        assert_eq!(chunk.body(), &(90u8..100).collect::<Vec<_>>());
        // A chunk never written is a 404, as a store says "fill value".
        assert_eq!(read("0/c/0/2", None).status(), StatusCode::NOT_FOUND);
        // Nothing outside the store, however the key is spelled.
        for key in ["../secret.txt", "..", "0/../../secret.txt", "./zarr.json", "0//c", "%2e%2e/secret.txt",
                    "..%2Fsecret.txt", "C:/Windows/win.ini", "0%5C..%5C..%5Csecret.txt", "0"] {
            assert_eq!(read(key, None).status(), StatusCode::NOT_FOUND, "{key}");
        }
        // The store itself is no file, and its id alone reads nothing.
        let bare = answer(&grants, &get(&id, None, Some(SERVER)), Some(SERVER), "main");
        assert_eq!(bare.status(), StatusCode::NOT_FOUND);
        // From the wrong page, nothing.
        let stranger = answer(&grants, &get(&format!("{id}/zarr.json"), None, Some("http://127.0.0.1:9999")),
                              Some(SERVER), "main");
        assert_eq!(stranger.status(), StatusCode::FORBIDDEN);
        // A link inside the store pointing out of it is not followed out.
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(dir.join("secret.txt"), store.join("leak")).unwrap();
            assert_eq!(read("leak", None).status(), StatusCode::NOT_FOUND);
        }
        #[cfg(windows)]
        if std::os::windows::fs::symlink_file(dir.join("secret.txt"), store.join("leak")).is_ok() {
            assert_eq!(read("leak", None).status(), StatusCode::NOT_FOUND);
        }
        // A file's grant takes no key.
        let (file, _) = grants.grant(&dir.join("secret.txt")).unwrap();
        let keyed = answer(&grants, &get(&format!("{file}/zarr.json"), None, Some(SERVER)), Some(SERVER), "main");
        assert_eq!(keyed.status(), StatusCode::NOT_FOUND);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn only_a_local_folder_holding_zarr_metadata_is_granted_as_a_store() {
        let dir = scratch("stores");
        let grants = Grants::default();
        assert!(grants.grant_store(&dir).is_err(), "a folder without zarr metadata");
        std::fs::write(dir.join("image.tif"), b"x").unwrap();
        assert!(grants.grant_store(&dir.join("image.tif")).is_err(), "a file");
        assert!(grants.grant_store(Path::new(r"\\fileserver\lab\image.zarr")).is_err());
        assert!(grants.grant_store(Path::new("relative.zarr")).is_err());
        let v2 = dir.join("v2.zarr");
        std::fs::create_dir_all(&v2).unwrap();
        std::fs::write(v2.join(".zattrs"), b"{}").unwrap();
        assert!(grants.grant_store(&v2).is_ok());
        assert!(store_file(&std::fs::canonicalize(&v2).unwrap(), ".zattrs").is_some());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn grants_forget_the_oldest_past_the_limit() {
        let dir = scratch("limit");
        let grants = Grants::default();
        let mut ids = Vec::new();
        for i in 0..MAX_GRANTS + 3 {
            let file = dir.join(format!("{i}.tif"));
            std::fs::write(&file, b"x").unwrap();
            ids.push(grants.grant(&file).unwrap().0);
        }
        assert_eq!(grants.len(), MAX_GRANTS);
        assert!(grants.lookup(&ids[0]).is_none());
        assert!(grants.lookup(ids.last().unwrap()).is_some());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn the_page_reaches_the_scheme_where_the_webview_serves_it() {
        let base = page_base();
        if cfg!(windows) {
            assert_eq!(base, "http://plexora-file.localhost");
        } else {
            assert!(base.starts_with("plexora-file://"));
        }
    }
}
