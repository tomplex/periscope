// Disable macOS automatic text "help" inside the WKWebView.
//
// WKWebView's text fields run through the system text input machinery,
// which reads these automatic-behavior switches from the app's own
// NSUserDefaults domain. Left on, they inflict the system defaults on
// every periscope input: an inline-prediction bubble that pops up and
// steals focus, autocorrect that rewrites what you typed, auto-capitalize,
// and smart quote/dash/period substitution — all hostile in a terminal-
// first dashboard where inputs are commands, branch names, and paths.
//
// Written to the application domain (not the registration domain) so it
// beats the user's NSGlobalDomain settings, which is where these are
// turned on system-wide. Idempotent: re-asserts the same values each
// launch.

use objc2_foundation::{NSString, NSUserDefaults};

pub fn disable_automatic_text_behaviors() {
    let defaults = NSUserDefaults::standardUserDefaults();
    for key in [
        "NSAutomaticSpellingCorrectionEnabled",
        "NSAutomaticCapitalizationEnabled",
        "NSAutomaticTextCompletionEnabled",
        "NSAutomaticInlinePredictionEnabled",
        "NSAutomaticQuoteSubstitutionEnabled",
        "NSAutomaticDashSubstitutionEnabled",
        "NSAutomaticPeriodSubstitutionEnabled",
        "NSAutomaticTextReplacementEnabled",
    ] {
        defaults.setBool_forKey(false, &NSString::from_str(key));
    }
}
