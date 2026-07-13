mod capture_launcher;

use std::sync::Arc;
use tauri::webview::{NewWindowResponse, WebviewWindowBuilder};
use tauri::{AppHandle, Manager};

#[tauri::command]
async fn open_webview(
    app: AppHandle,
    url: String,
    id: String,
    title: String,
    incognito: bool,
) -> Result<(), String> {
    // Sanitize ID to remove non-alphanumeric characters
    let sanitized_id: String = id.chars().filter(|c| c.is_alphanumeric()).collect();
    let mut new_id = format!("foundry{}", sanitized_id);

    // Check if a window with this label already exists
    if app.webview_windows().contains_key(&new_id) {
        let random_number = rand::random::<u32>() % 1000000;
        new_id = format!("foundry{}{}", sanitized_id, random_number);
    }

    WebviewWindowBuilder::new(
        &app,
        &new_id,
        tauri::WebviewUrl::External(url.parse().map_err(|e| format!("Invalid URL: {}", e))?),
    )
    .title(format!("Foundry VTT - {}", title))
    .incognito(incognito)
    .inner_size(1280.0, 800.0)
    .focused(true)
    .center()
    .devtools(true)
    .disable_drag_drop_handler()
    .zoom_hotkeys_enabled(true)
    .maximizable(true)
    .resizable(true)
    .minimizable(true)
    .closable(true)
    .on_new_window(|_url, _features| {
        // Allow popup windows to open
        NewWindowResponse::Allow
    })
    .build()
    .map_err(|e| format!("Failed to create webview: {}", e))?;

    Ok(())
}

// WebView2's underlying browser process (msedgewebview2.exe) can persist in the
// background after the app exits. If one lingers, the next launch silently reuses
// it instead of starting fresh — which means WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
// (including --remote-debugging-port) never takes effect, and capture.py polls
// forever for a CDP port that was never opened. Force-kill any stale instance
// tied to this app's WebView2 data folder before we set the env var, so the
// next one created is guaranteed to pick it up.
#[cfg(target_os = "windows")]
fn kill_stale_webview2_processes() {
    use std::os::windows::process::CommandExt;
    use std::process::Command;

    let script = r#"Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" | Where-Object { $_.CommandLine -match '\\flc\\EBWebView' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"#;
    let _ = Command::new("powershell")
        .args(["-NoProfile", "-WindowStyle", "Hidden", "-Command", script])
        .creation_flags(0x08000000) // CREATE_NO_WINDOW
        .output();
}

#[cfg(not(mobile))]
pub fn run() {
    #[cfg(target_os = "windows")]
    unsafe {
        kill_stale_webview2_processes();
        std::env::set_var(
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
            "--force-high-performance-gpu --allow-insecure-localhost --allow-running-insecure-content --block-new-web-contents=false --remote-debugging-port=9222",
        );
    }

    // WebKitGTK white screen bug workaround
    // https://github.com/khoj-ai/pipali/pull/44
    #[cfg(target_os = "linux")]
    unsafe {
        for var in ["WEBKIT_DISABLE_DMABUF_RENDERER", "WEBKIT_DISABLE_COMPOSITING_MODE"] {
            if std::env::var_os(var).is_none() {
                std::env::set_var(var, "1");
            }
        }
    }

    let capture_handle = capture_launcher::new_handle();
    let handle_for_setup = Arc::clone(&capture_handle);

    tauri::Builder::default()
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(capture_handle)
        .setup(move |app| {
            capture_launcher::spawn(&handle_for_setup, app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![open_webview])
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                capture_launcher::kill(app.state::<capture_launcher::CaptureHandle>().inner());
            }
        });
}
