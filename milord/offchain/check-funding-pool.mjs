// Read-only, block-pinned QuoterV2 simulations. No signer and no broadcast.
import { JsonRpcProvider, Contract } from 'ethers';
import { checkFunding, FUNDING } from './lib/funding-check.mjs';
const ABI = [
  'function token0() view returns(address)', 'function token1() view returns(address)',
  'function fee() view returns(uint24)', 'function factory() view returns(address)',
  'function getPool(address,address,uint24) view returns(address)',
  'function decimals() view returns(uint8)', 'function deposit() payable', 'function withdraw(uint256)',
  'function slot0() view returns(uint160,int24,uint16,uint16,uint16,uint8,bool)',
  'function observe(uint32[]) view returns(int56[],uint160[])',
  'function quoteExactInputSingle((address tokenIn,address tokenOut,uint256 amountIn,uint24 fee,uint160 sqrtPriceLimitX96)) returns(uint256,uint160,uint32,uint256)',
];
if (!process.env.RPC_URL) throw Error('set RPC_URL (read-only)');
const p = new JsonRpcProvider(process.env.RPC_URL);
try {
  const block = await p.getBlockNumber();
  const expected = {...FUNDING,pool:process.argv[2] ?? FUNDING.pool,usdg:process.argv[3] ?? FUNDING.usdg};
  const result = await checkFunding({
    chainId: async()=>Number((await p.getNetwork()).chainId),
    code:a=>p.getCode(a,block),
    call:(a,m,args=[])=>new Contract(a,ABI,p)[m].staticCall(...args,{blockTag:block,from:'0x0000000000000000000000000000000000000001'}),
  },expected);
  console.log(JSON.stringify({block,...expected,...result},null,2));
  process.exitCode = result.validated ? 0 : 1;
} finally { p.destroy(); }
