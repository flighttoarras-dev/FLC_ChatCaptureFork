use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Manager};

pub type CaptureHandle = Arc<Mutex<Option<Child>>>;

pub fn new_handle() -> CaptureHandle {
    Arc::new(Mutex::new(None))
}

fn find_python() -> Option<String> {
    for candidate in ["python", "python3"] {
        let ok = Command::new(candidate)
            .arg("--version")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);
        if ok {
            return Some(candidate.to_string());
        }
    }
    None
}

/// Find the directory containing capture.py and capture_config.json.
/// In production they are bundled as resources next to the exe.
/// In dev mode the exe is buried in target/debug/ so we walk up until we find them.
fn find_companion_dir(app: &AppHandle) -> Option<PathBuf> {
    if let Ok(res_dir) = app.path().resource_dir() {
        if res_dir.join("capture.py").exists() {
            return Some(res_dir);
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent()?.to_path_buf();
        for _ in 0..6 {
            if dir.join("capture.py").exists() {
                return Some(dir);
            }
            match dir.parent() {
                Some(p) => dir = p.to_path_buf(),
                None => break,
            }
        }
    }
    None
}

/// Returns (auto_launch, show_console). Defaults: (true, false).
fn read_config(dir: &PathBuf) -> (bool, bool) {
    let Ok(content) = std::fs::read_to_string(dir.join("capture_config.json")) else {
        return (true, false);
    };
    let Ok(config) = serde_json::from_str::<serde_json::Value>(&content) else {
        return (true, false);
    };
    let auto_launch   = config.get("auto_launch").and_then(|v| v.as_bool()).unwrap_or(true);
    let show_console  = config.get("show_console").and_then(|v| v.as_bool()).unwrap_or(false);
    (auto_launch, show_console)
}

pub fn spawn(handle: &CaptureHandle, app: &AppHandle) {
    if handle.lock().unwrap().is_some() {
        return;
    }

    let dir = match find_companion_dir(app) {
        Some(d) => d,
        None => {
            eprintln!("[capture] capture.py not found — will not run");
            return;
        }
    };

    let (auto_launch, show_console) = read_config(&dir);

    if !auto_launch {
        eprintln!("[capture] auto_launch is false in config — skipping");
        return;
    }

    let python = match find_python() {
        Some(p) => p,
        None => {
            use tauri_plugin_dialog::DialogExt;
            app.dialog()
                .message(
                    "capture.py requires Python to be installed.\n\n\
                     Install Python from python.org, then restart FLC \
                     to enable automatic session capture.",
                )
                .title("Session Capture — Python Not Found")
                .show(|_| {});
            eprintln!("[capture] Python not found — capture.py will not run");
            return;
        }
    };

    let script = dir.join("capture.py");

    let mut cmd = Command::new(&python);
    cmd.arg(&script).current_dir(&dir);
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        if !show_console {
            cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
    }

    match cmd.spawn()
    {
        Ok(child) => {
            let pid = child.id();
            *handle.lock().unwrap() = Some(child);
            eprintln!("[capture] capture.py started (PID: {})", pid);
        }
        Err(e) => {
            eprintln!("[capture] Failed to spawn capture.py: {}", e);
        }
    }
}

pub fn kill(handle: &CaptureHandle) {
    if let Ok(mut guard) = handle.lock() {
        if let Some(mut child) = guard.take() {
            // Signal capture.py to flush and exit cleanly before force-killing
            let sentinel = std::env::temp_dir().join("flc_capture_shutdown");
            let _ = std::fs::write(&sentinel, b"");

            // Give it up to 3 seconds to exit (covers one poll cycle + distill)
            let deadline = std::time::Instant::now()
                + std::time::Duration::from_secs(3);
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => {
                        eprintln!("[capture] capture.py exited cleanly");
                        break;
                    }
                    Ok(None) if std::time::Instant::now() < deadline => {
                        std::thread::sleep(std::time::Duration::from_millis(100));
                    }
                    _ => {
                        let _ = child.kill();
                        let _ = child.wait();
                        eprintln!("[capture] capture.py force-killed after grace period");
                        break;
                    }
                }
            }

            // Remove the sentinel unconditionally: if capture.py already
            // unlinked it itself, this is a harmless no-op. If it exited (or
            // was force-killed) without ever noticing it - e.g. it crashed
            // for an unrelated reason right as this ran - leaving it behind
            // would make the *next* launch see a stale sentinel and shut
            // itself down immediately, before ever trying to reach Foundry.
            let _ = std::fs::remove_file(&sentinel);
        }
    }
}
