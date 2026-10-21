import { mkdirSync, writeFileSync, rmSync } from "node:fs";
import { resolve } from "node:path";
// One process owns the ledger, including HTTP binding writes. Never guess that an
// existing lock is stale: after a crash the operator checks/stops the old PID.
export function acquireLedgerLock(database) {
  const path = resolve(database) + ".writer-lock";
  try {
    mkdirSync(path, { mode: 0o700 });
  } catch (e) {
    if (e.code === "EEXIST")
      throw Error(
        "Ledger is locked. Stop the owning service before CLI maintenance; verify stale locks manually.",
      );
    throw e;
  }
  try {
    writeFileSync(
      path + "/owner.json",
      JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() }),
      { mode: 0o600, flag: "wx" },
    );
  } catch (e) {
    rmSync(path, { recursive: true });
    throw e;
  }
  let released = false;
  const release = () => {
    if (!released) {
      released = true;
      rmSync(path, { recursive: true, force: true });
    }
  };
  process.once("exit", release);
  return () => {
    process.off("exit", release);
    release();
  };
}
