// Regression (VPS rehearsal at 2b4bb4b): fork/rehearse-full-launch.sh pointed DeployRobinhood's manifest at a mktemp
// directory under /tmp, which foundry.toml's fs_permissions refuse to vm.writeJson. Every forge script output a
// rehearsal script requests must land under a read-write fs_permissions path, uniquely and cleaned up on exit.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../..");
const toml = fs.readFileSync(path.join(root, "foundry.toml"), "utf8");
const rw = [...toml.matchAll(/\{\s*access\s*=\s*"read-write"\s*,\s*path\s*=\s*"([^"]+)"\s*\}/g)].map((m) => path.normalize(m[1]).replace(/\/$/, ""));
const scripts = ["fork", "sim"].flatMap((d) => fs.readdirSync(path.join(root, d)).filter((f) => f.endsWith(".sh")).map((f) => `${d}/${f}`));

test("fs_permissions grant read-write only to ./deployments and ./sim (never /tmp or the whole tree)", () => {
  assert.deepEqual(rw.sort(), ["deployments", "sim"]);
});

test("every MILORD_DEPLOYMENTS_OUT in the rehearsal scripts is a temp file under a read-write path", () => {
  let seen = 0;
  for (const f of scripts) {
    const src = fs.readFileSync(path.join(root, f), "utf8");
    for (const m of src.matchAll(/MILORD_DEPLOYMENTS_OUT=("?)([^"\s\\]+)\1/g)) {
      seen++;
      const v = m[2];
      assert.ok(!/\$TMP|\/tmp|mktemp -d/.test(v), `${f}: ${v} is outside fs_permissions`);
      const varName = v.match(/^\.\/\$\{?([A-Z_]+)\}?$/)?.[1];
      if (varName) {
        const def = src.match(new RegExp(`^${varName}=\\$\\(mktemp (--suffix=\\.json )?([^ )]+)\\)`, "m"));
        assert.ok(def, `${f}: ${varName} must be created with mktemp`);
        const dir = path.normalize(path.dirname(def[2]));
        assert.ok(rw.includes(dir), `${f}: ${varName} is created in ${dir}, not a read-write fs_permissions path`);
        assert.match(src, new RegExp(`rm -f -- "\\$${varName}"`), `${f}: ${varName} is not removed by the cleanup trap`);
      } else {
        assert.ok(rw.some((d) => path.normalize(v).startsWith(d + "/")), `${f}: ${v} not under ${rw}`);
      }
    }
  }
  assert.ok(seen >= 1, "rehearse-full-launch.sh sets MILORD_DEPLOYMENTS_OUT");
});

test("rehearse-full-launch.sh hands the moved manifest to every later phase", () => {
  const src = fs.readFileSync(path.join(root, "fork/rehearse-full-launch.sh"), "utf8");
  assert.match(src, /mv -- "\$MANIFEST" "\$TMP\/deployments\.json"/);
  for (const phase of ["flows launch", "flows operate"]) assert.match(src, new RegExp(`${phase} "\\$TMP/deployments\\.json"`));
  assert.match(src, /trap cleanup EXIT/);
});
