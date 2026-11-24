# Screenshots

Sizes are App Store Connect's and Play Console's as of November 2026. Take
every shot on the **demo deployment** (or a deployment seeded the same way),
signed in as a demo visitor, so nothing real appears: no customer names, no
real email addresses, no API keys. The status bar should read 9:41 with full
signal and battery (Simulator: `xcrun simctl status_bar booted override
--time 9:41 --batteryLevel 100 --cellularBars 4`).

## Required sizes

| Store | Device class | Size (portrait) | Required? | Capture on |
|---|---|---|---|---|
| App Store | iPhone 6.9" | 1320 x 2868 (or 1290 x 2796, 1260 x 2736) | **Yes** (the largest iPhone class; smaller iPhones are scaled from it) | iPhone 17 Pro Max simulator |
| App Store | iPhone 6.5" | 1284 x 2778 or 1242 x 2688 | Only if no 6.9" set is given; skip when 6.9" is provided | iPhone 11 Pro Max simulator |
| App Store | iPad 13" | 2064 x 2752 or 2048 x 2732 | **Yes**: the app runs on iPad (`TARGETED_DEVICE_FAMILY = "1,2"`) | iPad Pro 13-inch (M5) simulator |
| Google Play | Phone | 1080 x 1920 or larger, 16:9 or 9:16, each side 320 to 3840 px | **Yes**, 2 to 8 (4 or more recommended for featuring) | Pixel 9 Pro emulator |
| Google Play | 7" and 10" tablet | 1200 x 1920 / 1600 x 2560 class | Optional; only to be featured for tablets | |

1 to 10 screenshots per class on the App Store; 2 to 8 per class on Play.
PNG or JPEG, no alpha on the App Store. Up to three App Previews (15 to 30 s
videos) are optional.

**Decision for the product owner:** keeping iPad support means a set of iPad
screenshots and an iPad review pass (App Review tests on iPad too, and a
layout problem there is a 2.1 or 4.0 rejection). Dropping iPad means
`TARGETED_DEVICE_FAMILY = "1"` in both app configurations, which is
irreversible for users who already installed it. Recommended: keep iPad, the
layout is Ionic's and adapts.

## The shot list (same order on both stores)

| # | Screen | What it shows | Caption (if captions are added) |
|---|---|---|---|
| 1 | A conversation, a reply streaming with its steps | The core of the app | Your AI agents, working for you |
| 2 | Chats list, Pinned and folders | Several agents with avatars | Every agent in one place |
| 3 | An in-line approval in a conversation | Approve / Decline | Approve what matters with one tap |
| 4 | Cortex list with ratings | Ideas, prototypes, projects | See what your agents are building |
| 5 | Voice note with its transcript | Hold to talk | Talk instead of typing |
| 6 | Inbox with approvals and an alert | | Nothing slips through |
| 7 | Widgets on the Home Screen (iPhone only) | Agent and Approvals widgets | Your agents on your Home Screen |
| 8 | Connect screen with Try the demo | | Connect to your Olano, or try the demo |

Use the same set in light mode; one dark-mode shot (Settings, Appearance,
Dark) among them is a nice touch but not required.

## Producing them

- The end-to-end suite already walks every one of these screens in an
  iPhone-sized browser and saves a picture of each when
  `OLANO_MOBILE_SHOTS=<dir>` is set (see the README). Those are 1179 x 2556
  browser renders, good for drafting the set and the captions, but **not** for
  upload: the stores want device-size captures, and the browser shows no
  native status bar or widgets.
- Final shots: run the app in the iOS Simulator and the Android emulator (a
  Mac with Xcode for iOS; Android Studio for Android) against the demo
  deployment, and capture with `xcrun simctl io booted screenshot <file>.png`
  and `adb exec-out screencap -p > <file>.png`.
- Keep the source captures and any captioned versions outside the repository
  (they are large); record where they live in `SUBMISSION.md`'s last step.
