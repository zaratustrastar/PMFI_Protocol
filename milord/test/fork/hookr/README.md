# Vendored Hookr-1 interfaces (fork tests only)

Copied verbatim from github.com/cholakovvv/hookr-contracts at commit 7e771b3b244c812c699abe3e74d166289709ff0f,
folder `hookr-1/src` (MIT licence, Copyright (c) 2026 Hookr). Only the interfaces and types the fork suite needs to
call the deployed Robinhood Chain (4663) contracts. No Hookr implementation code is compiled into this repository.

- interfaces/IHookrLauncher.sol, IHookrLauncherView.sol, IHookrRegistry.sol, IHookrFactoryRegistry.sol,
  IHookrFamilyLock.sol, IHookrLaunchFee.sol
- types/HookrTypes.sol

`../HookrForkInterfaces.sol` holds the few extra view/claim functions (HookrRules, HookrDirectionalTax,
HookrTaxQueue) declared by hand from the same commit's sources.
