import { Interface, parseUnits } from "ethers";
const abi = new Interface(["function transfer(address,uint256) returns(bool)"]);
export function units(text, decimals) {
  if (!/^\d+(\.\d+)?$/.test(text) || text.length > 100)
    throw Error("Enter a positive decimal amount");
  const value = parseUnits(text, decimals);
  if (value <= 0n) throw Error("Amount must be greater than zero");
  return value;
}
export async function transferRequest(raw, state, account, amount, balance) {
  if (!state.enabled) throw Error("Deposits are currently unavailable");
  if (Number(await raw.request({ method: "eth_chainId" })) !== state.chainId)
    throw Error("Switch to Base before depositing");
  const accounts = await raw.request({ method: "eth_accounts" });
  if (accounts?.[0]?.toLowerCase() !== account.toLowerCase())
    throw Error("Wallet changed. Connect again and review.");
  const value = units(amount, state.decimals);
  if (value > balance) throw Error("Insufficient PMFI balance");
  return {
    from: account,
    to: state.token,
    data: abi.encodeFunctionData("transfer", [state.safe, value]),
    value: "0x0",
  };
}
export function errorText(e) {
  if (e.code === 4001 || e.code === "ACTION_REJECTED")
    return "Request cancelled in your wallet. No new transfer was submitted.";
  if (e.code === "INSUFFICIENT_FUNDS")
    return "Insufficient ETH for Base gas. Your PMFI amount has not changed.";
  if (e.code === "CALL_EXCEPTION")
    return "The token transaction reverted. Check balance and token restrictions.";
  return (
    e.shortMessage ||
    e.message ||
    "Unable to complete this request. Please retry."
  );
}
