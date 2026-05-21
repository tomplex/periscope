// Native macOS notifications via UNUserNotificationCenter (objc2).
//
// tauri-plugin-notification can't do this job: its desktop backend
// (notify-rust) is fire-and-forget — no click callback on macOS.
// UNUserNotificationCenter is event-driven: a delegate's
// didReceiveNotificationResponse fires when the user clicks a banner.
//
// The pane target rides in the notification's identifier
// ("periscope-route|{seq}|{target}") and is recovered there. The webview
// is a remote origin (localhost:8765) and can't invoke Rust commands, so
// both directions cross the Tauri event bus — the IPC surface core:default
// already grants it: "periscope:notify" carries a banner in,
// "periscope:notification-clicked" carries a click back out.

use std::sync::atomic::{AtomicUsize, Ordering};

use block2::{DynBlock, RcBlock};
use objc2::rc::{autoreleasepool, Retained};
use objc2::runtime::{Bool, ProtocolObject};
use objc2::{define_class, msg_send, AnyThread, DefinedClass};
use objc2_foundation::{NSError, NSObject, NSObjectProtocol, NSString};
use objc2_user_notifications::{
    UNAuthorizationOptions, UNMutableNotificationContent, UNNotification,
    UNNotificationPresentationOptions, UNNotificationRequest, UNNotificationResponse,
    UNUserNotificationCenter, UNUserNotificationCenterDelegate,
};
use tauri::{AppHandle, Emitter, Listener};

/// Webview → Rust: post this banner.
const NOTIFY_EVENT: &str = "periscope:notify";
/// Rust → webview: the pane behind this banner was clicked.
const CLICK_EVENT: &str = "periscope:notification-clicked";
/// Notification identifier shape. The seq keeps ids unique (a reused id
/// silently replaces the prior banner); the pane target is the tail,
/// recovered verbatim on click — `split_once` keeps any '|' inside it.
const ID_PREFIX: &str = "periscope-route|";

static SEQ: AtomicUsize = AtomicUsize::new(0);

#[derive(serde::Deserialize)]
struct NotifyRequest {
    title: String,
    body: String,
    target: String,
}

struct Ivars {
    app: AppHandle,
}

define_class!(
    #[unsafe(super(NSObject))]
    #[name = "PeriscopeUNDelegate"]
    #[ivars = Ivars]
    struct Delegate;

    unsafe impl NSObjectProtocol for Delegate {}

    unsafe impl UNUserNotificationCenterDelegate for Delegate {
        // Fired when the user clicks (or actions) a delivered notification.
        #[unsafe(method(userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:))]
        fn did_receive(
            &self,
            _center: &UNUserNotificationCenter,
            response: &UNNotificationResponse,
            completion_handler: &DynBlock<dyn Fn()>,
        ) {
            let id = response.notification().request().identifier().to_string();
            if let Some(target) = id
                .strip_prefix(ID_PREFIX)
                .and_then(|rest| rest.split_once('|'))
                .map(|(_seq, target)| target)
            {
                let _ = self.ivars().app.emit(CLICK_EVENT, target);
            }
            completion_handler.call(());
        }

        // Fired when a notification arrives while Periscope is frontmost.
        // Present it explicitly, else macOS swallows the banner.
        #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
        fn will_present(
            &self,
            _center: &UNUserNotificationCenter,
            _notification: &UNNotification,
            completion_handler: &DynBlock<dyn Fn(UNNotificationPresentationOptions)>,
        ) {
            completion_handler.call((
                UNNotificationPresentationOptions::Banner
                    | UNNotificationPresentationOptions::Sound,
            ));
        }
    }
);

impl Delegate {
    fn new(app: AppHandle) -> Retained<Self> {
        let this = Self::alloc().set_ivars(Ivars { app });
        unsafe { msg_send![super(this), init] }
    }
}

/// Install the notification delegate, request authorization, and start
/// listening for banner requests from the webview. Call once, from the
/// Tauri `setup` hook of the bundled app — UNUserNotificationCenter throws
/// if used from a non-bundled binary.
pub fn init(app: &AppHandle) {
    let center = UNUserNotificationCenter::currentNotificationCenter();

    let delegate = Delegate::new(app.clone());
    center.setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
    // setDelegate keeps only a weak reference — leak our single delegate
    // so it lives for the app's lifetime and keeps receiving clicks.
    std::mem::forget(delegate);

    let auth = RcBlock::new(|granted: Bool, _err: *mut NSError| {
        if !granted.as_bool() {
            eprintln!("periscope: notification permission not granted");
        }
    });
    center.requestAuthorizationWithOptions_completionHandler(
        UNAuthorizationOptions::Alert | UNAuthorizationOptions::Sound,
        &auth,
    );

    app.listen(NOTIFY_EVENT, |event| {
        match serde_json::from_str::<NotifyRequest>(event.payload()) {
            Ok(req) => send(&req.title, &req.body, &req.target),
            Err(e) => eprintln!("periscope: bad notify payload: {e}"),
        }
    });
}

fn send(title: &str, body: &str, target: &str) {
    autoreleasepool(|_| {
        let center = UNUserNotificationCenter::currentNotificationCenter();

        let content = UNMutableNotificationContent::new();
        content.setTitle(&NSString::from_str(title));
        content.setBody(&NSString::from_str(body));

        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let ident = NSString::from_str(&format!("{ID_PREFIX}{seq}|{target}"));
        let request =
            UNNotificationRequest::requestWithIdentifier_content_trigger(&ident, &content, None);

        let done = RcBlock::new(|err: *mut NSError| {
            if !err.is_null() {
                eprintln!("periscope: notification delivery failed");
            }
        });
        center.addNotificationRequest_withCompletionHandler(&request, Some(&done));
    });
}
