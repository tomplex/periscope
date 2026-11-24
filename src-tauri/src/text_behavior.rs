// Disable macOS automatic text "help" inside the WKWebView.
//
// Left on, the system defaults apply to every periscope input: an
// autocorrect bubble that rewrites what you typed ("peris" → "perks"), an
// inline-prediction bubble that steals focus, auto-capitalize, and smart
// quote/dash/period substitution — all hostile in a terminal-first
// dashboard where inputs are commands, branch names, and paths.
//
// Two key families, because two readers:
// - `Web*` — WebKit's own switches (UIProcess/mac/TextCheckerMac.mm). It
//   asks AppKit only when its key is absent, and AppKit's answer ignores
//   the `NS*` values below, so the `NS*` keys alone leave autocorrect on.
// - `NS*` — AppKit's switches, for what WebKit has no key of its own for
//   (capitalization, completion, inline prediction, period substitution).
//
// Written to the application domain (not the registration domain) so it
// beats NSGlobalDomain, where System Settings turns these on. Called
// before the first webview exists: WebKit reads its keys once per process.

use objc2_foundation::{NSString, NSUserDefaults};

pub fn disable_automatic_text_behaviors() {
    let defaults = NSUserDefaults::standardUserDefaults();
    for key in [
        "WebAutomaticSpellingCorrectionEnabled",
        "WebAutomaticTextReplacementEnabled",
        "WebAutomaticQuoteSubstitutionEnabled",
        "WebAutomaticDashSubstitutionEnabled",
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
