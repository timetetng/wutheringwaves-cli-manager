//! `--expect FILE`：可选的外部期望值表。
//!
//! 每行 `路径<TAB>大小<TAB>md5`（大小与 md5 的列顺序不限、大小可省略），
//! 由游戏 index.json 导出。有了它，输出就不只是"自报 md5"，而是与厂商给出
//! 的真值比对——这是"只比大小"做不到的那层保险。
use std::collections::HashMap;
use std::path::Path;

#[derive(Debug, Clone)]
pub struct Expectation {
    pub size: Option<u64>,
    pub md5: String,
}

pub struct ExpectTable {
    map: HashMap<String, Expectation>,
}

impl ExpectTable {
    pub fn load(path: &Path) -> Result<Self, String> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| format!("读取期望值表 {} 失败: {e}", path.display()))?;
        let mut map = HashMap::new();
        for (no, raw) in text.lines().enumerate() {
            let line = raw.trim_end_matches('\r');
            if line.trim().is_empty() || line.trim_start().starts_with('#') {
                continue;
            }
            let mut cols = line.split('\t');
            let rel = cols.next().unwrap_or("").trim();
            if rel.is_empty() {
                return Err(format!("{}:{} 缺少路径列", path.display(), no + 1));
            }
            let (mut size, mut md5) = (None, None);
            for c in cols {
                let c = c.trim();
                if c.is_empty() {
                    continue;
                }
                if c.len() == 32 && c.chars().all(|ch| ch.is_ascii_hexdigit()) {
                    md5 = Some(c.to_ascii_lowercase());
                } else if let Ok(v) = c.parse::<u64>() {
                    size = Some(v);
                } else {
                    return Err(format!(
                        "{}:{} 无法识别的列 {c:?}（应为文件大小或 32 位 md5）",
                        path.display(),
                        no + 1
                    ));
                }
            }
            let md5 = md5.ok_or_else(|| format!("{}:{} 缺少 md5 列", path.display(), no + 1))?;
            map.insert(rel.replace('\\', "/"), Expectation { size, md5 });
        }
        if map.is_empty() {
            return Err(format!("期望值表 {} 里没有任何条目", path.display()));
        }
        Ok(ExpectTable { map })
    }

    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn get(&self, rel: &str) -> Option<&Expectation> {
        // 容器里的路径已是 '/' 分隔，这里再规范化一次以便喂 Windows 风格的表。
        match self.map.get(rel) {
            Some(e) => Some(e),
            None => self.map.get(&rel.replace('\\', "/")),
        }
    }
}
