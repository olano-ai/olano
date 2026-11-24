// The app's version is written in three places, and all three are read by
// someone: package.json (the `OlanoApp/<version>` user agent a deployment sees),
// android/app/build.gradle versionName and the Xcode project's MARKETING_VERSION
// (what OlanoShell.getAppInfo() reports and the stores show). This fails when
// they disagree. Build numbers (versionCode, CURRENT_PROJECT_VERSION) are only
// checked for being plain positive integers; each store counts its own.

import { readFileSync } from "node:fs";

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

const pkg = JSON.parse(read("package.json")).version;
const gradle = read("android/app/build.gradle");
const pbxproj = read("ios/App/App.xcodeproj/project.pbxproj");

const versionName = /^\s*versionName\s+"([^"]+)"/m.exec(gradle)?.[1];
const versionCode = /^\s*versionCode\s+(\S+)\s*$/m.exec(gradle)?.[1];
const marketing = [...pbxproj.matchAll(/MARKETING_VERSION = ([^;]+);/g)].map((m) => m[1].trim());
const builds = [...pbxproj.matchAll(/CURRENT_PROJECT_VERSION = ([^;]+);/g)].map((m) => m[1].trim());

const problems = [];
if (versionName !== pkg) problems.push(`android versionName is ${versionName}, package.json says ${pkg}`);
if (!marketing.length || marketing.some((v) => v !== pkg)) {
  problems.push(`iOS MARKETING_VERSION is ${marketing.join(", ") || "missing"}, package.json says ${pkg}`);
}
if (!/^[1-9]\d*$/.test(versionCode ?? "")) problems.push(`android versionCode ${versionCode} is not a positive integer`);
if (!builds.length || builds.some((b) => !/^[1-9]\d*$/.test(b)) || new Set(builds).size > 1) {
  problems.push(`iOS CURRENT_PROJECT_VERSION is ${builds.join(", ") || "missing"}; use one positive integer`);
}

if (problems.length) {
  for (const problem of problems) console.error(`version mismatch: ${problem}`);
  process.exit(1);
}
console.log(`version ${pkg} (android versionCode ${versionCode}, iOS build ${builds[0]})`);
