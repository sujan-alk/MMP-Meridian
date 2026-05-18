#pragma once

/**
 * auth_utils.h — Shared cryptographic and encoding utilities for all connectors.
 *
 * All 4 exchanges sign requests with HMAC, but with different algorithms,
 * encoding formats, and string-to-sign constructions:
 *
 *   KuCoin  — HMAC-SHA256 → base64   (3-part auth: key + secret + passphrase)
 *   Gate.io — HMAC-SHA512 → hex      (body hash = SHA512, sig = HMAC-SHA512)
 *   MEXC    — HMAC-SHA256 → hex      (standard 2-part auth)
 *   Kraken  — HMAC-SHA512 → base64   (secret is base64-encoded, decoded first)
 *
 * All functions return std::string. Hashes return raw bytes; call hex_encode()
 * or base64_encode() on the result as needed.
 */

#include <string>

// ---------------------------------------------------------------------------
// Hashing primitives
// ---------------------------------------------------------------------------

/** HMAC-SHA256(key, message) → raw 32-byte digest. */
std::string hmac_sha256(const std::string& key, const std::string& message);

/** HMAC-SHA512(key, message) → raw 64-byte digest. Used by Kraken. */
std::string hmac_sha512(const std::string& key, const std::string& message);

/** SHA256(message) → raw 32-byte digest. */
std::string sha256(const std::string& message);

/** SHA512(message) → raw 64-byte digest. Used by Gate.io body hashing. */
std::string sha512(const std::string& message);

// ---------------------------------------------------------------------------
// Encoding
// ---------------------------------------------------------------------------

/** Base64-encode raw bytes → ASCII string (no newlines). */
std::string base64_encode(const std::string& input);

/** Base64-decode ASCII string → raw bytes. */
std::string base64_decode(const std::string& input);

/** Hex-encode raw bytes → lowercase hex string. */
std::string hex_encode(const std::string& input);

/** Percent-encode a string for use in query strings / URLs. */
std::string url_encode(const std::string& input);

// ---------------------------------------------------------------------------
// Timestamps
// ---------------------------------------------------------------------------

/** Current Unix time in milliseconds as decimal string. e.g. "1714832400000" */
std::string timestamp_ms();

/** Current Unix time in seconds as decimal string. e.g. "1714832400" */
std::string timestamp_s();

// ---------------------------------------------------------------------------
// Per-exchange signature builders
// ---------------------------------------------------------------------------

/**
 * KuCoin v2 request signature.
 *
 * str_to_sign = timestamp + method.upper() + path + body
 * signature   = base64(HMAC-SHA256(api_secret, str_to_sign))
 *
 * Use for the KC-API-SIGN header.
 */
std::string kucoin_sign(
    const std::string& api_secret,
    const std::string& timestamp,
    const std::string& method,
    const std::string& path,
    const std::string& body = ""
);

/**
 * KuCoin passphrase signature (KC-API-KEY-VERSION: 2 requires this).
 *
 * signed_passphrase = base64(HMAC-SHA256(api_secret, passphrase))
 *
 * Use for the KC-API-PASSPHRASE header.
 */
std::string kucoin_sign_passphrase(
    const std::string& api_secret,
    const std::string& passphrase
);

/**
 * Gate.io v4 request signature.
 *
 * str_to_sign = method.upper() + "\n" + path + "\n" + query + "\n"
 *             + hex(SHA512(body)) + "\n" + timestamp
 * signature   = hex(HMAC-SHA512(api_secret, str_to_sign))
 *
 * Use for the SIGN header. Timestamp is Unix seconds string.
 */
std::string gate_sign(
    const std::string& api_secret,
    const std::string& method,
    const std::string& path,
    const std::string& query,
    const std::string& timestamp,
    const std::string& body = ""
);

/**
 * MEXC v3 request signature.
 *
 * query_string includes all params (sorted) + "&timestamp=..."
 * signature = hex(HMAC-SHA256(api_secret, query_string))
 *
 * Appended as ?signature=... to the request URL.
 */
std::string mexc_sign(
    const std::string& api_secret,
    const std::string& query_string
);

/**
 * Kraken REST signature.
 *
 * nonce       = timestamp_ms() (ever-increasing integer as string)
 * sha_input   = nonce + encoded_post_body
 * sha_result  = SHA256(sha_input)            — raw 32 bytes
 * message     = path + sha_result            — binary concatenation
 * signature   = base64(HMAC-SHA512(base64_decode(api_secret), message))
 *
 * Kraken's api_secret is itself a base64-encoded string — it is decoded
 * before use as the HMAC key.
 * Use for the API-Sign header.
 */
std::string kraken_sign(
    const std::string& api_secret,
    const std::string& path,
    const std::string& nonce,
    const std::string& encoded_post_body
);
