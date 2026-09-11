import { build } from "esbuild";
import { copyFile, mkdir, rm } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const legacyOutputs = ["app.js", "scan-app.js", "index.html", "scan.html", "styles.css", "scan-only.css", "THIRD_PARTY_NOTICES.txt"];
const scannerInstallAssets = ["manifest.webmanifest", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"];

export async function buildApplications(outputDirectory = join(sourceDirectory, "dist")) {
  const adminDirectory = join(outputDirectory, "admin");
  const scannerDirectory = join(outputDirectory, "scanner");
  await Promise.all([
    mkdir(adminDirectory, { recursive: true }),
    mkdir(scannerDirectory, { recursive: true })
  ]);
  const options = {
    absWorkingDir: sourceDirectory,
    bundle: true,
    minify: true,
    sourcemap: false,
    target: ["es2022"],
    legalComments: "none",
    format: "esm"
  };
  await Promise.all([
    build({ ...options, entryPoints: { app: "src/app.js" }, outdir: adminDirectory }),
    build({ ...options, entryPoints: { "scan-app": "src/scan_app.js" }, outdir: scannerDirectory })
  ]);
  await Promise.all([
    ...scannerInstallAssets.map(name => copyFile(join(sourceDirectory, "pwa", name), join(scannerDirectory, name))),
    copyFile(join(sourceDirectory, "index.html"), join(adminDirectory, "index.html")),
    copyFile(join(sourceDirectory, "scan.html"), join(scannerDirectory, "index.html")),
    copyFile(join(sourceDirectory, "src/styles.css"), join(adminDirectory, "styles.css")),
    copyFile(join(sourceDirectory, "src/styles.css"), join(scannerDirectory, "styles.css")),
    copyFile(join(sourceDirectory, "src/scan_only.css"), join(scannerDirectory, "scan-only.css")),
    copyFile(join(sourceDirectory, "THIRD_PARTY_NOTICES.txt"), join(adminDirectory, "THIRD_PARTY_NOTICES.txt")),
    copyFile(join(sourceDirectory, "THIRD_PARTY_NOTICES.txt"), join(scannerDirectory, "THIRD_PARTY_NOTICES.txt"))
  ]);
  await Promise.all(legacyOutputs.map(name => rm(join(outputDirectory, name), { force: true })));
  return { adminDirectory, scannerDirectory };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await buildApplications();
