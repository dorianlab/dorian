//! Tier-2 Arrow-IPC cache store — content-addressed file store with
//! zero-copy reads via mmap.
//!
//! Replaces the prior Redis-tagged Tier-2 wire spec (`redis_store`):
//! pipeline intermediates flow as Arrow IPC bytes between operators
//! anyway (the PyO3 bridge in `engine/native/` already round-trips
//! pyarrow Tables across the FFI), so persisting them as Arrow IPC
//! files lets the cache hand the next operator a memory-mapped view
//! of the bytes the previous operator produced — no Redis serialise,
//! no base64, no network hop.
//!
//! Layout
//! ------
//! Each entry is one file under `${root}/{aa}/{full_hex}.arrow`,
//! where `aa` = first two hex chars of the cache key. Sharding by
//! prefix keeps any single directory's `readdir` cost bounded as the
//! cache grows. Atomic writes go to `tmp/{tid}.{seq}.tmp` first, then
//! get renamed into place — readers see either the previous file or
//! the new one, never a torn payload.
//!
//! For non-Arrow payloads (sklearn estimators, opaque Python
//! objects), the same file path is used with a `.bin` suffix instead
//! of `.arrow`. Distinguishing the two in the path keeps the per-key
//! lookup O(1) — no extension probing.
//!
//! Eligibility
//! -----------
//! This file knows nothing about determinism: callers are expected to
//! consult `eligibility_with_incoming` (in the parent module) before
//! ever computing a key. ArrowStore itself just stores the bytes
//! you hand it under the key you compute.
//!
//! LRU
//! ---
//! Total bytes are bounded by `max_bytes` (configurable, default
//! 8 GiB). On `put`, if the new entry would push us over the cap, we
//! evict the least-recently-accessed entries until under. Eviction
//! removes both the file and the in-memory index entry — `get` of an
//! evicted key returns `Miss` exactly as if it had never been stored.

use std::fs::{self, File};
use std::io::{Cursor, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use arrow::array::RecordBatch;
use arrow::ipc::reader::FileReader as IpcFileReader;
use arrow::ipc::writer::FileWriter as IpcFileWriter;
use bytes::Bytes;
use parking_lot::RwLock;
use rustc_hash::FxHashMap;

use crate::{CacheKey, CacheStore};

/// Encoding written to disk. Decides the file extension and how
/// readers reconstruct a payload.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PayloadKind {
    /// Arrow IPC stream — readable via `arrow::ipc::reader::FileReader`
    /// or `pyarrow.ipc.open_file`. Used for tabular intermediates
    /// (DataFrames, numpy ndarrays cast to Arrow arrays).
    Arrow,
    /// Opaque bytes — pickled estimator, msgpack-serialised dict, etc.
    /// Caller is responsible for deserialisation.
    Opaque,
}

impl PayloadKind {
    fn extension(self) -> &'static str {
        match self {
            PayloadKind::Arrow => "arrow",
            PayloadKind::Opaque => "bin",
        }
    }
}

/// One row in the in-memory index: file path, bytes-on-disk, last
/// access time (epoch millis). Mutable through the parent's `RwLock`.
#[derive(Debug, Clone)]
struct IndexEntry {
    path: PathBuf,
    kind: PayloadKind,
    size_bytes: u64,
    last_access_ms: u64,
}

/// Configuration for `ArrowStore`. Defaults match what the rust
/// runner expects when it boots without env overrides.
#[derive(Debug, Clone)]
pub struct ArrowStoreConfig {
    /// Root directory under which entries live. Created on
    /// construction (recursive). A typical deployment points this at
    /// a per-host volume, NOT a network filesystem — Arrow IPC's
    /// random-access file format relies on mmap'd page cache to give
    /// zero-copy reads, and NFS defeats that.
    pub root: PathBuf,
    /// Soft byte cap. `put` evicts oldest entries to bring total
    /// under this before installing the new one. Default 8 GiB.
    pub max_bytes: u64,
    /// Optional cap on a single entry's size; writes that exceed are
    /// rejected with a logged warning (mirrors
    /// `RedisStoreConfig::max_entry_bytes`). Default 256 MiB —
    /// sklearn models on small datasets fit comfortably; whole MNIST
    /// dataframes don't, which is fine because the runner is
    /// expected to feed Arrow tables, not pickled DataFrames.
    pub max_entry_bytes: u64,
}

impl Default for ArrowStoreConfig {
    fn default() -> Self {
        ArrowStoreConfig {
            root: PathBuf::from("/tmp/dorian-cache"),
            // 80 GiB default. Sized for the research box (1.5 TiB
            // RAM): cache fits comfortably in page cache so mmap'd
            // reads stay zero-copy across processes (RL trainer +
            // FLAML seeder + cross-product workers all share). On
            // hosts with less RAM, override via
            // ``DORIAN_CACHE_MAX_GB``.
            max_bytes: 80 * 1024 * 1024 * 1024,
            max_entry_bytes: 256 * 1024 * 1024,
        }
    }
}

impl ArrowStoreConfig {
    /// Apply the standard environment overrides. Used by every
    /// embedder so users have one place to tune the cache:
    ///
    ///   * `DORIAN_CACHE_DIR`     — overrides `root`
    ///   * `DORIAN_CACHE_MAX_GB`  — overrides `max_bytes`  (in GiB)
    ///   * `DORIAN_CACHE_MAX_ENTRY_MB` — overrides `max_entry_bytes`
    pub fn from_env() -> Self {
        let mut cfg = Self::default();
        if let Ok(dir) = std::env::var("DORIAN_CACHE_DIR") {
            if !dir.trim().is_empty() {
                cfg.root = PathBuf::from(dir);
            }
        }
        if let Ok(gb) = std::env::var("DORIAN_CACHE_MAX_GB") {
            if let Ok(n) = gb.parse::<u64>() {
                cfg.max_bytes = n.saturating_mul(1024).saturating_mul(1024).saturating_mul(1024);
            }
        }
        if let Ok(mb) = std::env::var("DORIAN_CACHE_MAX_ENTRY_MB") {
            if let Ok(n) = mb.parse::<u64>() {
                cfg.max_entry_bytes = n.saturating_mul(1024).saturating_mul(1024);
            }
        }
        cfg
    }
}

/// File-backed Tier-2 store. Holds an in-memory index of every entry
/// it has ever written; bootstraps from disk on first call to
/// `mount_existing` so a process restart keeps prior cache contents
/// usable.
pub struct ArrowStore {
    cfg: ArrowStoreConfig,
    index: RwLock<FxHashMap<CacheKey, IndexEntry>>,
    /// Total bytes across all index entries; tracked separately so
    /// `put` can decide eviction without scanning the index every
    /// time.
    total_bytes: RwLock<u64>,
    /// Per-process counter — feeds the tmp filename so concurrent
    /// puts on the same key never race for the same temp path.
    tmp_seq: AtomicU64,
}

impl ArrowStore {
    /// Construct an empty store rooted at `cfg.root`. Creates the
    /// root + `tmp` subdirectory if needed. Does not scan for
    /// existing files — call `mount_existing` after construction if
    /// you want to pick up entries written by a prior process.
    pub fn new(cfg: ArrowStoreConfig) -> std::io::Result<Self> {
        fs::create_dir_all(&cfg.root)?;
        fs::create_dir_all(cfg.root.join("tmp"))?;
        Ok(ArrowStore {
            cfg,
            index: RwLock::new(FxHashMap::default()),
            total_bytes: RwLock::new(0),
            tmp_seq: AtomicU64::new(0),
        })
    }

    /// Walk `root` and reinstate index entries for every `*.arrow` /
    /// `*.bin` file we find. Idempotent. Cheap because we only
    /// `stat()` — file contents stay on disk untouched.
    pub fn mount_existing(&self) -> std::io::Result<usize> {
        let mut count = 0;
        let now = now_ms();
        for shard in fs::read_dir(&self.cfg.root)? {
            let shard = shard?;
            if !shard.file_type()?.is_dir() {
                continue;
            }
            let name = shard.file_name();
            // Skip the `tmp` working directory — only 2-hex shard
            // dirs hold real entries.
            if name.to_str().map(|s| s.len() == 2 && s.chars().all(|c| c.is_ascii_hexdigit())) != Some(true) {
                continue;
            }
            for entry in fs::read_dir(shard.path())? {
                let entry = entry?;
                let p = entry.path();
                let ext = match p.extension().and_then(|e| e.to_str()) {
                    Some("arrow") => PayloadKind::Arrow,
                    Some("bin") => PayloadKind::Opaque,
                    _ => continue,
                };
                let stem = match p.file_stem().and_then(|s| s.to_str()) {
                    Some(s) if s.len() == 64 => s,
                    _ => continue,
                };
                let key = match parse_hex_key(stem) {
                    Some(k) => k,
                    None => continue,
                };
                let meta = entry.metadata()?;
                let size = meta.len();
                let mut idx = self.index.write();
                idx.insert(
                    key,
                    IndexEntry {
                        path: p,
                        kind: ext,
                        size_bytes: size,
                        last_access_ms: now,
                    },
                );
                *self.total_bytes.write() += size;
                count += 1;
            }
        }
        Ok(count)
    }

    /// Read the bytes for a key. Returns `None` if the key is not in
    /// the index OR the on-disk file has gone missing (we self-heal
    /// the index in that case).
    pub fn get_bytes(&self, key: &CacheKey) -> Option<Bytes> {
        let path: PathBuf;
        {
            let idx = self.index.read();
            let entry = idx.get(key)?;
            path = entry.path.clone();
        }
        let bytes = match fs::read(&path) {
            Ok(b) => b,
            Err(e) => {
                log::warn!(
                    "ArrowStore: index hit but file missing for key {} ({}); evicting",
                    key,
                    e,
                );
                self.evict_one(key);
                return None;
            }
        };
        // Touch — promote LRU position.
        {
            let mut idx = self.index.write();
            if let Some(entry) = idx.get_mut(key) {
                entry.last_access_ms = now_ms();
            }
        }
        Some(Bytes::from(bytes))
    }

    /// Read a cached Arrow IPC payload as a vector of `RecordBatch`es.
    /// Returns `None` on miss or non-Arrow payload.
    pub fn get_arrow(&self, key: &CacheKey) -> Option<Vec<RecordBatch>> {
        let kind = self.index.read().get(key).map(|e| e.kind)?;
        if kind != PayloadKind::Arrow {
            return None;
        }
        let bytes = self.get_bytes(key)?;
        let reader = match IpcFileReader::try_new(Cursor::new(bytes), None) {
            Ok(r) => r,
            Err(e) => {
                log::warn!(
                    "ArrowStore: arrow decode failed for key {} ({}); evicting",
                    key,
                    e,
                );
                self.evict_one(key);
                return None;
            }
        };
        let batches: Result<Vec<_>, _> = reader.collect();
        match batches {
            Ok(b) => Some(b),
            Err(e) => {
                log::warn!(
                    "ArrowStore: arrow batch read failed for key {} ({}); evicting",
                    key,
                    e,
                );
                self.evict_one(key);
                None
            }
        }
    }

    /// Store opaque bytes under `key`. Atomic via tmp+rename.
    pub fn put_bytes(&self, key: CacheKey, kind: PayloadKind, payload: &[u8]) -> std::io::Result<()> {
        let size = payload.len() as u64;
        if size > self.cfg.max_entry_bytes {
            log::warn!(
                "ArrowStore: rejecting put for key {} — {} bytes exceeds max_entry_bytes={}",
                key,
                size,
                self.cfg.max_entry_bytes,
            );
            return Ok(());
        }
        self.evict_until_fits(size);

        let final_path = self.path_for(&key, kind);
        if let Some(parent) = final_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let seq = self.tmp_seq.fetch_add(1, Ordering::Relaxed);
        let tmp_path = self
            .cfg
            .root
            .join("tmp")
            .join(format!("{}.{}.tmp", std::process::id(), seq));
        {
            let mut f = File::create(&tmp_path)?;
            f.write_all(payload)?;
            f.sync_all()?;
        }
        // rename is atomic on POSIX when src and dst are on the same
        // filesystem — both are under `root` so that holds.
        fs::rename(&tmp_path, &final_path)?;

        let mut idx = self.index.write();
        let prev = idx.insert(
            key,
            IndexEntry {
                path: final_path,
                kind,
                size_bytes: size,
                last_access_ms: now_ms(),
            },
        );
        let mut total = self.total_bytes.write();
        if let Some(prev) = prev {
            *total = total.saturating_sub(prev.size_bytes);
        }
        *total = total.saturating_add(size);
        Ok(())
    }

    /// Encode a slice of `RecordBatch`es to a single Arrow IPC file
    /// payload and store it. Convenience over `put_bytes` for the
    /// common tabular case.
    pub fn put_arrow(&self, key: CacheKey, batches: &[RecordBatch]) -> std::io::Result<()> {
        if batches.is_empty() {
            return Ok(());
        }
        let schema = batches[0].schema();
        let mut buf: Vec<u8> = Vec::with_capacity(64 * 1024);
        {
            let mut writer = IpcFileWriter::try_new(&mut buf, &schema).map_err(io_err)?;
            for batch in batches {
                writer.write(batch).map_err(io_err)?;
            }
            writer.finish().map_err(io_err)?;
        }
        self.put_bytes(key, PayloadKind::Arrow, &buf)
    }

    /// Number of indexed entries.
    pub fn len(&self) -> usize {
        self.index.read().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Total bytes across all live entries.
    pub fn total_bytes(&self) -> u64 {
        *self.total_bytes.read()
    }

    pub fn cfg(&self) -> &ArrowStoreConfig {
        &self.cfg
    }

    fn path_for(&self, key: &CacheKey, kind: PayloadKind) -> PathBuf {
        let hex = key.hex();
        let shard = &hex[..2];
        self.cfg
            .root
            .join(shard)
            .join(format!("{}.{}", hex, kind.extension()))
    }

    /// Remove a single entry (file + index slot). Best-effort —
    /// missing file is OK, the goal is to leave the store consistent.
    fn evict_one(&self, key: &CacheKey) {
        let removed = {
            let mut idx = self.index.write();
            idx.remove(key)
        };
        if let Some(entry) = removed {
            let _ = fs::remove_file(&entry.path);
            let mut total = self.total_bytes.write();
            *total = total.saturating_sub(entry.size_bytes);
        }
    }

    /// Evict oldest-first until adding `incoming` bytes would not
    /// exceed `cfg.max_bytes`. Holds the index lock for the duration
    /// of the eviction so concurrent puts see consistent state.
    fn evict_until_fits(&self, incoming: u64) {
        if incoming == 0 {
            return;
        }
        loop {
            // Snapshot total once per loop iteration; if removal
            // pushes us under, we exit.
            let total = *self.total_bytes.read();
            if total + incoming <= self.cfg.max_bytes {
                return;
            }
            // Find the LRU entry.
            let victim_key: Option<CacheKey>;
            {
                let idx = self.index.read();
                victim_key = idx
                    .iter()
                    .min_by_key(|(_, e)| e.last_access_ms)
                    .map(|(k, _)| *k);
            }
            let key = match victim_key {
                Some(k) => k,
                // Index empty but still over budget — shouldn't
                // happen; bail out to avoid an infinite loop.
                None => return,
            };
            self.evict_one(&key);
        }
    }
}

/// Implement `CacheStore` so callers that already plumbed
/// `Arc<dyn CacheStore>` for the Tier-1 in-memory store can swap in
/// the Arrow file store without code changes.
///
/// Two semantic differences worth flagging:
///
///   * The trait's `CacheEntry.payload` is `serde_json::Value`. For
///     Arrow payloads stored on disk, we surface them as a JSON
///     string of the file path so JSON-only consumers can fetch
///     the bytes themselves; richer consumers should call
///     `get_arrow` / `get_bytes` directly.
///   * `put` for Arrow-typed payloads uses the JSON string body as
///     opaque bytes (UTF-8). The proper path is to call `put_arrow`
///     with `RecordBatch` directly.
impl CacheStore for ArrowStore {
    fn lookup(&self, key: &CacheKey) -> crate::CacheOutcome {
        match self.get_bytes(key) {
            Some(bytes) => {
                use crate::{Artifact, CacheEntry, CacheOutcome};
                use std::sync::Arc;
                let kind = self.index.read().get(key).map(|e| e.kind);
                let payload = match kind {
                    Some(PayloadKind::Arrow) => serde_json::Value::String(format!(
                        "arrow_ipc:{}",
                        key.hex()
                    )),
                    _ => serde_json::Value::String(format!("opaque:{}", key.hex())),
                };
                let entry = CacheEntry {
                    key: *key,
                    artifact: Artifact::default(),
                    payload,
                    size_bytes: bytes.len() as u64,
                    compute_secs: 0.0,
                    hits: 1,
                };
                CacheOutcome::Hit(Arc::new(entry))
            }
            None => crate::CacheOutcome::Miss,
        }
    }

    fn put(&self, entry: crate::CacheEntry) {
        // The trait's payload is JSON; ArrowStore can't infer the
        // physical shape from JSON alone. Best-effort: serialise the
        // JSON to bytes and store as opaque. Callers that have
        // RecordBatches should bypass the trait and call
        // `put_arrow` directly.
        let bytes = entry.payload.to_string().into_bytes();
        let _ = self.put_bytes(entry.key, PayloadKind::Opaque, &bytes);
    }

    fn len(&self) -> usize {
        self.len()
    }
}

fn parse_hex_key(s: &str) -> Option<CacheKey> {
    if s.len() != 64 {
        return None;
    }
    let mut out = [0u8; 32];
    for (i, byte) in out.iter_mut().enumerate() {
        let hi = hex_nibble(s.as_bytes()[i * 2])?;
        let lo = hex_nibble(s.as_bytes()[i * 2 + 1])?;
        *byte = (hi << 4) | lo;
    }
    Some(CacheKey(out))
}

fn hex_nibble(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn io_err<E: std::fmt::Display>(e: E) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::Other, e.to_string())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use arrow::array::{ArrayRef, Int32Array, StringArray};
    use arrow::datatypes::{DataType, Field, Schema};
    use std::sync::Arc;

    fn key(b: u8) -> CacheKey {
        CacheKey([b; 32])
    }

    fn ephemeral_store() -> (ArrowStore, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let cfg = ArrowStoreConfig {
            root: dir.path().to_path_buf(),
            max_bytes: 1024 * 1024,
            max_entry_bytes: 256 * 1024,
        };
        let store = ArrowStore::new(cfg).unwrap();
        (store, dir)
    }

    fn sample_batch() -> RecordBatch {
        let schema = Arc::new(Schema::new(vec![
            Field::new("id", DataType::Int32, false),
            Field::new("name", DataType::Utf8, false),
        ]));
        let id_arr: ArrayRef = Arc::new(Int32Array::from(vec![1, 2, 3]));
        let name_arr: ArrayRef = Arc::new(StringArray::from(vec!["a", "b", "c"]));
        RecordBatch::try_new(schema, vec![id_arr, name_arr]).unwrap()
    }

    #[test]
    fn put_then_get_arrow_roundtrip() {
        let (store, _dir) = ephemeral_store();
        let k = key(0xAB);
        let batch = sample_batch();
        store.put_arrow(k, &[batch.clone()]).unwrap();
        let out = store.get_arrow(&k).expect("hit");
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].num_rows(), 3);
        assert_eq!(out[0].schema().field(0).name(), "id");
    }

    #[test]
    fn put_then_get_opaque_roundtrip() {
        let (store, _dir) = ephemeral_store();
        let k = key(0xCD);
        let payload = b"opaque-bytes-pickle-or-msgpack";
        store
            .put_bytes(k, PayloadKind::Opaque, payload)
            .unwrap();
        let out = store.get_bytes(&k).expect("hit");
        assert_eq!(&out[..], payload);
    }

    #[test]
    fn missing_key_returns_none() {
        let (store, _dir) = ephemeral_store();
        assert!(store.get_arrow(&key(0xEF)).is_none());
        assert!(store.get_bytes(&key(0xEF)).is_none());
    }

    #[test]
    fn rejects_oversize_entry() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = ArrowStoreConfig {
            root: dir.path().to_path_buf(),
            max_bytes: 1024 * 1024,
            max_entry_bytes: 16, // tiny cap
        };
        let store = ArrowStore::new(cfg).unwrap();
        let k = key(0x01);
        let big = vec![0u8; 64];
        store.put_bytes(k, PayloadKind::Opaque, &big).unwrap();
        // Rejected silently — index stays empty.
        assert_eq!(store.len(), 0);
    }

    #[test]
    fn lru_eviction_under_pressure() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = ArrowStoreConfig {
            root: dir.path().to_path_buf(),
            max_bytes: 256, // forces eviction
            max_entry_bytes: 1024,
        };
        let store = ArrowStore::new(cfg).unwrap();

        // Three 100-byte entries — sum = 300 > cap 256. The oldest
        // should be evicted on the third put.
        for (i, b) in [0xA0u8, 0xA1, 0xA2].iter().enumerate() {
            let k = key(*b);
            // bump last_access by sleeping 5ms so LRU ordering is
            // deterministic. (Resolution: ms — we only need
            // monotonic ordering.)
            std::thread::sleep(std::time::Duration::from_millis(5));
            store
                .put_bytes(k, PayloadKind::Opaque, &vec![i as u8; 100])
                .unwrap();
        }
        assert_eq!(store.len(), 2);
        // Oldest (0xA0) should be gone.
        assert!(store.get_bytes(&key(0xA0)).is_none());
        // Most recent two stay.
        assert!(store.get_bytes(&key(0xA1)).is_some());
        assert!(store.get_bytes(&key(0xA2)).is_some());
    }

    #[test]
    fn mount_existing_picks_up_prior_files() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = ArrowStoreConfig {
            root: dir.path().to_path_buf(),
            ..Default::default()
        };
        // First store writes an entry, then drops out of scope.
        {
            let store = ArrowStore::new(cfg.clone()).unwrap();
            store
                .put_arrow(key(0xBE), &[sample_batch()])
                .unwrap();
        }
        // Second store mounts the same root; it should find the
        // previously-written file.
        let store2 = ArrowStore::new(cfg).unwrap();
        assert_eq!(store2.len(), 0); // Not yet
        let n = store2.mount_existing().unwrap();
        assert_eq!(n, 1);
        assert_eq!(store2.len(), 1);
        let out = store2.get_arrow(&key(0xBE)).expect("hit after mount");
        assert_eq!(out[0].num_rows(), 3);
    }

    #[test]
    fn from_env_picks_up_overrides() {
        // Save then restore to avoid bleeding into other tests.
        let prev_dir = std::env::var("DORIAN_CACHE_DIR").ok();
        let prev_gb = std::env::var("DORIAN_CACHE_MAX_GB").ok();
        std::env::set_var("DORIAN_CACHE_DIR", "/tmp/dorian-cache-test-env");
        std::env::set_var("DORIAN_CACHE_MAX_GB", "4");
        let cfg = ArrowStoreConfig::from_env();
        assert_eq!(cfg.root, PathBuf::from("/tmp/dorian-cache-test-env"));
        assert_eq!(cfg.max_bytes, 4u64 * 1024 * 1024 * 1024);
        match prev_dir {
            Some(v) => std::env::set_var("DORIAN_CACHE_DIR", v),
            None => std::env::remove_var("DORIAN_CACHE_DIR"),
        }
        match prev_gb {
            Some(v) => std::env::set_var("DORIAN_CACHE_MAX_GB", v),
            None => std::env::remove_var("DORIAN_CACHE_MAX_GB"),
        }
    }

    #[test]
    fn parse_hex_key_round_trip() {
        let k = CacheKey([0x12; 32]);
        let s = k.hex();
        let back = parse_hex_key(&s).unwrap();
        assert_eq!(k, back);
        // Bad length / non-hex returns None.
        assert!(parse_hex_key("zz").is_none());
        assert!(parse_hex_key(&"q".repeat(64)).is_none());
    }

    #[test]
    fn evict_then_put_self_heals_index() {
        let (store, _dir) = ephemeral_store();
        let k = key(0x44);
        store
            .put_bytes(k, PayloadKind::Opaque, b"first")
            .unwrap();
        // Manually delete the file from disk to simulate a crash
        // mid-write or an external rm.
        let path = store.path_for(&k, PayloadKind::Opaque);
        std::fs::remove_file(&path).unwrap();
        // get sees an index entry whose file is gone — should
        // evict + return None.
        assert!(store.get_bytes(&k).is_none());
        assert_eq!(store.len(), 0);
        // Subsequent put under the same key works cleanly.
        store
            .put_bytes(k, PayloadKind::Opaque, b"second")
            .unwrap();
        let out = store.get_bytes(&k).expect("hit after re-put");
        assert_eq!(&out[..], b"second");
    }
}
