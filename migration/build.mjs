import { build } from "esbuild";
import { mkdir, copyFile } from "node:fs/promises";
await mkdir("dist", { recursive: true });
for (const f of ["index.html", "styles.css"])
  await copyFile("public/" + f, "dist/" + f);
await build({
  entryPoints: ["public/app.mjs"],
  bundle: true,
  format: "esm",
  platform: "browser",
  target: ["es2022"],
  outfile: "dist/app.js",
  minify: true,
});
// Copy the existing, reviewed deployed Privy bridge only when supplied. No new Privy app ID.
if (process.env.PMFI_PRIVY_BRIDGE)
  await copyFile(process.env.PMFI_PRIVY_BRIDGE, "dist/privy-bridge.bundle.js");
console.log(
  "Built scoped /migrate assets. No deployment or activation performed.",
);
