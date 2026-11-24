# Submitting Olano to the App Store and Google Play

The step-by-step a person follows. Everything that can live in the repository
already does; what is left needs an Apple Developer account, a Mac or the
TestFlight workflow, and a Play Console account. Tick the boxes in a copy of
this file in the release ticket, not here.

Files in this folder: `LISTING.md` (copy), `REVIEW_NOTES.md` (review notes and
App access), `PRIVACY.md` (App Privacy and Data safety), `COMPLIANCE.md`
(accounts, age rating, export), `SCREENSHOTS.md`, `DEMO.md` (the review
demo).

## 0. Before either store

- [ ] Decide the open points: the demo box's address and owner, the demo's
      model and credit cap (`DEMO.md`, end); keep iPad or not
      (`SCREENSHOTS.md`); the age rating (`COMPLIANCE.md`); confirm the
      privacy reading in `PRIVACY.md` with whoever owns the privacy policy.
- [ ] The runtime release that contains `POST /api/auth/demo` is on `:latest`,
      and the cloud web app that serves `/api/v1/mobile/config` is deployed.
      Check: `curl -s https://cloud.olano.ai/api/v1/mobile/config` shows
      `"enabled": true` and the demo URL.
- [ ] The demo deployment is provisioned and hardened (`DEMO.md` sections 1
      and 2) and `curl -si -X POST https://demo.olano.run/api/auth/demo`
      answers 200.
- [ ] https://olano.ai/privacy, https://olano.ai/support and
      https://olano.ai/account-deletion load (`COMPLIANCE.md` says what the
      last one must say).
- [ ] Version: `package.json` `version`, `android/app/build.gradle`
      `versionName` and the Xcode `MARKETING_VERSION` agree (`npm test` checks).
      Build numbers come from CI (below).
- [ ] `cd mobile && npm ci && npm test && npm run build`: green, including
      `store readiness` and the version check.
- [ ] On a real iPhone and a real Android phone, from a fresh install: connect
      to a real deployment, sign in, chat, photo, voice note, notification;
      then sign out, Switch deployment, Try the demo. Rotate to landscape and
      try an iPad (or the iPad simulator) if iPad stays supported.

## 1. Apple

### 1.1 Account and identifiers (once)

- [ ] Apple Developer Program membership for the company (an Organization
      account, D-U-N-S number), so the seller shown is "Olano", not a person.
- [ ] In Certificates, Identifiers & Profiles, Identifiers (automatic signing
      registers these on the first archive, but check):
  - `ai.olano.app`: capabilities **Push Notifications**, **App Groups**
    (`group.ai.olano.app`), Associated Domains not needed, **Siri** (App
    Intents need no entitlement; leave Siri off unless Xcode asks).
  - `ai.olano.app.share`: **App Groups** (`group.ai.olano.app`).
  - `ai.olano.app.widgets`: **App Groups** (`group.ai.olano.app`).
  - App Group `group.ai.olano.app` exists.
  - Keychain Sharing needs no registration (`$(AppIdentifierPrefix)ai.olano.app`).
- [ ] An App Store Connect API key with the **Admin** role (Users and Access,
      Integrations, App Store Connect API). Download the `.p8` once.
- [ ] Repository secrets for `.github/workflows/mobile-testflight.yml`:
      `APPLE_TEAM_ID`, `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8_BASE64`
      (`base64 -i AuthKey_X.p8 | tr -d '\n'`). Never commit the key.
- [ ] Push for production: the APNs `.p8` (Keys, a key with Apple Push
      Notifications service) with its Key ID and Team ID is in the cloud
      repository's secrets for the push relay (its push document). Without it
      the app installs but notifications never arrive.

### 1.2 The app record (once)

- [ ] App Store Connect, Apps, "+", New App: iOS, name **Olano**, primary
      language English (U.S.), bundle ID `ai.olano.app`, SKU `olano-ios`,
      full access.
- [ ] App Information: subtitle, category Productivity / Business, content
      rights (`COMPLIANCE.md`), age rating questionnaire (`COMPLIANCE.md`),
      privacy policy URL.
- [ ] App Privacy: answer exactly as `PRIVACY.md`, then Publish.
- [ ] Pricing and Availability: Free; all countries where the service is
      offered (check local AI and data rules before adding, for example
      China needs a separate filing and is best left out).

### 1.3 A build

- [ ] Actions tab, **Mobile TestFlight**, Run workflow on the release commit.
      It builds with Xcode on macOS, signs automatically with the API key,
      numbers the build `100 + run number` and uploads. Wait for Apple's
      processing email (10 to 30 minutes).
- [ ] If the archive fails on capabilities, add them under Identifiers (1.1)
      and run again; the workflow log is kept as an artifact on failure.
- [ ] Without the workflow, on a Mac: `cd mobile && npm ci && npm run sync:ios`,
      open `ios/App/App.xcodeproj`, scheme App, Any iOS Device, Product,
      Archive, Distribute App, App Store Connect, Upload.
- [ ] Export compliance: nothing to answer (`ITSAppUsesNonExemptEncryption`
      is false).
- [ ] The privacy report: in Xcode's Organizer, right-click the archive,
      Generate Privacy Report, and check it lists the three manifests and the
      data types in `PRIVACY.md` (Apple warns at upload if a required-reason
      API is undeclared).

### 1.4 TestFlight

- [ ] Internal testing: add the team, install with the TestFlight app, run the
      "Before either store" device checks on this build (push included: it is
      the first build signed for production APNs).
- [ ] Optional external testing (beta review, about a day): use the same
      review notes.

### 1.5 Submit for review

- [ ] Version page: screenshots (6.9" iPhone, 13" iPad), promotional text,
      description, keywords, support and marketing URLs, What's New
      (`LISTING.md`, `SCREENSHOTS.md`).
- [ ] Build: the TestFlight build tested above.
- [ ] App Review Information: contact; **Sign-in required** checked with
      `demo@olano.run` and the template password from the password manager;
      Notes from `REVIEW_NOTES.md`.
- [ ] Release: "Manually release this version" for the first release.
- [ ] Submit. While in review, keep the demo box up and watch its alerts; do
      not change `MOBILE_DEMO_*` on the cloud.

## 2. Google

### 2.1 Account (once)

- [ ] A Google Play Console **organization** account for the company
      (D-U-N-S verified). A personal account created after November 2023
      must run a closed test with at least 12 testers for 14 days before it
      can publish to production; an organization account is exempt.
- [ ] Firebase project with an Android app `ai.olano.app`; download
      `google-services.json` into `mobile/android/app/` on the build machine
      only (git-ignored). Without it the release has no push. The cloud relay
      needs the Firebase service-account JSON in its secrets.

### 2.2 Signing (once)

- [ ] Create the upload key outside the repository:
      `keytool -genkeypair -v -keystore olano-upload.jks -keyalg RSA -keysize 4096 -validity 10000 -alias upload`.
      Store the keystore and both passwords in the password manager.
- [ ] Copy `android/keystore.properties.example` to `android/keystore.properties`
      (git-ignored) and fill it in, or export `OLANO_UPLOAD_STORE_FILE`,
      `OLANO_UPLOAD_STORE_PASSWORD`, `OLANO_UPLOAD_KEY_ALIAS`,
      `OLANO_UPLOAD_KEY_PASSWORD`.
- [ ] On the first upload, accept **Play App Signing** (Google holds the app
      signing key; the upload key above can be reset if lost).

### 2.3 A build

- [ ] On a machine with JDK 21 and the Android SDK (platform 36):
      `cd mobile && npm ci && npm run sync:android && cd android && ./gradlew bundleRelease -PolanoVersionCode=<n>`,
      where `<n>` is higher than any code uploaded before (use the same
      `100 + run number` scheme as TestFlight, or a date like `2026111301`).
      The bundle is `app/build/outputs/bundle/release/app-release.aab`.
      (CI builds only a debug APK today; a release job would need the upload
      key as secrets, a follow-up.)
- [ ] Install the release build on a phone once (`bundletool` or an internal
      test) before uploading to production.

### 2.4 The app in Play Console

- [ ] Create app: name **Olano**, default language English (United States),
      App, Free, declarations accepted.
- [ ] App content, every item (answers in `REVIEW_NOTES.md`, `PRIVACY.md`,
      `COMPLIANCE.md`): privacy policy, **App access** (Try the demo plus the
      demo credentials), ads (no), content rating (IARC), target audience
      (18+), news (no), data safety, government (no), financial features
      (none), health (none), advertising ID (no), account deletion URL.
- [ ] Main store listing: short and full description, icon 512, feature
      graphic 1024 x 500, phone screenshots (`LISTING.md`, `SCREENSHOTS.md`).
- [ ] Store settings: category Productivity, contact email and website.

### 2.5 Testing tracks, then production

- [ ] Internal testing: create a release, upload the `.aab`, add the team's
      Google accounts, install from the opt-in link, run the device checks.
      Fix anything the pre-launch report flags (it runs the app on Google's
      devices; it will stop at the connect screen unless it taps Try the
      demo, which is fine).
- [ ] (Personal accounts only) closed testing with 12 or more testers for 14
      days.
- [ ] Production: promote the tested release, choose countries, staged
      rollout (for example 20 %), submit for review. Review usually takes a
      few days for a new app.

## 3. After approval

- [ ] Release (Apple: Release This Version; Google: rollout to 100 %).
- [ ] Keep the demo running (`DEMO.md`, section 6): every future update is
      reviewed with it again, so a switched-off demo must be back on before the
      next submission.
- [ ] Note in the release ticket where the screenshots, the upload key and
      the template password live.
