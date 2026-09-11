# Install Meal Scanner on a phone

The scanner can be installed as a web app with its own home-screen icon. Use the scanner's stable HTTPS address. The installation banner appears in a normal browser and is hidden when the scanner runs as an installed app. Dismissing the banner hides it for the current browser session.

## Android

Open the scanner link in Chrome or another browser that supports installing web apps. When the browser makes installation available, tap **Install app** in the banner and confirm its installation dialog. The browser controls when that dialog is available; it may require a page interaction and some time on the page. If the banner shows instructions instead, use the browser menu's **Install app** or **Add to Home screen** option, when available.

After installation, tap **Meal Scanner** on the phone's home screen. A supported installation opens the scanner in a standalone window. An embedded browser inside another app may require opening the link in a full browser first.

## iPhone and iPad

Open the scanner link in Safari. Tap **Share**, choose **Add to Home Screen**, leave **Open as Web App** enabled when shown, and tap **Add**. Safari does not expose the same JavaScript installation dialog as Android Chrome, so the scanner displays these steps. The new **Meal Scanner** icon opens the web app.

## Access and internet

Installing creates a shortcut to the existing scanner application. It does not create a different database or give the device administrator access. The existing scanner activation code may be needed in the installed app if the browser keeps its storage separate. Do not put activation codes, QR credentials, or passwords in installation links.

The application still requires internet access and a confirmed server commit to approve a meal. This feature adds no service worker, offline cache, offline approval, background retry, APK download, push-notification permission, or app-store publication. Browser and operating-system policies determine the final installation experience; a website cannot silently install itself.

## Deployment

Run the usual frontend and scanner Git build checks before deploying. The scanner build includes the manifest and four PNG icons under `/assets/`; the administrator build excludes them. Publish the reviewed files using the existing scanner deployment workflow. This feature needs no dependency installation, database migration, or environment setting.

## Device checks after deployment

1. On Android Chrome, open the stable scanner HTTPS link. Verify the install banner, its dismissal, the browser installation dialog when available, and the home-screen icon.
2. On iPhone Safari, follow the banner's Share instructions and verify the icon and standalone launch.
3. Launch from the icon. The install banner should be hidden; activate the scanner if requested and allow the camera when starting it.
4. Use an approved test QR to verify camera scanning and server-confirmed results. Interrupt the connection during a test serving and verify recovery uses the same request identifier without adding another meal. Approval must never be shown solely because the app is installed.
5. Test an unavailable camera and an embedded browser. The existing QR-image upload and installation guidance should remain usable.

Automated tests cover installation events, dismissal, standalone detection, manifest/icon serving, access separation, and deployment packaging. Real phone installation and camera behavior require device verification. Laptop `localhost` is not the phone's `localhost`; use the deployed HTTPS link for phone checks.

References: [Chrome installation criteria](https://web.dev/articles/install-criteria), [installation prompt behavior](https://web.dev/learn/pwa/installation-prompt), and [Apple's home-screen web app instructions](https://support.apple.com/en-euro/guide/iphone/iphea86e5236/ios).
