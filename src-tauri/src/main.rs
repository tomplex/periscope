// Prevents an extra console window from opening on Windows; harmless on macOS.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::Manager;
use tauri_plugin_window_state::StateFlags;

mod focus;
mod notifications;
mod recycle;
mod text_behavior;
mod zoom;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        // Everything but VISIBLE: the plugin restores state on every window it
        // sees, each recycle's rebuild included, and its VISIBLE restore calls
        // set_focus (activateIgnoringOtherApps: YES), pulling you out of the
        // app you're in. tauri.conf.json already shows the startup window.
        .plugin(
            tauri_plugin_window_state::Builder::default()
                .with_state_flags(StateFlags::all() - StateFlags::VISIBLE)
                .build(),
        )
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            // macOS expects an App menu with at least Quit; without a menu
            // the system Cmd-Q still works but no other accelerators (Cmd-R,
            // Cmd-C/V, etc.) get bound to webview actions. Without an Edit
            // submenu, paste in input fields silently fails.
            let handle = app.handle();
            notifications::init(handle);
            text_behavior::disable_automatic_text_behaviors();
            let app_submenu = SubmenuBuilder::new(handle, "Periscope")
                .item(&PredefinedMenuItem::about(handle, Some("Periscope"), None)?)
                .separator()
                .services()
                .separator()
                .hide()
                .hide_others()
                .show_all()
                .separator()
                .quit()
                .build()?;
            let edit_submenu = SubmenuBuilder::new(handle, "Edit")
                .undo()
                .redo()
                .separator()
                .cut()
                .copy()
                .paste()
                .select_all()
                .build()?;
            let view_submenu = SubmenuBuilder::new(handle, "View")
                .item(
                    // Shift+Cmd+R, NOT Cmd+R: a native menu accelerator is
                    // consumed by the menu before the webview sees the key, so
                    // binding Cmd+R here would make the in-app refresh
                    // (static/src/keys.js — refresh the focused document or
                    // repaint the pane mirror) permanently dead in the .app
                    // while still working in a browser tab.
                    &MenuItemBuilder::with_id("reload", "Reload")
                        .accelerator("Shift+CmdOrCtrl+R")
                        .build(handle)?,
                )
                .item(
                    &MenuItemBuilder::with_id("devtools", "Toggle Developer Tools")
                        .accelerator("Alt+CmdOrCtrl+I")
                        .build(handle)?,
                )
                .separator()
                // ⌘= rather than ⌘+ (⌘⇧=): the unshifted key is what Safari
                // and Chrome users press, and it's the one AppKit matches
                // against a "=" key equivalent.
                .item(
                    &MenuItemBuilder::with_id("zoom_in", "Zoom In")
                        .accelerator("CmdOrCtrl+=")
                        .build(handle)?,
                )
                .item(
                    &MenuItemBuilder::with_id("zoom_out", "Zoom Out")
                        .accelerator("CmdOrCtrl+-")
                        .build(handle)?,
                )
                .item(
                    &MenuItemBuilder::with_id("zoom_reset", "Actual Size")
                        .accelerator("CmdOrCtrl+0")
                        .build(handle)?,
                )
                .separator()
                .item(
                    // Full WebContent process replacement — the only reset for
                    // WKWebView's trusted-input graphics-region leak (a plain
                    // reload reuses the process and frees nothing; see recycle.rs).
                    &MenuItemBuilder::with_id("recycle", "Recycle Webview")
                        .build(handle)?,
                )
                .build()?;
            let window_submenu = SubmenuBuilder::new(handle, "Window")
                .minimize()
                .close_window()
                .build()?;
            let menu = MenuBuilder::new(handle)
                .items(&[&app_submenu, &edit_submenu, &view_submenu, &window_submenu])
                .build()?;
            app.set_menu(menu)?;
            if let Some(window) = app.get_webview_window("main") {
                zoom::init(&window);
            }
            recycle::start_monitor(&handle.clone());
            Ok(())
        })
        .on_menu_event(|app, event| {
            if event.id().as_ref() == "recycle" {
                recycle::recycle_now(app);
                return;
            }
            let Some(window) = app.get_webview_window("main") else { return };
            match event.id().as_ref() {
                "reload" => {
                    let _ = window.eval("location.reload()");
                }
                "zoom_in" => zoom::zoom_in(&window),
                "zoom_out" => zoom::zoom_out(&window),
                "zoom_reset" => zoom::reset(&window),
                "devtools" => {
                    #[cfg(debug_assertions)]
                    if window.is_devtools_open() {
                        window.close_devtools();
                    } else {
                        window.open_devtools();
                    }
                }
                _ => {}
            }
        })
        // focus.rs keeps the window's focus for readers off the main thread.
        // A webview recycle destroys the focused window without a
        // Focused(false) first — tao's windowWillClose drops its delegate
        // before that fires — so Destroyed resets it too.
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }
            match event {
                tauri::WindowEvent::Focused(focused) => focus::set_focused(*focused),
                tauri::WindowEvent::Destroyed => focus::set_focused(false),
                _ => {}
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|_app, event| {
            // Between destroy and rebuild inside a recycle the app has zero
            // windows, which macOS/tauri reads as "quit". Veto it.
            if let tauri::RunEvent::ExitRequested { api, .. } = &event {
                if recycle::RECYCLING.load(std::sync::atomic::Ordering::SeqCst) {
                    api.prevent_exit();
                }
            }
        });
}
