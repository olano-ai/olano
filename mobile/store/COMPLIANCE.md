# Compliance answers: accounts, age rating, export, content

## Account creation and deletion

**Finding: the app cannot create accounts.** Its screens are connect
(`src/screens/start/ConnectScreen.tsx`), sign in with an existing email and
password (`SignInScreen.tsx`, `POST /api/auth/login`), and Try the demo. There
is no sign-up, invite acceptance or password reset in the app; accounts exist
on a deployment and are created by its administrator in the web dashboard
(Users), which is also where an account is deactivated or deleted
(`DELETE /api/admin/users/{id}`). The demo's visitor accounts are created by
the demo deployment for one visit, hold nothing of the person's, and are
deleted with their conversations after a day.

- **Apple 5.1.1(v)** ("apps that support account creation must also offer
  account deletion"): does not apply. Text for the review notes is in
  `REVIEW_NOTES.md`.
- **Google Play account deletion policy** (apps that let users create an
  account from within the app): does not apply for the same reason. Play
  Console still asks every app with sign-in for a deletion URL in the Data
  safety form; give `https://olano.ai/account-deletion` (to publish: a page
  saying that accounts on an Olano deployment are created and deleted by that
  deployment's administrator in its dashboard, that an Olano Cloud customer
  deletes their organization and deployments from the console or by writing
  to privacy@olano.ai, and what is deleted and kept).
- If a future version adds sign-up (for example "Create a deployment" in the
  app), it must add in-app account deletion in the same release (Apple) and
  the web deletion URL above must cover it (Google).

**Sign in with Apple** (guideline 4.8) is not required: the app offers no
third-party or social login, only the deployment's own email and password.

## Apple age rating (App Store Connect, App Information, Age Rating)

The questionnaire as of November 2026 (the 2025 system with 4+, 9+, 13+,
16+, 18+, the social media questions added July 2026, and the Shared Location
and Dating and Relationships descriptors required from November 16, 2026).

| Question | Answer | Reason |
|---|---|---|
| Parental controls | No | |
| Age assurance | No | |
| Unrestricted web access | **Yes** | Links open in an in-app browser that can go anywhere, and the agent's live browser can be driven to any page |
| User-generated content | Yes | People and AI agents write messages; on a deployment, colleagues share agents |
| Messaging and chat | Yes | Group chats on a deployment can include other people |
| Social media (redistribute or amplify content through a feed) | No | No feed, no discovery |
| Advertising | No | |
| Shared Location | No | The app never reads location |
| Dating and Relationships | No | Agents are business assistants, not companions; nothing romantic or sexual |
| Violence, sexual content, nudity, profanity, horror, drugs, alcohol, gambling, contests, loot boxes, simulated gambling | None | The app has no content of its own; replies are generated for the person's request |
| Medical or wellness topics | None | |

Expected result: **16+** (unrestricted web access). Olano is a business tool;
App Store Connect lets you raise the rating after answering. Recommended:
leave it at the computed 16+, which already says "not for children", unless
the terms of service set 18 as the minimum age, in which case choose 18+ to
match them (a product and legal decision).

Content Rights: "Does your app contain, show, or access third-party
content?" **Yes**: replies can quote web pages the agents read, and the app
shows the person's own files. Olano has the rights to everything the app
itself ships.

## Google Play content rating (IARC questionnaire)

- Category: **Utility, Productivity, Communication, or Other**.
- Violence, fear, sexuality, language, controlled substances, crude humour,
  gambling: **No** to each.
- Does the app allow users to interact or exchange content with other users?
  **Yes** (group chats and shared agents on a deployment).
- Does the app share the user's current physical location with other users?
  **No**.
- Does the app allow users to purchase digital goods? **No**.
- Does the app provide unrestricted internet access (browser, search engine)?
  **Yes**.
- Does the app contain generative AI features? **Yes**: AI agents write
  replies. The app lets a person report a reply from its menu (Report, with a
  reason), which records it for the deployment's administrators, as Play's
  AI-generated content policy requires.

Expected result: Everyone / PEGI 3 / USK 0 class ratings with the
"Users Interact" and "Unrestricted Internet" interactive elements, or Teen in
some regions. Set **Target audience** to 18 and over regardless: it is a
business app, and choosing under-18 audiences brings the Families policy.

## Export compliance

- `ITSAppUsesNonExemptEncryption` is `false` in `ios/App/App/Info.plist`, so
  App Store Connect asks nothing per build.
- The app uses only encryption built into the operating system: HTTPS (TLS)
  through URLSession and HttpURLConnection/OkHttp, the Keychain, and the
  platform's biometric APIs. It implements no cryptography of its own and
  ships no crypto library. That is exempt under the US EAR (category 5,
  part 2, note 4 / the mass-market exemption for standard HTTPS), so no
  annual self-classification report is needed for it.
- If asked in App Store Connect: "Does your app use encryption?" **Yes**;
  "Does it qualify for any exemptions?" **Yes, only standard encryption
  (HTTPS) built into the OS**; France: no separate declaration needed for
  that use.
- Google Play has no equivalent form; the same facts apply if a distribution
  country asks.

## Other App Review points already handled in the build

| Guideline | Status |
|---|---|
| 2.1 App completeness | Try the demo; see `DEMO.md` |
| 2.3.1 Hidden features | The remote config cannot enable anything |
| 2.5.4 Background modes | `UIBackgroundModes` is empty; push is delivered by the system |
| 4.2 Minimum functionality | Native screens, widgets, Siri intents, share extension; never loads the deployment's web pages |
| 5.1.1(i) Privacy policy | Link in the listing and in App Store Connect; see `LISTING.md` |
| 5.1.1(ii) Permission strings | Specific purpose strings for each permission, asked on first use |
| 5.1.2 Data use | No tracking, no data sold or shared; App Tracking Transparency is not needed |
| 1.2 User-generated content | Report on every reply; per-visitor demo accounts; the deployment's admins moderate their own deployment |
