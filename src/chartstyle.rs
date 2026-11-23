use ab_glyph::{Font, FontRef, PxScale, ScaleFont};
use memmap2::Mmap;
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::process::Command;
use tiny_skia::{Color, Pixmap};

const CANDIDATES: &[&str] = &[
    "~/.local/share/fonts/wqy-microhei.ttc",
    "~/.local/share/fonts/wqy-zenhei.ttc",
    "~/.local/share/fonts/NotoSansCJK-Regular.ttc",
    "~/.local/share/fonts/simkai.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
];

fn expand_home(path: &str) -> PathBuf {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Ok(home) = std::env::var("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(path)
}

fn fc_match() -> Option<PathBuf> {
    let output = Command::new("fc-match")
        .args(["-f", "%{file}", "sans-serif:lang=zh"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let path_str = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if path_str.is_empty() {
        return None;
    }
    let p = PathBuf::from(path_str);
    p.exists().then_some(p)
}

pub fn discover_font_path(configured: Option<&str>) -> Option<PathBuf> {
    if let Some(cfg) = configured {
        let p = expand_home(cfg);
        if p.exists() {
            return Some(p);
        }
    }
    for candidate in CANDIDATES {
        let p = expand_home(candidate);
        if p.exists() {
            return Some(p);
        }
    }
    if let Some(p) = fc_match() {
        return Some(p);
    }
    for dir in ["~/.local/share/fonts", "~/.fonts"] {
        let expanded = expand_home(dir);
        if let Ok(entries) = fs::read_dir(&expanded) {
            let mut files: Vec<PathBuf> = entries
                .filter_map(Result::ok)
                .map(|e| e.path())
                .filter(|p| {
                    matches!(
                        p.extension().and_then(|e| e.to_str()),
                        Some("ttc" | "ttf" | "otf")
                    )
                })
                .collect();
            files.sort();
            if let Some(first) = files.into_iter().next() {
                return Some(first);
            }
        }
    }
    None
}

pub struct ChartFont {
    mmap: Option<Mmap>,
    pub has_cjk_font: bool,
}

impl ChartFont {
    pub fn load(configured: Option<&str>) -> Self {
        if let Some(path) = discover_font_path(configured) {
            if let Some(mmap) = Self::map_font(&path) {
                if FontRef::try_from_slice_and_index(&mmap, 0).is_ok() {
                    return Self {
                        mmap: Some(mmap),
                        has_cjk_font: true,
                    };
                }
            }
        }
        Self {
            mmap: None,
            has_cjk_font: false,
        }
    }

    fn map_font(path: &Path) -> Option<Mmap> {
        let file = File::open(path).ok()?;
        // SAFETY: Read-only font file mapping; system fonts are not mutated in place.
        unsafe { Mmap::map(&file).ok() }
    }

    fn font_ref(&self) -> Option<FontRef<'_>> {
        let bytes = self.mmap.as_deref()?;
        FontRef::try_from_slice_and_index(bytes, 0).ok()
    }

    pub fn text_width(&self, text: &str, size: f32) -> f32 {
        let Some(font) = self.font_ref() else {
            return text.chars().count() as f32 * size * 0.6;
        };
        let scaled = font.as_scaled(PxScale::from(size));
        let mut width = 0.0f32;
        let mut prev = None;
        for ch in text.chars() {
            let gid = scaled.glyph_id(ch);
            if let Some(p) = prev {
                width += scaled.kern(p, gid);
            }
            width += scaled.h_advance(gid);
            prev = Some(gid);
        }
        width
    }

    pub fn fit_text(&self, text: &str, size: f32, max_width: f32) -> String {
        if max_width <= 0.0 {
            return String::new();
        }
        if self.text_width(text, size) <= max_width {
            return text.to_string();
        }
        let mut chars: Vec<char> = text.chars().collect();
        while !chars.is_empty() {
            let candidate: String = chars.iter().collect();
            let with_ellipsis = format!("{candidate}…");
            if self.text_width(&with_ellipsis, size) <= max_width {
                return with_ellipsis;
            }
            chars.pop();
        }
        String::new()
    }

    pub fn draw_text(&self, pixmap: &mut Pixmap, x: f32, y: f32, text: &str, size: f32, color: Color) {
        let Some(font) = self.font_ref() else {
            return;
        };
        let scale = PxScale::from(size);
        let scaled = font.as_scaled(scale);
        let baseline_y = y + scaled.ascent();

        let cr = (color.red() * 255.0).round() as u8;
        let cg = (color.green() * 255.0).round() as u8;
        let cb = (color.blue() * 255.0).round() as u8;
        let width = pixmap.width() as i32;
        let height = pixmap.height() as i32;
        let data = pixmap.data_mut();

        let mut cursor_x = x;
        let mut prev = None;
        for ch in text.chars() {
            let gid = scaled.glyph_id(ch);
            if let Some(p) = prev {
                cursor_x += scaled.kern(p, gid);
            }
            let glyph = gid.with_scale_and_position(scale, ab_glyph::point(cursor_x, baseline_y));
            if let Some(outlined) = font.outline_glyph(glyph) {
                let bounds = outlined.px_bounds();
                let ox = bounds.min.x.floor() as i32;
                let oy = bounds.min.y.floor() as i32;
                outlined.draw(|gx, gy, cov| {
                    if cov <= 0.004 {
                        return;
                    }
                    let px = ox + gx as i32;
                    let py = oy + gy as i32;
                    if px < 0 || py < 0 || px >= width || py >= height {
                        return;
                    }
                    let idx = ((py * width + px) * 4) as usize;
                    let a = cov.clamp(0.0, 1.0);
                    let inv = 1.0 - a;
                    data[idx] = (cr as f32 * a + data[idx] as f32 * inv).round() as u8;
                    data[idx + 1] = (cg as f32 * a + data[idx + 1] as f32 * inv).round() as u8;
                    data[idx + 2] = (cb as f32 * a + data[idx + 2] as f32 * inv).round() as u8;
                    data[idx + 3] = 255;
                });
            }
            cursor_x += scaled.h_advance(gid);
            prev = Some(gid);
        }
    }
}

pub fn hex_color(hex: &str, alpha: u8) -> Color {
    let h = hex.trim_start_matches('#');
    let r = u8::from_str_radix(&h[0..2], 16).unwrap_or(0);
    let g = u8::from_str_radix(&h[2..4], 16).unwrap_or(0);
    let b = u8::from_str_radix(&h[4..6], 16).unwrap_or(0);
    Color::from_rgba8(r, g, b, alpha)
}
