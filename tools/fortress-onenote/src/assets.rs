//! Content-addressed asset store: `assets/<sha256>.<ext>`.
//!
//! File names coming from OneNote files are untrusted and never used as
//! paths; only the content hash and a sanitised extension are.

use crate::manifest::AssetInfo;
use anyhow::{Context, Result};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

pub struct AssetStore {
    dir: PathBuf,
    pub assets: BTreeMap<String, AssetInfo>,
    by_hash: BTreeMap<String, String>,
    tmp_counter: u64,
}

/// Sanitise an untrusted extension: ASCII alphanumerics only, lower case,
/// 1..=10 chars.
pub fn sanitize_ext(ext: &str) -> Option<String> {
    let e = ext.trim().trim_start_matches('.').to_ascii_lowercase();
    if e.is_empty() || e.len() > 10 || !e.chars().all(|c| c.is_ascii_alphanumeric()) {
        return None;
    }
    Some(e)
}

/// Extension of an untrusted file name, sanitised.
pub fn ext_of_name(name: &str) -> Option<String> {
    let base = name.rsplit(['/', '\\']).next().unwrap_or(name);
    let (stem, ext) = base.rsplit_once('.')?;
    if stem.is_empty() {
        return None;
    }
    sanitize_ext(ext)
}

/// Sanitise an untrusted file name for *display* (never for paths): strips
/// directories and control characters and caps the length.
pub fn display_name(name: &str) -> String {
    let base = name.rsplit(['/', '\\']).next().unwrap_or(name);
    let cleaned: String = base
        .chars()
        .filter(|c| {
            !c.is_control() && !matches!(c, '\u{202A}'..='\u{202E}' | '\u{2066}'..='\u{2069}')
        })
        .collect();
    let cleaned = cleaned.trim().trim_matches('.').trim();
    let mut out: String = cleaned.chars().take(200).collect();
    if out.is_empty() {
        out = "file".into();
    }
    out
}

/// Sniff a file type from its first bytes. Returns (ext, mime).
pub fn sniff(head: &[u8]) -> Option<(&'static str, &'static str)> {
    let starts = |p: &[u8]| head.starts_with(p);
    if starts(b"\x89PNG\r\n\x1a\n") {
        Some(("png", "image/png"))
    } else if starts(b"\xFF\xD8\xFF") {
        Some(("jpg", "image/jpeg"))
    } else if starts(b"GIF87a") || starts(b"GIF89a") {
        Some(("gif", "image/gif"))
    } else if starts(b"BM") && head.len() > 14 {
        Some(("bmp", "image/bmp"))
    } else if starts(b"II*\0") || starts(b"MM\0*") {
        Some(("tiff", "image/tiff"))
    } else if head.len() >= 12 && &head[0..4] == b"RIFF" && &head[8..12] == b"WEBP" {
        Some(("webp", "image/webp"))
    } else if starts(b"%PDF") {
        Some(("pdf", "application/pdf"))
    } else if starts(&[0x01, 0x00, 0x00, 0x00]) && head.len() >= 44 && &head[40..44] == b" EMF" {
        Some(("emf", "image/emf"))
    } else if starts(&[0xD7, 0xCD, 0xC6, 0x9A]) {
        Some(("wmf", "image/wmf"))
    } else if head.len() >= 12 && &head[4..8] == b"ftyp" {
        let brand = &head[8..12];
        if brand == b"heic" || brand == b"heix" || brand == b"mif1" {
            Some(("heic", "image/heic"))
        } else if brand == b"M4A " {
            Some(("m4a", "audio/mp4"))
        } else {
            Some(("mp4", "video/mp4"))
        }
    } else if starts(b"<svg") || starts(b"<?xml") {
        Some(("svg", "image/svg+xml"))
    } else if starts(b"PK\x03\x04") {
        Some(("zip", "application/zip"))
    } else {
        None
    }
}

const ZIP_BASED: &[&str] = &[
    "xps", "oxps", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub", "jar", "apk",
];

/// Whether an asset name is an image type browsers display natively.
pub fn is_web_image(asset: &str) -> bool {
    let ext = asset.rsplit('.').next().unwrap_or("");
    matches!(ext, "png" | "jpg" | "jpeg" | "gif" | "webp" | "svg" | "bmp")
}

/// MIME type for a (sanitised) extension.
pub fn mime_for_ext(ext: &str) -> &'static str {
    match ext {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "bmp" => "image/bmp",
        "tif" | "tiff" => "image/tiff",
        "webp" => "image/webp",
        "svg" => "image/svg+xml",
        "emf" => "image/emf",
        "wmf" => "image/wmf",
        "heic" => "image/heic",
        "pdf" => "application/pdf",
        "xps" => "application/vnd.ms-xpsdocument",
        "doc" => "application/msword",
        "docx" => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xls" => "application/vnd.ms-excel",
        "xlsx" => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ppt" => "application/vnd.ms-powerpoint",
        "pptx" => "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "txt" | "log" => "text/plain",
        "csv" => "text/csv",
        "htm" | "html" => "text/html",
        "md" => "text/markdown",
        "rtf" => "application/rtf",
        "json" => "application/json",
        "xml" => "application/xml",
        "zip" => "application/zip",
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "wma" => "audio/x-ms-wma",
        "m4a" => "audio/mp4",
        "mp4" => "video/mp4",
        "mov" => "video/quicktime",
        "wmv" => "video/x-ms-wmv",
        "avi" => "video/x-msvideo",
        _ => "application/octet-stream",
    }
}

impl AssetStore {
    pub fn new(dir: &Path) -> Result<Self> {
        fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
        Ok(AssetStore {
            dir: dir.to_path_buf(),
            assets: BTreeMap::new(),
            by_hash: BTreeMap::new(),
            tmp_counter: 0,
        })
    }

    /// Stream `reader` into the store. `ext_hint` (already sanitised) is used
    /// when sniffing the content does not identify the type, or when
    /// `prefer_hint` is set (attachments keep their own extension).
    /// Returns the asset name (`<sha256>.<ext>`), or `None` for empty data.
    pub fn put_reader(
        &mut self,
        reader: &mut dyn Read,
        ext_hint: Option<&str>,
        prefer_hint: bool,
    ) -> Result<Option<String>> {
        self.tmp_counter += 1;
        let tmp = self
            .dir
            .join(format!(".tmp-{}-{}", std::process::id(), self.tmp_counter));
        let mut file = File::create(&tmp).with_context(|| format!("creating {}", tmp.display()))?;
        let mut hasher = Sha256::new();
        let mut head: Vec<u8> = Vec::with_capacity(64);
        let mut size: u64 = 0;
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            let n = match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => n,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => {
                    let _ = fs::remove_file(&tmp);
                    return Err(e).context("reading embedded data");
                }
            };
            if head.len() < 64 {
                let take = (64 - head.len()).min(n);
                head.extend_from_slice(&buf[..take]);
            }
            hasher.update(&buf[..n]);
            file.write_all(&buf[..n])?;
            size += n as u64;
        }
        file.flush()?;
        drop(file);
        if size == 0 {
            let _ = fs::remove_file(&tmp);
            return Ok(None);
        }
        let sha = hex::encode(hasher.finalize());
        if let Some(existing) = self.by_hash.get(&sha) {
            let _ = fs::remove_file(&tmp);
            return Ok(Some(existing.clone()));
        }
        let mut sniffed = sniff(&head);
        // Zip-based formats (XPS, Office documents) sniff as zip; trust the
        // hint for those.
        if sniffed.map(|s| s.0) == Some("zip")
            && let Some(h) = ext_hint
            && ZIP_BASED.contains(&h)
        {
            sniffed = None;
        }
        let (ext, mime): (String, String) = match (prefer_hint, ext_hint, sniffed) {
            (true, Some(h), _) => (h.to_string(), mime_for_ext(h).to_string()),
            (_, _, Some((e, m))) => (e.to_string(), m.to_string()),
            (_, Some(h), None) => (h.to_string(), mime_for_ext(h).to_string()),
            (_, None, None) => ("bin".to_string(), "application/octet-stream".to_string()),
        };
        let name = format!("{sha}.{ext}");
        let dest = self.dir.join(&name);
        fs::rename(&tmp, &dest).with_context(|| format!("writing {}", dest.display()))?;
        self.by_hash.insert(sha.clone(), name.clone());
        self.assets.insert(
            name.clone(),
            AssetInfo {
                path: format!("assets/{name}"),
                sha256: sha,
                size,
                mime,
                ext,
            },
        );
        Ok(Some(name))
    }

    pub fn put_bytes(&mut self, data: &[u8], ext_hint: Option<&str>) -> Result<Option<String>> {
        let mut r: &[u8] = data;
        self.put_reader(&mut r, ext_hint, true)
    }

    pub fn total_bytes(&self) -> u64 {
        self.assets.values().map(|a| a.size).sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn untrusted_names() {
        assert_eq!(ext_of_name("../../etc/passwd"), None);
        assert_eq!(ext_of_name("C:\\x\\Report.PDF").as_deref(), Some("pdf"));
        assert_eq!(ext_of_name("a.p/df"), None);
        assert_eq!(ext_of_name(".bashrc"), None);
        assert_eq!(ext_of_name("x.verylongextension"), None);
        assert_eq!(display_name("..\\..\\evil\u{202E}fdp.exe"), "evilfdp.exe");
        assert_eq!(display_name("/"), "file");
        assert_eq!(sanitize_ext("p\0ng"), None);
    }

    #[test]
    fn dedupes_by_content() {
        let dir = std::env::temp_dir().join(format!("fo-assets-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let mut s = AssetStore::new(&dir).unwrap();
        let png = b"\x89PNG\r\n\x1a\nrest-of-image";
        let a = s.put_bytes(png, None).unwrap().unwrap();
        let mut r: &[u8] = png;
        let b = s.put_reader(&mut r, Some("bin"), false).unwrap().unwrap();
        assert_eq!(a, b);
        assert!(a.ends_with(".png"));
        assert_eq!(s.assets.len(), 1);
        assert!(dir.join(&a).exists());
        assert_eq!(s.put_bytes(b"", None).unwrap(), None);
        let t = s.put_bytes(b"hello", Some("txt")).unwrap().unwrap();
        assert!(t.ends_with(".txt"));
        assert_eq!(s.assets[&t].mime, "text/plain");
        // No stray temp files.
        let names: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        assert!(names.iter().all(|n| !n.starts_with(".tmp")));
        fs::remove_dir_all(&dir).unwrap();
    }
}
