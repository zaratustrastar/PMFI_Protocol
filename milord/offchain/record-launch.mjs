// Read-only recorder. Exit 2 means validated launch evidence but incomplete service configuration.
import fs from 'node:fs';
import { JsonRpcProvider, Contract } from 'ethers';
import { validateLaunch } from './lib/launch-record.mjs';
const [file,helper,stateView] = process.argv.slice(2);
if (!process.env.RPC_URL || !file || !helper) throw Error('usage: RPC_URL=... node offchain/record-launch.mjs <file> <helper> [StateView]');
const p = new JsonRpcProvider(process.env.RPC_URL);
try {
  const D = JSON.parse(fs.readFileSync(file,'utf8'));
  const chainId = Number((await p.getNetwork()).chainId);
  if (D.chainId !== undefined && Number(D.chainId) !== chainId) throw Error('chain mismatch');
  const block = await p.getBlockNumber();
  const result = await validateLaunch(D,helper,stateView,{
    code:a=>p.getCode(a,block),
    contract:(a,abi)=>new Proxy(new Contract(a,abi,p),{get:(c,m)=> (...args)=>c[m].staticCall(...args,{blockTag:block})}),
  });
  const output = {...D,...result.added,launchValidation:{validated:result.validated,complete:result.complete,missing:result.missing,block,chainId}};
  const temp = `${file}.${process.pid}.tmp`;
  try { fs.writeFileSync(temp,JSON.stringify(output,null,2)+'\n',{flag:'wx'}); fs.renameSync(temp,file); }
  finally { if (fs.existsSync(temp)) fs.unlinkSync(temp); }
  console.log(JSON.stringify({recorded:file,...result},null,2));
  process.exitCode = result.complete ? 0 : 2;
} finally { p.destroy(); }
