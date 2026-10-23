// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;
import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {UniV3SwapVenue,IUniswapV3PoolSwap,IWETH9} from "../../src/automation/UniV3SwapVenue.sol";
import {UniV3TwapReference,IUniswapV3PoolOracle} from "../../src/automation/UniV3TwapReference.sol";
import {ArbitrageFundingRouter,IParbFundingAdapter} from "../../src/automation/ArbitrageFundingRouter.sol";
import {PMFIArbVaultV3} from "../../src/parb/PMFIArbVaultV3.sol";
import {ParbFundingAdapter,IParbVault} from "../../src/parb/ParbFundingAdapter.sol";
contract FundingPathForkTest is Test {
    address constant POOL=address(bytes20(hex"69bfaf19c9f377bb306a89aed9f6b07e2c1a8d9a"));
    address constant WETH=0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    address constant USDG=0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    UniV3SwapVenue venue;
    UniV3TwapReference refPrice;
    PMFIArbVaultV3 vault;
    ParbFundingAdapter adapter;
    ArbitrageFundingRouter router;
    address distributor=address(0x1234);
    function setUp() public {
        if (block.chainid != 4663 || POOL.code.length == 0) {vm.skip(true);return;}
        venue=new UniV3SwapVenue(IUniswapV3PoolSwap(POOL),IWETH9(WETH));
        refPrice=new UniV3TwapReference(IUniswapV3PoolOracle(POOL),WETH,1800,100);
        vault=new PMFIArbVaultV3("Test pARB","pARB",USDG,vm.addr(123),address(this),address(0x5678),1e15,0);
        adapter=new ParbFundingAdapter(address(this),IERC20(USDG),IParbVault(address(vault)),distributor);
        router=new ArbitrageFundingRouter(address(this),IERC20(USDG),IParbFundingAdapter(address(adapter)),venue,refPrice,0.05 ether,2 ether,10 ether,100,3600);
        router.setOpenExecution(true);
        adapter.setOperator(address(router),true);
        vault.setDepositorRestricted(true);
        vault.setAllowedDepositor(address(adapter),true);
    }
    function fund(uint256 amount) internal {
        vm.deal(address(router),amount);
        uint256 minimum=amount*refPrice.assetPerEth()/1e18*9900/10000;
        vm.recordLogs();
        uint256 id=router.execute();
        assertGt(vm.getRecordedLogs().length,0,"execution emits receipt logs");
        uint256 received=IERC20(USDG).balanceOf(address(vault));
        assertGe(received,minimum);
        assertEq(router.totalAssetFunded(),received);
        assertEq(vault.totalPendingDepositAssets(),received);
        assertEq(IERC20(USDG).balanceOf(address(adapter)),0);
        assertEq(IERC20(WETH).balanceOf(address(venue)),0);
        PMFIArbVaultV3.ReportData memory r=PMFIArbVaultV3.ReportData(received,block.timestamp,block.timestamp+60,1);
        (uint8 v,bytes32 rr,bytes32 ss)=vm.sign(123,MessageHashUtils.toEthSignedMessageHash(vault.reportStructHash(r)));
        vault.report(r,abi.encodePacked(rr,ss,v),100,100);
        adapter.settle(id);
        assertEq(vault.balanceOf(distributor),received*1e12);
        vm.expectRevert(ParbFundingAdapter.AlreadySettled.selector);adapter.settle(id);
    }
    function test_fundingRealPoolMinimum() public {fund(0.05 ether);}
    function test_fundingRealPoolMaximum() public {fund(2 ether);}
    function test_fundingRollbackAndPermissions() public {
        vm.deal(address(this),2 ether);
        uint256 before=IERC20(USDG).balanceOf(address(this));
        vm.expectPartialRevert(UniV3SwapVenue.Slippage.selector);
        venue.swapExactETHForToken{value:0.05 ether}(USDG,type(uint256).max,address(this),block.timestamp);
        assertEq(IERC20(USDG).balanceOf(address(this)),before);
        assertEq(address(this).balance,2 ether);
        vm.expectRevert(UniV3SwapVenue.NotPool.selector);venue.uniswapV3SwapCallback(1,0,"");
        router.setOpenExecution(false);
        vm.prank(address(0xbad));vm.expectRevert(ArbitrageFundingRouter.NotKeeper.selector);router.execute();
        router.setOpenExecution(true);
        adapter.setOperator(address(router),false);
        vm.deal(address(router),0.05 ether);
        vm.expectRevert(ParbFundingAdapter.NotOperator.selector);router.execute();
        assertEq(address(router).balance,0.05 ether);
        assertEq(router.nonce(),0);
        assertEq(vault.totalPendingDepositAssets(),0);
    }
}
