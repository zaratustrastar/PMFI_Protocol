import { DatabaseSync } from "node:sqlite";
import { fingerprint, eligibility, address } from "./core.mjs";
export class Store {
  constructor(path, c) {
    this.c = c;
    this.db = new DatabaseSync(path);
    this.db
      .exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS blocks(number INTEGER PRIMARY KEY,hash TEXT NOT NULL,timestamp INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,tx TEXT NOT NULL,logIndex INTEGER NOT NULL,blockNumber INTEGER NOT NULL,blockHash TEXT NOT NULL,timestamp INTEGER NOT NULL,sender TEXT NOT NULL,recipient TEXT NOT NULL,amount TEXT NOT NULL,orphaned INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS nonces(nonce TEXT PRIMARY KEY,wallet TEXT NOT NULL,message TEXT NOT NULL,expires INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS bindings(wallet TEXT PRIMARY KEY,recipient TEXT NOT NULL,nonce TEXT NOT NULL,signature TEXT NOT NULL,verifiedAt TEXT NOT NULL);
    `);
    // Ledger identity/window cannot silently change on restart. Mode and operational pause may change.
    const identity = { ...c };
    delete identity.mode;
    delete identity.bindingFrozen;
    delete identity.maxIndexAgeSeconds;
    const hash = fingerprint(identity),
      prev = this.get("configHash");
    if (prev && prev !== hash)
      throw Error(
        "Ledger config changed: use a reviewed migration, never reuse this database",
      );
    this.set("configHash", hash);
  }
  get(k) {
    return this.db.prepare("SELECT value FROM meta WHERE key=?").get(k)?.value;
  }
  set(k, v) {
    this.db
      .prepare(
        "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      )
      .run(k, String(v));
  }
  audit(kind, data) {
    this.db
      .prepare("INSERT INTO audit(at,kind,data) VALUES (?,?,?)")
      .run(new Date().toISOString(), kind, JSON.stringify(data));
  }
  transaction(fn) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const v = fn();
      this.db.exec("COMMIT");
      return v;
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }
  tip() {
    return this.db
      .prepare("SELECT * FROM blocks ORDER BY number DESC LIMIT 1")
      .get();
  }
  rollback(number) {
    this.transaction(() => {
      this.audit("reorg", { after: number });
      if (this.get("allocation")) this.set("allocationInvalid", true);
      this.db
        .prepare("UPDATE events SET orphaned=1 WHERE blockNumber>?")
        .run(number);
      this.db.prepare("DELETE FROM blocks WHERE number>?").run(number);
      this.set("healthy", false);
    });
  }
  commit(block, logs) {
    this.transaction(() => {
      this.db
        .prepare("INSERT OR REPLACE INTO blocks VALUES (?,?,?)")
        .run(block.number, block.hash, block.timestamp);
      const q = this.db.prepare(
        `INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET blockNumber=excluded.blockNumber,blockHash=excluded.blockHash,timestamp=excluded.timestamp,sender=excluded.sender,recipient=excluded.recipient,amount=excluded.amount,orphaned=0`,
      );
      for (const e of logs)
        q.run(
          `${this.c.chainId}:${e.tx}:${e.logIndex}`,
          e.tx,
          e.logIndex,
          block.number,
          block.hash,
          block.timestamp,
          e.from,
          e.to,
          e.amount,
        );
    });
  }
  events() {
    const head = Number(this.get("head") || 0);
    return this.db
      .prepare("SELECT * FROM events ORDER BY blockNumber,logIndex")
      .all()
      .map((e) => ({
        chainId: this.c.chainId,
        token: this.c.token,
        id: e.id,
        tx: e.tx,
        logIndex: e.logIndex,
        blockNumber: e.blockNumber,
        blockHash: e.blockHash,
        timestamp: e.timestamp,
        from: e.sender,
        to: e.recipient,
        amount: e.amount,
        orphaned: !!e.orphaned,
        status: eligibility(this.c, { ...e, from: e.sender }, head),
      }));
  }
  deposits(wallet) {
    return this.events().filter(
      (e) =>
        e.to === address(this.c.safe) &&
        (!wallet || e.from === address(wallet)),
    );
  }
  totals() {
    const out = {};
    for (const e of this.deposits())
      if (e.status === "eligible")
        out[e.from] = (BigInt(out[e.from] || 0) + BigInt(e.amount)).toString();
    return out;
  }
  close() {
    this.db.close();
  }
}
