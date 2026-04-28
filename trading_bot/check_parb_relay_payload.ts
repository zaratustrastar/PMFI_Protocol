import { createWalletClient, http, type Hex } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { polygon } from 'viem/chains';
import { RelayClient, RelayerTxType } from '@polymarket/builder-relayer-client';
import { BuilderConfig } from '@polymarket/builder-signing-sdk';
import * as dotenv from 'dotenv';

dotenv.config({ path: '/root/PolyNotifyBot/.env' });

async function main() {
  let pk = (process.env.POLY_PRIVATE_KEY || '').trim();
  if (!pk) throw new Error('POLY_PRIVATE_KEY missing');
  if (!pk.startsWith('0x')) pk = '0x' + pk;

  const account = privateKeyToAccount(pk as Hex);
  const wallet = createWalletClient({
    account,
    chain: polygon,
    transport: http(process.env.POLYGON_RPC_URL || 'https://polygon-rpc.com'),
  });

  const builderConfig = new BuilderConfig({
    localBuilderCreds: {
      key: process.env.POLY_API_KEY || '',
      secret: process.env.POLY_API_SECRET || '',
      passphrase: process.env.POLY_API_PASSPHRASE || '',
    },
  });

  console.log('EOA:', account.address);
  console.log('ENV POLY_PROXY_ADDRESS:', process.env.POLY_PROXY_ADDRESS || '');

  const safeClient = new RelayClient(
    'https://relayer-v2.polymarket.com/',
    137,
    wallet,
    builderConfig,
    RelayerTxType.SAFE
  );

  const proxyClient = new RelayClient(
    'https://relayer-v2.polymarket.com/',
    137,
    wallet,
    builderConfig,
    RelayerTxType.PROXY
  );

  try {
    const safePayload = await (safeClient as any).getRelayPayload?.(account.address, 'SAFE');
    console.log('SAFE payload:', JSON.stringify(safePayload, null, 2));
  } catch (e) {
    console.log('SAFE payload error:', String(e));
  }

  try {
    const proxyPayload = await (proxyClient as any).getRelayPayload?.(account.address, 'PROXY');
    console.log('PROXY payload:', JSON.stringify(proxyPayload, null, 2));
  } catch (e) {
    console.log('PROXY payload error:', String(e));
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
