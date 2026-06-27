(function () {
    "use strict";

    var BUILDER_CODE = "bc_uyxykegl";

    // ERC-8021 Schema 0:
    // code bytes + code length + schema ID + repeated 8021 marker
    var DATA_SUFFIX =
        "0x62635f757978796b65676c0b0080218021802180218021802180218021";

    var wrappedSigners = new WeakSet();

    function appendSuffix(existingData, suffix) {
        var base =
            typeof existingData === "string" &&
            existingData.startsWith("0x")
                ? existingData.slice(2)
                : "";

        var suffixHex =
            suffix.startsWith("0x")
                ? suffix.slice(2)
                : suffix;

        if (
            base
                .toLowerCase()
                .endsWith(suffixHex.toLowerCase())
        ) {
            return "0x" + base;
        }

        return "0x" + base + suffixHex;
    }

    function wrapSigner(signer) {
        if (
            !signer ||
            typeof signer.sendTransaction !== "function"
        ) {
            return signer;
        }

        if (wrappedSigners.has(signer)) {
            return signer;
        }

        var originalSend =
            signer.sendTransaction.bind(signer);

        signer.sendTransaction =
            async function (transaction) {
                var patched = Object.assign(
                    {},
                    transaction || {}
                );

                patched.data = appendSuffix(
                    patched.data || "0x",
                    DATA_SUFFIX
                );

                return originalSend(patched);
            };

        wrappedSigners.add(signer);

        return signer;
    }

    window.__builderAttribution = {
        builderCode: BUILDER_CODE,
        dataSuffix: DATA_SUFFIX,
        wrapSigner: wrapSigner
    };
})();
