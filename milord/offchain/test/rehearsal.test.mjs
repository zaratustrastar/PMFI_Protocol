import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
const source=fs.readFileSync(new URL('../../fork/rehearse-atomic-deploy.sh',import.meta.url),'utf8');
function run(env={}, forge='exit 42') {
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'rehearsal-test-'));
 try {
  fs.mkdirSync(`${dir}/fork`); fs.mkdirSync(`${dir}/bin`);
  fs.writeFileSync(`${dir}/fork/run.sh`,source);
  const bins={anvil:'exec sleep 20',forge,cast:`case "$1" in
chain-id) echo 4663;; client) echo anvil;; rpc) echo '{"forkBlockNumber":123}';; codesize) echo 1;; block-number) echo 123;; *) exit 1;; esac`};
  for(const [name,s] of Object.entries(bins)) fs.writeFileSync(`${dir}/bin/${name}`,'#!/bin/bash\n'+s+'\n',{mode:0o755});
  return spawnSync('bash',[`${dir}/fork/run.sh`],{env:{PATH:`${dir}/bin:${process.env.PATH}`,FORK_BUILD:'lowmem',FORK_PORT:'19879',...env},encoding:'utf8',timeout:5000});
 } finally {fs.rmSync(dir,{recursive:true,force:true});}
}
test('invalid port rejected',()=>assert.notEqual(run({FORK_PORT:'8549/path'}).status,0));
test('external fork URL rejected',()=>assert.notEqual(run({FORK_URL:'https://mainnet.invalid'}).status,0));
test('missing matching artifacts rejected',()=>assert.match(run({FORK_BUILD:'ci'}).stdout,/artifact commit mismatch/));
test('forge failure cannot be masked or reach recording',()=>{const r=run();assert.equal(r.status,42);assert.doesNotMatch(r.stdout,/REHEARSAL PASSED|== 2\./);});
