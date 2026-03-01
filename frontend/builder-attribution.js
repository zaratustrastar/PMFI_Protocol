(function () {
    var BUILDER_CODE = "bc_uyxykegl";

    function hexToBytes(hex) {
        if (hex.startsWith("0x")) hex = hex.slice(2);
        var bytes = new Uint8Array(hex.length / 2);
        for (var i = 0; i < bytes.length; i++) {
            bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
        }
        return bytes;
    }

    function bytesToHex(bytes) {
        return "0x" + Array.from(bytes).map(function (b) {
            return b.toString(16).padStart(2, "0");
        }).join("");
    }

    function encodeBuilderCode(code) {
        var encoder = new TextEncoder();
        var codeBytes = encoder.encode(code);
        var result = new Uint8Array(4 + codeBytes.length);
        var view = new DataView(result.buffer);
        view.setUint32(0, codeBytes.length, false);
        result.set(codeBytes, 4);
        return result;
    }

    function buildDataSuffix(codes) {
        var ERC_8021_MAGIC = new Uint8Array([0x45, 0x52, 0x43, 0x2d, 0x38, 0x30, 0x32, 0x31]);
        var VERSION = new Uint8Array([0x01]);
        var count = new Uint8Array(1);
        count[0] = codes.length;

        var encoded = codes.map(encodeBuilderCode);
        var totalLen = ERC_8021_MAGIC.length + VERSION.length + count.length;
        for (var i = 0; i < encoded.length; i++) totalLen += encoded[i].length;

        var result = new Uint8Array(totalLen);
        var offset = 0;
        result.set(ERC_8021_MAGIC, offset); offset += ERC_8021_MAGIC.length;
        result.set(VERSION, offset); offset += VERSION.length;
        result.set(count, offset); offset += count.length;
        for (var j = 0; j < encoded.length; j++) {
            result.set(encoded[j], offset);
            offset += encoded[j].length;
        }
        return bytesToHex(result);
    }

    function appendSuffix(existingData, suffix) {
        if (!existingData || existingData === "0x" || existingData === "") {
            return suffix;
        }
        var base = existingData.startsWith("0x") ? existingData.slice(2) : existingData;
        var suf = suffix.startsWith("0x") ? suffix.slice(2) : suffix;
        return "0x" + base + suf;
    }

    function wrapSigner(signer) {
        if (!signer || !window.__builderAttribution || !window.__builderAttribution.dataSuffix) return signer;
        var dataSuffix = window.__builderAttribution.dataSuffix;
        var origSend = signer.sendTransaction.bind(signer);
        signer.sendTransaction = function (tx) {
            try {
                var patched = Object.assign({}, tx);
                patched.data = appendSuffix(tx.data || "0x", dataSuffix);
                return origSend(patched);
            } catch (e) {
                console.warn("[Attribution] Could not append suffix:", e);
                return origSend(tx);
            }
        };
        return signer;
    }

    try {
        if (!BUILDER_CODE || BUILDER_CODE === "bc_REPLACE_ME") {
            console.warn("[Attribution] Builder code not set. Set BUILDER_CODE in builder-attribution.js to enable ERC-8021 attribution.");
            window.__builderAttribution = { dataSuffix: null, wrapSigner: function (s) { return s; } };
            return;
        }
        var dataSuffix = buildDataSuffix([BUILDER_CODE]);
        window.__builderAttribution = { dataSuffix: dataSuffix, wrapSigner: wrapSigner };
        if (typeof window !== "undefined" && window.location && window.location.hostname !== "app.pmfi.cc") {
            console.log("[Attribution] DATA_SUFFIX ready, length:", dataSuffix.length, "bytes");
        }
    } catch (e) {
        console.warn("[Attribution] Failed to initialize:", e);
        window.__builderAttribution = { dataSuffix: null, wrapSigner: function (s) { return s; } };
    }
})();
