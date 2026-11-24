# The mobile app's everyday commands. Each target does the whole chain it
# needs, so `make ios` from a fresh checkout installs the npm tree, builds the
# screens into www/, copies them into the Xcode project and opens it; the same
# for `make android` and Android Studio. The `*-build` targets compile without
# an IDE (what CI does), the `*-run` targets build and start the app on a
# device or emulator you pick. Run `make help` for the list.
#
# Pull before you build: the native projects (ios/, android/) are committed,
# and a stale checkout opens a stale project.

.PHONY: help install test typecheck build dev preview sync \
        ios ios-sync ios-build ios-run ios-open \
        android android-sync android-build android-run android-open apk \
        clean

SHELL := /bin/bash
NPX := npx --no-install
APK := android/app/build/outputs/apk/debug/app-debug.apk

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ── Web app (shared by both platforms) ────────────────────────────────────────

# `npm install` runs once per package-lock change; node_modules is the stamp.
node_modules: package.json package-lock.json
	npm install
	@touch node_modules

install: node_modules ## Install the npm dependencies (runs by itself when missing or stale)

test: node_modules ## Unit tests + the version check
	npm test

typecheck: node_modules ## Type-check the screens
	npm run typecheck

build: node_modules ## Type-check and build the screens into www/
	npm run build

dev: node_modules ## Serve the app in a desktop browser at http://localhost:5199 (proxies /api to a runtime)
	npm run dev

preview: node_modules ## Serve the built www/ the way the native shells load it
	npm run preview

sync: node_modules ## Build www/ and copy it, the config and the plugins into ios/ and android/
	npm run sync

# ── iOS (a Mac with Xcode) ────────────────────────────────────────────────────

ios: ios-sync ios-open ## Build, sync into the Xcode project and open it (then pick your team and Run)

ios-sync: node_modules ## Build www/ and sync it into ios/
	npm run sync:ios

ios-open: ## Open the Xcode project (no build)
	$(NPX) cap open ios

ios-build: ios-sync ## Build, sync and compile the app for the iOS Simulator without opening Xcode (what CI does)
	@command -v xcodebuild >/dev/null || { echo "xcodebuild not found: install Xcode and run 'sudo xcode-select -s /Applications/Xcode.app'"; exit 1; }
	cd ios/App && xcodebuild -project App.xcodeproj -scheme App -configuration Debug \
	  -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO -quiet build
	@echo "iOS simulator build succeeded."

ios-run: ios-sync ## Build, sync and run on a simulator or a plugged-in iPhone you pick
	$(NPX) cap run ios

# ── Android (Android Studio, or JDK 21 + the Android SDK) ─────────────────────

android: android-sync android-open ## Build, sync into the Android Studio project and open it (then Run)

android-sync: node_modules ## Build www/ and sync it into android/
	npm run sync:android

android-open: ## Open the project in Android Studio (no build)
	$(NPX) cap open android

android-build: android-sync ## Build, sync and compile the debug APK without the IDE (what CI does)
	cd android && ./gradlew assembleDebug
	@echo "APK: $(APK)"

apk: android-build ## Same as android-build, and install the APK on the connected device with adb
	adb install -r $(APK)

android-run: android-sync ## Build, sync and run on an emulator or a plugged-in phone you pick
	$(NPX) cap run android

# ── Housekeeping ──────────────────────────────────────────────────────────────

clean: ## Remove the built web app and the Android build output (keeps node_modules and Xcode's DerivedData)
	rm -rf www android/app/build android/build android/.gradle
