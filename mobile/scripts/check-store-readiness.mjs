// What App Review and Google Play reject a build for that a file in this
// repository can get wrong, checked on every `npm test` so a later edit cannot
// quietly undo it (mobile/store/SUBMISSION.md is the human half):
//
// - every iOS target (App, ShareExtension, OlanoWidgets) bundles its own
//   PrivacyInfo.xcprivacy, declares no tracking, and the app's declares the
//   required-reason APIs its Swift uses;
// - Info.plist carries a purpose string for every permission the code asks
//   for, and says the app uses only exempt encryption;
// - the Android manifest asks for no permission Play would question and
//   strips the advertising ID a library could merge in.

import { readFileSync } from "node:fs";

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const problems = [];
const need = (ok, message) => {
  if (!ok) problems.push(message);
};

const pbxproj = read("ios/App/App.xcodeproj/project.pbxproj");
for (const target of ["App", "ShareExtension", "OlanoWidgets"]) {
  let manifest = "";
  try {
    manifest = read(`ios/App/${target}/PrivacyInfo.xcprivacy`);
  } catch {
    problems.push(`ios/App/${target}/PrivacyInfo.xcprivacy is missing`);
    continue;
  }
  need(/<key>NSPrivacyTracking<\/key>\s*<false\/>/.test(manifest), `${target}: NSPrivacyTracking must be false`);
  need(/<key>NSPrivacyTrackingDomains<\/key>\s*<array\/>/.test(manifest), `${target}: no tracking domains`);
}
const inResources = pbxproj.match(/PrivacyInfo\.xcprivacy in Resources \*\/,/g) ?? [];
need(inResources.length === 3, `each target's Resources phase must list its PrivacyInfo.xcprivacy (found ${inResources.length})`);

const appManifest = read("ios/App/App/PrivacyInfo.xcprivacy");
for (const reason of ["CA92.1", "1C8F.1", "C617.1"]) {
  need(appManifest.includes(`<string>${reason}</string>`), `App privacy manifest: required-reason ${reason} missing`);
}
const widgetManifest = read("ios/App/OlanoWidgets/PrivacyInfo.xcprivacy");
need(widgetManifest.includes("<string>1C8F.1</string>"), "OlanoWidgets privacy manifest: UserDefaults 1C8F.1 missing");

const plist = read("ios/App/App/Info.plist");
const purpose = (key) => new RegExp(`<key>${key}</key>\\s*<string>([^<]{20,})</string>`).exec(plist)?.[1];
for (const key of [
  "NSCameraUsageDescription",
  "NSMicrophoneUsageDescription",
  "NSSpeechRecognitionUsageDescription",
  "NSFaceIDUsageDescription",
  "NSPhotoLibraryUsageDescription",
  "NSPhotoLibraryAddUsageDescription",
]) {
  need(purpose(key), `Info.plist: ${key} needs a specific purpose string`);
}
need(/<key>ITSAppUsesNonExemptEncryption<\/key>\s*<false\/>/.test(plist), "Info.plist: ITSAppUsesNonExemptEncryption must be false");
need(!/<string>armv7<\/string>/.test(plist), "Info.plist: UIRequiredDeviceCapabilities must not name armv7 (the app is arm64 only)");
for (const target of ["App", "ShareExtension", "OlanoWidgets"]) {
  const info = read(`ios/App/${target}/Info.plist`);
  need(info.includes("$(MARKETING_VERSION)") && info.includes("$(CURRENT_PROJECT_VERSION)"), `${target}/Info.plist: versions must come from the build settings`);
}

const manifest = read("android/app/src/main/AndroidManifest.xml");
const asked = [...manifest.matchAll(/<uses-permission android:name="([^"]+)"\s*\/>/g)].map((m) => m[1]).sort();
const allowed = [
  "android.permission.CAMERA",
  "android.permission.INTERNET",
  "android.permission.MODIFY_AUDIO_SETTINGS",
  "android.permission.POST_NOTIFICATIONS",
  "android.permission.RECORD_AUDIO",
];
need(JSON.stringify(asked) === JSON.stringify(allowed), `AndroidManifest asks for ${asked.join(", ")}; update the Data safety answers (mobile/store/PRIVACY.md) and this list together`);
for (const removed of ["com.google.android.gms.permission.AD_ID", "android.permission.READ_MEDIA_IMAGES", "android.permission.READ_EXTERNAL_STORAGE"]) {
  need(new RegExp(`android:name="${removed.replace(/\./g, "\\.")}" tools:node="remove"`).test(manifest), `AndroidManifest must remove ${removed}`);
}
const variables = read("android/variables.gradle");
const targetSdk = Number(/targetSdkVersion\s*=\s*(\d+)/.exec(variables)?.[1]);
need(targetSdk >= 36, `targetSdkVersion ${targetSdk}: Google Play requires 36 for new apps and updates`);

if (problems.length) {
  for (const problem of problems) console.error(`store readiness: ${problem}`);
  process.exit(1);
}
console.log("store readiness: privacy manifests, purpose strings, encryption flag and Android permissions in order");
