// Report signing for PMFIArbVaultV3 (same struct as V2, V3 domain salt).
import { AbiCoder, keccak256, toUtf8Bytes, getBytes } from "ethers";

export const REPORT_TYPEHASH = keccak256(toUtf8Bytes(
  "ReportDataV2(uint256 reportedAssets,uint256 timestamp,uint256 deadline,uint256 nonce,address vault,uint256 chainId,bytes32 domainSalt)"));
export const V3_SALT = keccak256(toUtf8Bytes("PMFIArbVaultV3.milord.v1"));

export async function signReport(wallet, { reportedAssets, timestamp, deadline, nonce, vault, chainId }) {
  const structHash = keccak256(AbiCoder.defaultAbiCoder().encode(
    ["bytes32", "uint256", "uint256", "uint256", "uint256", "address", "uint256", "bytes32"],
    [REPORT_TYPEHASH, reportedAssets, timestamp, deadline, nonce, vault, chainId, V3_SALT]));
  return wallet.signMessage(getBytes(structHash));
}
