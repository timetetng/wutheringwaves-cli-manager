//! `krpatch` — native Linux applier for Kuro Games directory diffs (`.krpdiff`).
//!
//! Command line:
//! ```text
//! krpatch apply --old-root DIR --out-root DIR [--workers N] [--cache-mb N] [--fsync] PATCH...
//! krpatch info PATCH...
//! krpatch version
//! ```
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

mod apply;
mod ffi;
mod head;
mod streams;

const USAGE: &str = "\
用法:
  krpatch apply --old-root DIR --out-root DIR [选项] PATCH...
  krpatch info PATCH...
  krpatch version

apply 选项:
  --old-root DIR    旧版本游戏根目录（读取补丁引用的旧文件）
  --out-root DIR    输出根目录（按补丁内相对路径写出新文件）
  --workers N       并发分组数（默认 min(CPU, 分组数)）
  --cache-mb N      每组解压临时缓存 MiB（默认 8，无需调大）
  --fsync           写完每个文件后执行 fsync（默认关）
  --json FILE       把结果 JSON 写入文件（默认 stdout）
  --quiet           不输出进度
";

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match run(&args) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("krpatch: {e}");
            2
        }
    };
    std::process::exit(code);
}

fn run(args: &[String]) -> Result<i32, String> {
    let cmd = args.first().map(|s| s.as_str()).unwrap_or("");
    match cmd {
        "apply" => cmd_apply(&args[1..]),
        "info" => cmd_info(&args[1..]),
        "version" => {
            println!(
                "krpatch {} (upstream HDiffPatch {}, zstd plugin {})",
                env!("CARGO_PKG_VERSION"),
                ffi::upstream_version(),
                if ffi::zstd_plugin_available() {
                    "ok"
                } else {
                    "MISSING"
                }
            );
            Ok(0)
        }
        "-h" | "--help" | "" => {
            print!("{USAGE}");
            Ok(if cmd.is_empty() { 2 } else { 0 })
        }
        other => Err(format!("未知子命令 {other:?}\n{USAGE}")),
    }
}

struct ApplyCli {
    old_root: PathBuf,
    out_root: PathBuf,
    workers: usize,
    cache_mb: usize,
    fsync: bool,
    json: Option<PathBuf>,
    quiet: bool,
    patches: Vec<PathBuf>,
}

fn parse_apply_args(args: &[String]) -> Result<ApplyCli, String> {
    let mut cli = ApplyCli {
        old_root: PathBuf::new(),
        out_root: PathBuf::new(),
        workers: 0,
        cache_mb: 8,
        fsync: false,
        json: None,
        quiet: false,
        patches: Vec::new(),
    };
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let mut value = |name: &str| -> Result<String, String> {
            it.next()
                .cloned()
                .ok_or_else(|| format!("{name} 需要一个参数"))
        };
        match a.as_str() {
            "--old-root" => cli.old_root = PathBuf::from(value("--old-root")?),
            "--out-root" => cli.out_root = PathBuf::from(value("--out-root")?),
            "--workers" => {
                cli.workers = value("--workers")?
                    .parse()
                    .map_err(|_| "--workers 需要正整数".to_string())?
            }
            "--cache-mb" => {
                cli.cache_mb = value("--cache-mb")?
                    .parse()
                    .map_err(|_| "--cache-mb 需要正整数".to_string())?
            }
            "--json" => cli.json = Some(PathBuf::from(value("--json")?)),
            "--fsync" => cli.fsync = true,
            "--quiet" => cli.quiet = true,
            s if s.starts_with('-') => return Err(format!("未知选项 {s}\n{USAGE}")),
            s => cli.patches.push(PathBuf::from(s)),
        }
    }
    if cli.old_root.as_os_str().is_empty() {
        return Err("必须指定 --old-root".into());
    }
    if cli.out_root.as_os_str().is_empty() {
        return Err("必须指定 --out-root".into());
    }
    if cli.patches.is_empty() {
        return Err("至少需要一个 PATCH 文件".into());
    }
    if cli.cache_mb < 4 {
        cli.cache_mb = 4;
    }
    Ok(cli)
}

fn human(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut v = bytes as f64;
    let mut i = 0;
    while v >= 1024.0 && i + 1 < UNITS.len() {
        v /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{bytes} B")
    } else {
        format!("{v:.1} {}", UNITS[i])
    }
}

fn cmd_apply(args: &[String]) -> Result<i32, String> {
    let cli = parse_apply_args(args)?;
    let started = Instant::now();

    // Parse every container up front: gives the total workload for progress and
    // lets us reject overlapping output paths before touching any file.
    let mut groups: Vec<(PathBuf, head::GroupHead)> = Vec::with_capacity(cli.patches.len());
    for p in &cli.patches {
        let h = head::parse(p).map_err(|e| format!("{}: {e}", p.display()))?;
        if h.head_data_leftover != 0 {
            return Err(format!(
                "{}: head data 解析剩余 {} 字节",
                p.display(),
                h.head_data_leftover
            ));
        }
        groups.push((p.clone(), h));
    }
    // Output paths must not be produced twice (would be a race between workers).
    {
        let mut seen: std::collections::HashMap<&str, &PathBuf> = std::collections::HashMap::new();
        for (p, h) in &groups {
            for r in &h.new_refs {
                if let Some(prev) = seen.insert(r.path.as_str(), p) {
                    return Err(format!(
                        "输出路径重复: {} 同时出现在 {} 和 {}",
                        r.path,
                        prev.display(),
                        p.display()
                    ));
                }
            }
        }
    }

    let total_new: u64 = groups.iter().map(|(_, h)| h.new_data_size()).sum();
    let cpu = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);
    let workers = if cli.workers > 0 {
        cli.workers
    } else {
        cpu.min(groups.len()).max(1)
    };

    if !cli.quiet {
        eprintln!(
            "krpatch: {} 个分组，共 {} 待生成，并发 {}{}",
            groups.len(),
            human(total_new),
            workers,
            if workers > 1 { "" } else { "（单分组只能串行）" }
        );
    }

    let counter = Arc::new(AtomicU64::new(0));
    let opts = apply::ApplyOptions {
        old_root: cli.old_root.clone(),
        out_root: cli.out_root.clone(),
        cache_bytes: cli.cache_mb << 20,
        fsync: cli.fsync,
        counter: counter.clone(),
    };

    let stop = Arc::new(AtomicBool::new(false));
    let progress = if cli.quiet {
        None
    } else {
        let counter = counter.clone();
        let stop = stop.clone();
        Some(std::thread::spawn(move || {
            let mut last_done = 0u64;
            while !stop.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_millis(500));
                let done = counter.load(Ordering::Relaxed);
                let pct = if total_new > 0 {
                    done as f64 * 100.0 / total_new as f64
                } else {
                    100.0
                };
                let speed = (done - last_done.min(done)) as f64 / 0.5;
                last_done = done;
                eprint!(
                    "\r  {:5.1}%  {} / {}  {}/s",
                    pct,
                    human(done),
                    human(total_new),
                    human(speed as u64),
                );
                let _ = std::io::stderr().flush();
            }
        }))
    };

    let next = AtomicUsize::new(0);
    let results: Mutex<Vec<(usize, Result<apply::GroupOutcome, String>)>> =
        Mutex::new(Vec::with_capacity(groups.len()));
    std::thread::scope(|scope| {
        for _ in 0..workers {
            scope.spawn(|| loop {
                let i = next.fetch_add(1, Ordering::Relaxed);
                let Some((path, h)) = groups.get(i) else {
                    break;
                };
                let r = apply::apply_head(path, h, &opts);
                results.lock().unwrap().push((i, r));
            });
        }
    });
    stop.store(true, Ordering::Relaxed);
    if let Some(t) = progress {
        let _ = t.join();
        eprintln!();
    }

    let mut results = results.into_inner().unwrap();
    results.sort_by_key(|(i, _)| *i);

    for (_, r) in &results {
        match r {
            Ok(o) if !cli.quiet => eprintln!(
                "  [{}] {} -> {} 个文件, {:.2}s, {}/s",
                o.group,
                human(o.new_data_size),
                o.files.len(),
                o.secs,
                human(o.throughput() as u64)
            ),
            Ok(_) => {}
            Err(e) => eprintln!("  [失败] {e}"),
        }
    }

    let ok = results.iter().all(|(_, r)| r.is_ok());
    let json = build_json(&results, workers, cli.cache_mb, started.elapsed().as_secs_f64());
    match &cli.json {
        Some(path) => {
            std::fs::write(path, json).map_err(|e| format!("写 {} 失败: {e}", path.display()))?;
        }
        None => println!("{json}"),
    }
    if !cli.quiet {
        eprintln!(
            "krpatch: {} ({:.2}s)",
            if ok { "完成" } else { "有分组失败" },
            started.elapsed().as_secs_f64()
        );
    }
    Ok(if ok { 0 } else { 1 })
}

fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn build_json(
    results: &[(usize, Result<apply::GroupOutcome, String>)],
    workers: usize,
    cache_mb: usize,
    total_secs: f64,
) -> String {
    let ok = results.iter().all(|(_, r)| r.is_ok());
    let mut s = String::new();
    s.push_str("{\n");
    s.push_str(&format!("  \"ok\": {ok},\n"));
    s.push_str(&format!("  \"workers\": {workers},\n"));
    s.push_str(&format!("  \"cache_mb\": {cache_mb},\n"));
    s.push_str(&format!("  \"total_secs\": {total_secs:.3},\n"));
    s.push_str("  \"groups\": [\n");
    for (i, (_, r)) in results.iter().enumerate() {
        match r {
            Ok(o) => {
                s.push_str("    {\n");
                s.push_str(&format!("      \"group\": {},\n", json_str(&o.group)));
                s.push_str(&format!(
                    "      \"patch\": {},\n",
                    json_str(&o.patch.display().to_string())
                ));
                s.push_str("      \"ok\": true,\n");
                s.push_str(&format!("      \"old_size\": {},\n", o.old_data_size));
                s.push_str(&format!("      \"new_size\": {},\n", o.new_data_size));
                s.push_str(&format!("      \"secs\": {:.3},\n", o.secs));
                s.push_str("      \"files\": [\n");
                for (j, f) in o.files.iter().enumerate() {
                    s.push_str(&format!(
                        "        {{\"path\": {}, \"size\": {}, \"md5\": {}}}{}\n",
                        json_str(&f.rel),
                        f.size,
                        json_str(&f.md5),
                        if j + 1 == o.files.len() { "" } else { "," }
                    ));
                }
                s.push_str("      ]\n");
                s.push_str("    }");
            }
            Err(e) => {
                s.push_str("    {\n");
                s.push_str("      \"ok\": false,\n");
                s.push_str(&format!("      \"error\": {}\n", json_str(e)));
                s.push_str("    }");
            }
        }
        s.push_str(if i + 1 == results.len() { "\n" } else { ",\n" });
    }
    s.push_str("  ]\n}\n");
    s
}

fn cmd_info(args: &[String]) -> Result<i32, String> {
    if args.is_empty() {
        return Err("至少需要一个 PATCH 文件".into());
    }
    for a in args {
        let p = PathBuf::from(a);
        let h = head::parse(&p).map_err(|e| format!("{}: {e}", p.display()))?;
        println!("{}", p.display());
        println!(
            "  {} / {} / {}  head data {} -> {} 字节（解压后 {}，余量 {}）",
            h.version,
            h.compress_type,
            h.checksum_type,
            h.head.head_data_compressed_size,
            h.head.head_data_size,
            h.head_data_len,
            h.head_data_leftover
        );
        println!(
            "  old: {} refs / {}  new: {} refs / {}  samePairs {} ({})  exec {}",
            h.old_refs.len(),
            human(h.old_data_size()),
            h.new_refs.len(),
            human(h.new_data_size()),
            h.head.same_file_pair_count,
            human(h.head.same_file_size),
            h.head.new_execute_count
        );
        println!(
            "  目录标记 old/new = {}/{}  外部数据 reserved/private/extern = {}/{}/{}",
            h.head.old_path_is_dir,
            h.head.new_path_is_dir,
            h.head.private_reserved_data_size,
            h.head.private_extern_data_size,
            h.head.extern_data_size
        );
        println!(
            "  hdiff 段: offset {} 长度 {}  校验: {}",
            h.hdiff_offset,
            human(h.hdiff_len),
            h.head_checksums
                .iter()
                .map(|c| streams::hex32(c))
                .collect::<Vec<_>>()
                .join(" ")
        );
        for r in &h.old_refs {
            println!("    old  {:>12}  {}", r.size, r.path);
        }
        for r in &h.new_refs {
            println!(
                "    new  {:>12}  {}  extra={:#x}",
                r.size, r.path, r.extra
            );
        }
    }
    Ok(0)
}
