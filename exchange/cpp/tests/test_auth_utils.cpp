/**
 * test_auth_utils.cpp — Standalone verification of auth_utils against known vectors.
 *
 * RFC 4231 test vectors for HMAC-SHA256 and HMAC-SHA512.
 * SHA-256 empty-string vector from FIPS 180-4.
 * Base64 / hex / URL-encode vectors from RFC 4648 / RFC 3986.
 * Per-exchange signature vectors are synthetic (hand-calculated from spec).
 *
 * Build: see CMakeLists.txt (target test_auth_utils)
 * Run:   ./test_auth_utils
 */

#include "auth_utils.h"

#include <cassert>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <stdexcept>

// ---------------------------------------------------------------------------
// Tiny test framework
// ---------------------------------------------------------------------------

static int g_total  = 0;
static int g_passed = 0;

static void check(const std::string& name, bool ok) {
    ++g_total;
    if (ok) {
        ++g_passed;
        std::printf("  PASS  %s\n", name.c_str());
    } else {
        std::printf("  FAIL  %s\n", name.c_str());
    }
}

static void check_eq(const std::string& name, const std::string& got, const std::string& expected) {
    bool ok = (got == expected);
    ++g_total;
    if (ok) {
        ++g_passed;
        std::printf("  PASS  %s\n", name.c_str());
    } else {
        std::printf("  FAIL  %s\n  got      : %s\n  expected : %s\n",
                    name.c_str(), got.c_str(), expected.c_str());
    }
}

// Build a raw-byte string from a hex literal (e.g. "0b0b0b...")
static std::string from_hex(const std::string& hex) {
    std::string result;
    result.reserve(hex.size() / 2);
    for (size_t i = 0; i + 1 < hex.size(); i += 2) {
        unsigned int byte = 0;
        std::sscanf(hex.c_str() + i, "%02x", &byte);
        result += static_cast<char>(byte);
    }
    return result;
}

// Build a raw-byte string by repeating a single byte N times
static std::string repeat_byte(unsigned char b, size_t n) {
    return std::string(n, static_cast<char>(b));
}

// ---------------------------------------------------------------------------
// Test groups
// ---------------------------------------------------------------------------

static void test_hmac_sha256() {
    std::printf("\n[HMAC-SHA256 — RFC 4231 vectors]\n");

    // TC1: key = 20 × 0x0b, data = "Hi There"
    {
        std::string key = repeat_byte(0x0b, 20);
        std::string msg = "Hi There";
        std::string got = hex_encode(hmac_sha256(key, msg));
        check_eq("TC1 Hi There",
                 got,
                 "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7");
    }

    // TC2: key = "Jefe", data = "what do ya want for nothing?"
    {
        std::string key = "Jefe";
        std::string msg = "what do ya want for nothing?";
        std::string got = hex_encode(hmac_sha256(key, msg));
        check_eq("TC2 Jefe",
                 got,
                 "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
    }

    // TC3: key = 20 × 0xaa, data = 50 × 0xdd
    {
        std::string key = repeat_byte(0xaa, 20);
        std::string msg = repeat_byte(0xdd, 50);
        std::string got = hex_encode(hmac_sha256(key, msg));
        check_eq("TC3 0xdd*50",
                 got,
                 "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe");
    }
}

static void test_hmac_sha512() {
    std::printf("\n[HMAC-SHA512 — RFC 4231 vectors]\n");

    // TC1: key = 20 × 0x0b, data = "Hi There"
    {
        std::string key = repeat_byte(0x0b, 20);
        std::string msg = "Hi There";
        std::string got = hex_encode(hmac_sha512(key, msg));
        check_eq("TC1 Hi There",
                 got,
                 "87aa7cdea5ef619d4ff0b4241a1d6cb02379f4e2ce4ec2787ad0b30545e17cd"
                 "edaa833b7d6b8a702038b274eaea3f4e4be9d914eeb61f1702e696c203a126854");
    }

    // TC2: key = "Jefe", data = "what do ya want for nothing?"
    {
        std::string key = "Jefe";
        std::string msg = "what do ya want for nothing?";
        std::string got = hex_encode(hmac_sha512(key, msg));
        check_eq("TC2 Jefe",
                 got,
                 "164b7a7bfcf819e2e395fbe73b56e0a387bd64222e831fd610270cd7ea2505549758bf75c05a994a6d034f65f8f0e6fdcaeab1a34d4a6b4b636e070a38bce737");
    }

    // TC3: key = 20 × 0xaa, data = 50 × 0xdd
    {
        std::string key = repeat_byte(0xaa, 20);
        std::string msg = repeat_byte(0xdd, 50);
        std::string got = hex_encode(hmac_sha512(key, msg));
        check_eq("TC3 0xdd*50",
                 got,
                 "fa73b0089d56a284efb0f0756c890be9b1b5dbdd8ee81a3655f83e33b2279d3"
                 "9bf3e848279a722c806b485a47e67c807b946a337bee8942674278859e13292fb");
    }
}

static void test_sha256() {
    std::printf("\n[SHA-256 — FIPS 180-4 vectors]\n");

    // Empty string
    {
        std::string got = hex_encode(sha256(""));
        check_eq("SHA256 empty",
                 got,
                 "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    }

    // "abc"
    {
        std::string got = hex_encode(sha256("abc"));
        check_eq("SHA256 abc",
                 got,
                 "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    }

    // "The quick brown fox jumps over the lazy dog"
    {
        std::string got = hex_encode(sha256("The quick brown fox jumps over the lazy dog"));
        check_eq("SHA256 fox",
                 got,
                 "d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592");
    }
}

static void test_base64() {
    std::printf("\n[Base64 — RFC 4648]\n");

    struct { const char* plain; const char* encoded; } cases[] = {
        { "",           ""             },
        { "f",          "Zg=="         },
        { "fo",         "Zm8="         },
        { "foo",        "Zm9v"         },
        { "foobar",     "Zm9vYmFy"     },
        { "Hello, World!", "SGVsbG8sIFdvcmxkIQ==" },
    };

    for (auto& c : cases) {
        std::string enc = base64_encode(c.plain);
        check_eq(std::string("b64_encode \"") + c.plain + "\"", enc, c.encoded);

        std::string dec = base64_decode(c.encoded);
        check_eq(std::string("b64_decode \"") + c.encoded + "\"", dec, c.plain);
    }

    // Round-trip with binary data
    std::string binary = from_hex("deadbeefcafebabe");
    std::string rt     = base64_decode(base64_encode(binary));
    check("base64 binary round-trip", rt == binary);
}

static void test_hex_encode() {
    std::printf("\n[hex_encode]\n");

    check_eq("hex empty",    hex_encode(""),              "");
    check_eq("hex \\x00",   hex_encode(std::string(1, '\x00')), "00");
    check_eq("hex \\xff",   hex_encode(std::string(1, '\xff')), "ff");
    check_eq("hex deadbeef",hex_encode(from_hex("deadbeef")),   "deadbeef");
}

static void test_url_encode() {
    std::printf("\n[url_encode — RFC 3986]\n");

    // Unreserved chars pass through unchanged
    check_eq("url alnum",   url_encode("abc123"), "abc123");
    check_eq("url tilde",   url_encode("~"),      "~");
    check_eq("url dash",    url_encode("-"),       "-");
    check_eq("url dot",     url_encode("."),       ".");
    check_eq("url uscore",  url_encode("_"),       "_");

    // Reserved / special chars must be percent-encoded (uppercase hex)
    check_eq("url space",   url_encode(" "),       "%20");
    check_eq("url slash",   url_encode("/"),       "%2F");
    check_eq("url amp",     url_encode("&"),       "%26");
    check_eq("url equals",  url_encode("="),       "%3D");
    check_eq("url plus",    url_encode("+"),       "%2B");
    check_eq("url at",      url_encode("@"),       "%40");

    // Full query-string example
    check_eq("url query",
             url_encode("symbol=BTC/USDT&side=buy"),
             "symbol%3DBTC%2FUSDT%26side%3Dbuy");
}

static void test_timestamps() {
    std::printf("\n[timestamps]\n");

    // We can only sanity-check: ms > s, both > 0, reasonable magnitude.
    std::string ms_str = timestamp_ms();
    std::string s_str  = timestamp_s();

    long long ms = std::stoll(ms_str);
    long long s  = std::stoll(s_str);

    // Unix time in ms should be > 1.7 trillion (year 2024)
    check("timestamp_ms magnitude", ms > 1'700'000'000'000LL);
    check("timestamp_s  magnitude", s  > 1'700'000'000LL);
    check("ms > s (approx)", ms / 1000 == s || ms / 1000 == s + 1 || ms / 1000 == s - 1);
    check("ms string non-empty",  !ms_str.empty());
    check("s  string non-empty",  !s_str.empty());
}

static void test_exchange_signs() {
    std::printf("\n[per-exchange sign functions]\n");

    // --- KuCoin ---
    // Synthetic: sign("secret", "1234567890", "GET", "/api/v1/accounts", "")
    // str = "1234567890GET/api/v1/accounts"
    // Expected = base64(hmac_sha256("secret", str))
    {
        std::string secret    = "secret";
        std::string ts        = "1234567890";
        std::string method    = "GET";
        std::string path      = "/api/v1/accounts";
        std::string body      = "";
        std::string computed  = kucoin_sign(secret, ts, method, path, body);

        // Independently derive: str_to_sign = ts+method+path+body
        std::string str_to_sign = ts + method + path + body;
        std::string expected    = base64_encode(hmac_sha256(secret, str_to_sign));
        check_eq("kucoin_sign basic", computed, expected);

        // Non-empty body
        std::string body2     = R"({"size":"1","price":"100"})";
        std::string comp2     = kucoin_sign(secret, ts, "POST", "/api/v1/orders", body2);
        std::string exp2      = base64_encode(hmac_sha256(secret, ts + "POST/api/v1/orders" + body2));
        check_eq("kucoin_sign with body", comp2, exp2);
    }

    // --- KuCoin passphrase ---
    {
        std::string secret     = "my_api_secret";
        std::string passphrase = "my_passphrase";
        std::string computed   = kucoin_sign_passphrase(secret, passphrase);
        std::string expected   = base64_encode(hmac_sha256(secret, passphrase));
        check_eq("kucoin_sign_passphrase", computed, expected);
    }

    // --- Gate.io ---
    // str = METHOD\npath\nquery\nhex(sha256(body))\ntimestamp
    {
        std::string secret    = "gate_secret";
        std::string method    = "GET";
        std::string path      = "/api/v4/spot/tickers";
        std::string query     = "currency_pair=BTC_USDT";
        std::string ts        = "1714832400";
        std::string body      = "";
        std::string computed  = gate_sign(secret, method, path, query, ts, body);

        std::string body_hash  = hex_encode(sha256(body));
        std::string str_to_sign = method + "\n" + path + "\n" + query + "\n" + body_hash + "\n" + ts;
        std::string expected    = hex_encode(hmac_sha256(secret, str_to_sign));
        check_eq("gate_sign GET", computed, expected);
    }
    {
        std::string secret    = "gate_secret";
        std::string body      = R"({"currency_pair":"BTC_USDT","type":"limit","side":"buy","amount":"1","price":"60000"})";
        std::string ts        = "1714832400";
        std::string computed  = gate_sign(secret, "POST", "/api/v4/spot/orders", "", ts, body);

        std::string body_hash   = hex_encode(sha256(body));
        std::string str_to_sign = std::string("POST") + "\n/api/v4/spot/orders\n\n" + body_hash + "\n" + ts;
        std::string expected    = hex_encode(hmac_sha256(secret, str_to_sign));
        check_eq("gate_sign POST with body", computed, expected);
    }

    // --- MEXC ---
    {
        std::string secret = "mexc_secret";
        std::string qstr   = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=60000&timestamp=1714832400000";
        std::string computed = mexc_sign(secret, qstr);
        std::string expected = hex_encode(hmac_sha256(secret, qstr));
        check_eq("mexc_sign", computed, expected);
    }

    // --- Kraken ---
    {
        // api_secret must be base64-encoded (as Kraken provides it)
        std::string raw_secret    = "kraken_raw_secret_32bytes_xxxxxxx";
        std::string api_secret    = base64_encode(raw_secret);   // as provided by Kraken
        std::string path          = "/0/private/AddOrder";
        std::string nonce         = "1714832400000";
        std::string post_body     = "nonce=1714832400000&ordertype=limit&type=buy&volume=1&pair=XBTUSDT&price=60000";
        std::string computed      = kraken_sign(api_secret, path, nonce, post_body);

        // Hand-derive
        std::string sha_input  = nonce + post_body;
        std::string sha_result = sha256(sha_input);
        std::string message    = path + sha_result;
        std::string dk         = base64_decode(api_secret);
        std::string expected   = base64_encode(hmac_sha512(dk, message));
        check_eq("kraken_sign", computed, expected);
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

int main() {
    std::printf("=== auth_utils test suite ===\n");

    try {
        test_hmac_sha256();
        test_hmac_sha512();
        test_sha256();
        test_base64();
        test_hex_encode();
        test_url_encode();
        test_timestamps();
        test_exchange_signs();
    } catch (const std::exception& e) {
        std::printf("\nFATAL exception: %s\n", e.what());
        return 1;
    }

    std::printf("\n=== Results: %d / %d passed ===\n", g_passed, g_total);
    return (g_passed == g_total) ? 0 : 1;
}
