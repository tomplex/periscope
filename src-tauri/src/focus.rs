// Whether the dashboard window has focus, and since when it hasn't — kept by
// main.rs's window events. Read off the main thread by notification banners
// (quiet while focused; will_present runs on a thread UNUserNotificationCenter
// chooses) and by the recycle monitor (recycle once periscope is left). One
// mutex, so a reader never pairs "unfocused" with a stale time.

use std::sync::Mutex;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, PartialEq)]
pub enum Focus {
    Unknown, // launch, before the first window event
    Focused,
    UnfocusedSince(Instant),
}

static FOCUS: Mutex<Focus> = Mutex::new(Focus::Unknown);

impl Focus {
    /// How long periscope has been unfocused; None while focused or not yet known.
    pub fn unfocused_for(self) -> Option<Duration> {
        match self {
            Focus::UnfocusedSince(t) => Some(t.elapsed()),
            _ => None,
        }
    }
}

/// One locked read: a reader that needs more than one answer asks this snapshot.
pub fn focus() -> Focus {
    *FOCUS.lock().unwrap_or_else(|e| e.into_inner())
}

/// Only a real change moves the clock: a recycle's Destroyed after an earlier
/// Focused(false) keeps the unfocused time running.
pub fn set_focused(focused: bool) {
    let mut f = FOCUS.lock().unwrap_or_else(|e| e.into_inner());
    *f = match (*f, focused) {
        (_, true) => Focus::Focused,
        (Focus::UnfocusedSince(t), false) => Focus::UnfocusedSince(t),
        (_, false) => Focus::UnfocusedSince(Instant::now()),
    };
}

pub fn is_focused() -> bool {
    focus() == Focus::Focused
}

#[cfg(test)]
mod tests {
    use super::*;

    // One test: the state is process-global and cargo runs tests in parallel.
    #[test]
    fn tracks_focus_and_how_long_it_has_been_lost() {
        assert_eq!(focus().unfocused_for(), None); // Unknown at start
        assert!(!is_focused());

        set_focused(true);
        assert!(is_focused());
        assert_eq!(focus().unfocused_for(), None);

        set_focused(false);
        assert!(!is_focused());
        std::thread::sleep(Duration::from_millis(30));
        let first = focus().unfocused_for().expect("unfocused");
        assert!(first >= Duration::from_millis(30));

        set_focused(false); // a recycle's Destroyed
        assert!(focus().unfocused_for().expect("unfocused") >= first);

        set_focused(true);
        assert_eq!(focus().unfocused_for(), None);
    }
}
