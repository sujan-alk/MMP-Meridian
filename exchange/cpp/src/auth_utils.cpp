/**
 * auth_utils.cpp — Full implementation of shared auth utilities.
 *
 * Cryptography: OpenSSL 3.x (EVP_MAC API for HMAC, EVP_MD_CTX for SHA256).
 * Encoding:     OpenSSL BIO for base64; hand-written for hex and URL-encode.
 * Timestamps:   std::chrono::system_clock.
 */

#include "auth_utils.h"

#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/params.h>
#include <openssl/bio.h>
#include <openssl/buffer.h>
#include <openssl/core_names.h>

#include <chrono>
#include <cctype>
#include <sstream>
#include <iomanip>
#include <stdexcept>
#include <string>

// ---------------------------------------------------------------------------
// Internal helper: generic HMAC using OpenSSL EVP_MAC (OpenSSL 3 API)
// ---------------------------------------------------------------------------

static std::string hmac_generic(
    const std::string& key,
    const std::string& message,
    const char* digest_name
) {
    EVP_MAC* mac = EVP_MAC_fetch(nullptr, "HMAC", nullptr);
    if (!mac) throw std::runtime_error("EVP_MAC_fetch(HMAC) failed");

    EVP_MAC_CTX* ctx = EVP_MAC_CTX_new(mac);
    EVP_MAC_free(mac);
    if (!ctx) throw std::runtime_error("EVP_MAC_CTX_new failed");

    // Set digest algorithm parameter
    OSSL_PARAM params[2];
    params[0] = OSSL_PARAM_construct_utf8_string(
        OSSL_MAC_PARAM_DIGEST,
        const_cast<char*>(digest_name),
        0
    );
    params[1] = OSSL_PARAM_construct_end();

    if (!EVP_MAC_init(ctx,
                      reinterpret_cast<const unsigned char*>(key.data()),
                      key.size(),
                      params)) {
        EVP_MAC_CTX_free(ctx);
        throw std::runtime_error(std::string("EVP_MAC_init failed for ") + digest_name);
    }

    if (!EVP_MAC_update(ctx,
                        reinterpret_cast<const unsigned char*>(message.data()),
                        message.size())) {
        EVP_MAC_CTX_free(ctx);
        throw std::runtime_error("EVP_MAC_update failed");
    }

    // Get output length first
    size_t out_len = 0;
    EVP_MAC_final(ctx, nullptr, &out_len, 0);

    std::string result(out_len, '\0');
    if (!EVP_MAC_final(ctx,
                       reinterpret_cast<unsigned char*>(result.data()),
                       &out_len,
                       out_len)) {
        EVP_MAC_CTX_free(ctx);
        throw std::runtime_error("EVP_MAC_final failed");
    }

    EVP_MAC_CTX_free(ctx);
    result.resize(out_len);
    return result;
}

// ---------------------------------------------------------------------------
// Public: hashing primitives
// ---------------------------------------------------------------------------

std::string hmac_sha256(const std::string& key, const std::string& message) {
    return hmac_generic(key, message, "SHA256");
}

std::string hmac_sha512(const std::string& key, const std::string& message) {
    return hmac_generic(key, message, "SHA512");
}

std::string sha256(const std::string& message) {
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    if (!ctx) throw std::runtime_error("EVP_MD_CTX_new failed");

    if (!EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr)) {
        EVP_MD_CTX_free(ctx);
        throw std::runtime_error("EVP_DigestInit_ex(SHA256) failed");
    }

    if (!EVP_DigestUpdate(ctx, message.data(), message.size())) {
        EVP_MD_CTX_free(ctx);
        throw std::runtime_error("EVP_DigestUpdate failed");
    }

    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int digest_len = 0;
    if (!EVP_DigestFinal_ex(ctx, digest, &digest_len)) {
        EVP_MD_CTX_free(ctx);
        throw std::runtime_error("EVP_DigestFinal_ex failed");
    }

    EVP_MD_CTX_free(ctx);
    return std::string(reinterpret_cast<char*>(digest), digest_len);
}

std::string sha512(const std::string& message) {
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    if (!ctx) throw std::runtime_error("EVP_MD_CTX_new failed");

    if (!EVP_DigestInit_ex(ctx, EVP_sha512(), nullptr)) {
        EVP_MD_CTX_free(ctx);
        throw std::runtime_error("EVP_DigestInit_ex(SHA512) failed");
    }

    if (!EVP_DigestUpdate(ctx, message.data(), message.size())) {
        EVP_MD_CTX_free(ctx);
        throw std::runtime_error("EVP_DigestUpdate failed");
    }

    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int digest_len = 0;
    if (!EVP_DigestFinal_ex(ctx, digest, &digest_len)) {
        EVP_MD_CTX_free(ctx);
        throw std::runtime_error("EVP_DigestFinal_ex failed");
    }

    EVP_MD_CTX_free(ctx);
    return std::string(reinterpret_cast<char*>(digest), digest_len);
}

// ---------------------------------------------------------------------------
// Public: encoding
// ---------------------------------------------------------------------------

std::string base64_encode(const std::string& input) {
    BIO* b64  = BIO_new(BIO_f_base64());
    BIO* bmem = BIO_new(BIO_s_mem());
    b64 = BIO_push(b64, bmem);

    // No newlines in output
    BIO_set_flags(b64, BIO_FLAGS_BASE64_NO_NL);
    BIO_write(b64, input.data(), static_cast<int>(input.size()));
    BIO_flush(b64);

    BUF_MEM* bptr = nullptr;
    BIO_get_mem_ptr(b64, &bptr);

    std::string result(bptr->data, bptr->length);
    BIO_free_all(b64);
    return result;
}

std::string base64_decode(const std::string& input) {
    BIO* b64  = BIO_new(BIO_f_base64());
    BIO* bmem = BIO_new_mem_buf(input.data(), static_cast<int>(input.size()));
    bmem = BIO_push(b64, bmem);

    BIO_set_flags(bmem, BIO_FLAGS_BASE64_NO_NL);

    // Decoded output is always shorter than or equal to input
    std::string result(input.size(), '\0');
    int decoded_len = BIO_read(bmem, result.data(), static_cast<int>(input.size()));
    BIO_free_all(bmem);

    if (decoded_len < 0) throw std::runtime_error("base64_decode: BIO_read failed");
    result.resize(static_cast<size_t>(decoded_len));
    return result;
}

std::string hex_encode(const std::string& input) {
    static constexpr char kHexChars[] = "0123456789abcdef";
    std::string result;
    result.reserve(input.size() * 2);
    for (unsigned char byte : input) {
        result += kHexChars[byte >> 4];
        result += kHexChars[byte & 0x0f];
    }
    return result;
}

std::string url_encode(const std::string& input) {
    static constexpr char kHexChars[] = "0123456789ABCDEF";
    std::string result;
    result.reserve(input.size() * 3);
    for (unsigned char c : input) {
        // RFC 3986 unreserved characters — pass through unchanged
        if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
            result += static_cast<char>(c);
        } else {
            result += '%';
            result += kHexChars[c >> 4];
            result += kHexChars[c & 0x0f];
        }
    }
    return result;
}

// ---------------------------------------------------------------------------
// Public: timestamps
// ---------------------------------------------------------------------------

std::string timestamp_ms() {
    using namespace std::chrono;
    auto now = system_clock::now().time_since_epoch();
    auto ms  = duration_cast<milliseconds>(now).count();
    return std::to_string(ms);
}

std::string timestamp_s() {
    using namespace std::chrono;
    auto now = system_clock::now().time_since_epoch();
    auto s   = duration_cast<seconds>(now).count();
    return std::to_string(s);
}

// ---------------------------------------------------------------------------
// Public: per-exchange signature builders
// ---------------------------------------------------------------------------

std::string kucoin_sign(
    const std::string& api_secret,
    const std::string& timestamp,
    const std::string& method,
    const std::string& path,
    const std::string& body
) {
    // KuCoin v2: sign_str = timestamp + METHOD_UPPER + path + body
    // All 4 parts are concatenated as plain strings (no separators).
    std::string str_to_sign = timestamp + method + path + body;
    return base64_encode(hmac_sha256(api_secret, str_to_sign));
}

std::string kucoin_sign_passphrase(
    const std::string& api_secret,
    const std::string& passphrase
) {
    // KC-API-KEY-VERSION: 2 requires the passphrase to also be HMAC-signed.
    return base64_encode(hmac_sha256(api_secret, passphrase));
}

std::string gate_sign(
    const std::string& api_secret,
    const std::string& method,
    const std::string& path,
    const std::string& query,
    const std::string& timestamp,
    const std::string& body
) {
    // Gate.io v4 uses SHA512 for body hash and HMAC-SHA512 for signature.
    // str_to_sign = METHOD\npath\nquery\nhex(SHA512(body))\ntimestamp
    std::string body_hash = hex_encode(sha512(body));
    std::string str_to_sign = method + "\n"
                            + path    + "\n"
                            + query   + "\n"
                            + body_hash + "\n"
                            + timestamp;
    return hex_encode(hmac_sha512(api_secret, str_to_sign));
}

std::string mexc_sign(
    const std::string& api_secret,
    const std::string& query_string
) {
    // MEXC v3: signature = hex(HMAC-SHA256(api_secret, query_string))
    // query_string already includes timestamp= and all sorted params.
    return hex_encode(hmac_sha256(api_secret, query_string));
}

std::string kraken_sign(
    const std::string& api_secret,
    const std::string& path,
    const std::string& nonce,
    const std::string& encoded_post_body
) {
    // Kraken:
    // 1. sha256_input  = nonce_string + encoded_post_body
    // 2. sha256_result = SHA256(sha256_input)                    -- raw 32 bytes
    // 3. message       = path + sha256_result                    -- binary concat
    // 4. decoded_key   = base64_decode(api_secret)
    // 5. signature     = base64(HMAC-SHA512(decoded_key, message))
    std::string sha256_input  = nonce + encoded_post_body;
    std::string sha256_result = sha256(sha256_input);
    std::string message       = path + sha256_result;
    std::string decoded_key   = base64_decode(api_secret);
    return base64_encode(hmac_sha512(decoded_key, message));
}
