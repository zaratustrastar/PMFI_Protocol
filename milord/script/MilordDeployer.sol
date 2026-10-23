// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {MILORDToken} from "../src/robinhood/MILORDToken.sol";
import {MilordsNFT} from "../src/robinhood/MilordsNFT.sol";
import {MilordConverter} from "../src/robinhood/MilordConverter.sol";
import {RevenueSplitter} from "../src/robinhood/RevenueSplitter.sol";
import {BuybackBurner, ISwapAdapter} from "../src/robinhood/BuybackBurner.sol";
import {StrategyRegistry} from "../src/robinhood/StrategyRegistry.sol";
import {MilordGovernor} from "../src/robinhood/MilordGovernor.sol";
import {IMILORD} from "../src/interfaces/IMILORD.sol";
import {PMFIArbVaultV3} from "../src/parb/PMFIArbVaultV3.sol";
import {ParbFundingAdapter, IParbVault} from "../src/parb/ParbFundingAdapter.sol";
import {ShareDistributor} from "../src/parb/ShareDistributor.sol";
import {ArbitrageFundingRouter, IParbFundingAdapter} from "../src/automation/ArbitrageFundingRouter.sol";
import {IPriceReference, ISwapVenue} from "../src/automation/IPriceReference.sol";

/// @notice Single-chain MILORD deployment: token, NFT, converter, fee routing AND the pARB
///         vault + weekly distributor, all on Robinhood Chain (or a local/test chain).
///         Shared by DeployLocal, DeployRobinhood and the tests so they deploy identically.
abstract contract MilordDeployer {
    struct Config {
        address deployer; // msg.sender during deployment; configures, then hands over
        address owner; // Team Safe (final owner of every admin role)
        address teamSafe;
        address arbitrageSafe;
        address servicer; // arbitrage engine wallet that receives tend() capital
        address quoteSigner;
        address reportSigner;
        address operator; // buyback, funding and root-publishing ops key
        address tokenHolder; // receives the 6.767B MILORD (ignored when `token` is set)
        // Atomic launch: MilordAtomicLaunch.predictedToken(). MILORD is created later, inside the Safe's launch
        // transaction, so nothing here may call the token; the constructors below only store its address.
        address token;
        address asset; // vault stablecoin: USDG on Robinhood Chain (6 decimals)
        address swapAdapter; // MILORD buyback venue adapter
        string baseURI;
        string[] characteristics; // 8 initial labels
        uint256 vaultCap; // asset units
        uint256 rootActivationDelay;
        uint16[3] splitterMin; // optional production bounds (entry-arb, royalty-team, tax-each); 0 = unbounded
        uint16[3] splitterMax; // 0 = 10000
        // Automation router (optional). When fundingReference is set, the splitters pay their arbitrage share to
        // ArbitrageFundingRouter (owned by arbitrageSafe) instead of to the Safe itself.
        address fundingVenue; // ETH -> USDG venue (UniV3SwapVenue over the WETH/USDG pool)
        address fundingReference; // IPriceReference (UniV3TwapReference, optionally inside DualReference)
        uint256[3] routerAmounts; // min swap, max swap, daily cap (wei)
        uint16 routerSlippageBps;
        uint32 routerInterval;
        address routerKeeper; // 0 = open execution
        address routerPauser; // monitoring key that may pause (not unpause)
    }

    struct Deployment {
        MILORDToken token;
        MilordsNFT nft;
        MilordConverter converter;
        RevenueSplitter entrySplit;
        RevenueSplitter royaltySplit;
        RevenueSplitter taxSplit;
        BuybackBurner burner;
        StrategyRegistry registry;
        MilordGovernor governor;
        PMFIArbVaultV3 vault;
        ShareDistributor distributor;
        ParbFundingAdapter adapter;
        uint256 arbitrageStrategyId;
        ArbitrageFundingRouter router; // zero when not configured
    }

    function _deployMilord(Config memory c) internal returns (Deployment memory d) {
        require(IERC20Metadata(c.asset).decimals() == 6, "vault asset must have 6 decimals");
        require(c.characteristics.length == 8, "need 8 characteristics");
        address me = c.deployer;

        d.token = c.token != address(0) ? MILORDToken(c.token) : new MILORDToken(c.tokenHolder);

        // pARB on the same chain: no reward bridge.
        d.vault = new PMFIArbVaultV3(
            "PMFI Arb (MILORD)", "pARB", c.asset, c.reportSigner, c.teamSafe, c.servicer, c.vaultCap, 0
        );
        d.distributor = new ShareDistributor(me, IERC20(address(d.vault)), c.operator);
        d.adapter = new ParbFundingAdapter(me, IERC20(c.asset), IParbVault(address(d.vault)), address(d.distributor));
        d.adapter.setOperator(c.operator, true);
        d.vault.setDepositorRestricted(true);
        d.vault.setAllowedDepositor(address(d.adapter), true);
        d.distributor.setActivationDelay(c.rootActivationDelay);

        address arbPayee = c.arbitrageSafe;
        if (c.fundingReference != address(0)) {
            d.router = new ArbitrageFundingRouter(
                me,
                IERC20(c.asset),
                IParbFundingAdapter(address(d.adapter)),
                ISwapVenue(c.fundingVenue),
                IPriceReference(c.fundingReference),
                c.routerAmounts[0],
                c.routerAmounts[1],
                c.routerAmounts[2],
                c.routerSlippageBps,
                c.routerInterval
            );
            if (c.routerKeeper == address(0)) d.router.setOpenExecution(true);
            else d.router.setKeeper(c.routerKeeper, true);
            if (c.routerPauser != address(0)) d.router.setPauser(c.routerPauser, true);
            d.adapter.setOperator(address(d.router), true);
            arbPayee = address(d.router);
        }

        d.royaltySplit = new RevenueSplitter(me, _two(c.teamSafe, 5000, arbPayee, 5000, "team", "arbitrage", c, 1));
        d.entrySplit = new RevenueSplitter(me, _two(arbPayee, 7000, c.teamSafe, 3000, "arbitrage", "team", c, 0));
        d.nft = new MilordsNFT(me, address(d.royaltySplit), c.baseURI);
        d.converter = new MilordConverter(me, IMILORD(address(d.token)), d.nft, address(d.entrySplit), c.quoteSigner);
        d.nft.setConverter(address(d.converter));

        d.burner = new BuybackBurner(me, IMILORD(address(d.token)), ISwapAdapter(c.swapAdapter));
        d.burner.setOperator(c.operator, true);
        RevenueSplitter.Payee[] memory t = new RevenueSplitter.Payee[](3);
        (uint16 lo, uint16 hi) = _bounds(c, 2);
        t[0] = RevenueSplitter.Payee(address(d.burner), 3334, lo, hi, "buyback-burn");
        t[1] = RevenueSplitter.Payee(c.teamSafe, 3333, lo, hi, "team");
        t[2] = RevenueSplitter.Payee(arbPayee, 3333, lo, hi, "arbitrage");
        d.taxSplit = new RevenueSplitter(me, t);

        d.registry = new StrategyRegistry(me, d.nft);
        for (uint256 i; i < 8; ++i) d.registry.addCharacteristic(c.characteristics[i]);
        d.arbitrageStrategyId = d.registry.addStrategy("pArbitrage", block.chainid, address(d.vault), address(d.vault), true);
        d.registry.setDefaultStrategy(d.arbitrageStrategyId);
        d.governor = new MilordGovernor(d.nft);

        // The router belongs to the Arbitrage Funding Safe (Ownable2Step: the Safe calls acceptOwnership()).
        if (address(d.router) != address(0) && c.arbitrageSafe != me) d.router.transferOwnership(c.arbitrageSafe);
        if (c.owner != me) {
            // Ownable2Step contracts: the Safe must call acceptOwnership() on each.
            d.nft.transferOwnership(c.owner);
            d.converter.transferOwnership(c.owner);
            d.entrySplit.transferOwnership(c.owner);
            d.royaltySplit.transferOwnership(c.owner);
            d.taxSplit.transferOwnership(c.owner);
            d.burner.transferOwnership(c.owner);
            d.registry.transferOwnership(c.owner);
            d.distributor.transferOwnership(c.owner);
            d.adapter.transferOwnership(c.owner);
            d.vault.transferOwnership(c.owner); // Ownable (one step), as in V2
        }
    }

    function _bounds(Config memory c, uint256 i) private pure returns (uint16 lo, uint16 hi) {
        lo = c.splitterMin[i];
        hi = c.splitterMax[i] == 0 ? 10000 : c.splitterMax[i];
    }

    /// i selects the bounds for the FIRST payee; the second gets the mirror range.
    function _two(address a, uint16 ab, address b, uint16 bb, string memory al, string memory bl, Config memory c, uint256 i)
        private
        pure
        returns (RevenueSplitter.Payee[] memory p)
    {
        (uint16 lo, uint16 hi) = _bounds(c, i);
        p = new RevenueSplitter.Payee[](2);
        p[0] = RevenueSplitter.Payee(a, ab, lo, hi, al);
        p[1] = RevenueSplitter.Payee(b, bb, 10000 - hi, 10000 - lo, bl);
    }
}
