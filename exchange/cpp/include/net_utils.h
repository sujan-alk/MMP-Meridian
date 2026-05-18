#pragma once

/**
 * net_utils.h — Shared TLS + HTTP + WebSocket transport layer.
 *
 * Used by all 4 exchange connectors (KuCoin, Gate.io, MEXC, Kraken).
 * Implementations in net_utils.cpp.
 *
 * Stack: OpenSSL 3.x for TLS, POSIX sockets for TCP.  No Boost required.
 *
 * Classes:
 *   TlsConn     — raw TLS TCP connection (connect, read, write, close)
 *   WsClient    — RFC 6455 WebSocket over TLS (connect, send, recv, ping)
 *
 * Function:
 *   https_request — single-shot HTTPS call; opens a fresh TlsConn per call
 */

#include <openssl/ssl.h>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

// ---------------------------------------------------------------------------
// TlsConn — OpenSSL TLS socket (not copyable, not thread-safe)
// ---------------------------------------------------------------------------

class TlsConn {
public:
    TlsConn()  = default;
    ~TlsConn() { close(); }

    TlsConn(const TlsConn&)            = delete;
    TlsConn& operator=(const TlsConn&) = delete;

    /** Open TCP + TLS to host:port (blocks until handshake complete). */
    void connect(const std::string& host, int port);

    /** Write all bytes, retrying on partial writes. */
    void write_all(const char* data, size_t len);
    void write_all(const std::string& s) { write_all(s.data(), s.size()); }

    /** Read exactly n bytes; throws on EOF or error. */
    void read_exact(void* buf, size_t n);

    /**
     * Read up to n bytes waiting at most timeout_ms.
     * Returns 0 = timeout, -1 = error, >0 = bytes read.
     */
    int read_some(void* buf, int n, int timeout_ms);

    /** Graceful TLS shutdown + socket close. Safe to call multiple times. */
    void close();

    bool is_open() const { return ssl_ != nullptr; }

private:
    SSL_CTX* ctx_ = nullptr;
    SSL*     ssl_ = nullptr;
    int      fd_  = -1;

    static std::string ssl_err_str();
};

// ---------------------------------------------------------------------------
// https_request — one-shot HTTPS call (new TlsConn per call)
// ---------------------------------------------------------------------------

struct HttpResponse {
    int         status = 0;  // HTTP status code (0 = connection failed)
    std::string body;
};

HttpResponse https_request(
    const std::string& host,
    const std::string& method,
    const std::string& path_and_query,
    const std::map<std::string, std::string>& headers,
    const std::string& body);

// ---------------------------------------------------------------------------
// WsClient — RFC 6455 WebSocket over TLS (not thread-safe)
// ---------------------------------------------------------------------------

class WsClient {
public:
    WsClient()  = default;
    ~WsClient() = default;

    WsClient(const WsClient&)            = delete;
    WsClient& operator=(const WsClient&) = delete;

    /**
     * Perform HTTP/1.1 Upgrade and enter WebSocket state.
     * host: bare hostname, e.g. "ws-api-spot.kucoin.com"
     * path: full path with query, e.g. "/?token=xxx&connectId=yyy"
     */
    void connect(const std::string& host, const std::string& path);

    /** Send a UTF-8 text frame (client MUST mask per RFC 6455 §5.3). */
    void send_text(const std::string& msg);

    /** Send a WebSocket-level ping frame. */
    void send_ping();

    /**
     * Receive next complete data (text/binary) message.
     * Handles fragmented frames (reassembly), auto-replies to server pings.
     * Returns "" on timeout.  Throws on connection error or server close.
     */
    std::string recv_msg(int timeout_ms = 5000);

    bool is_open() const { return connected_; }

private:
    TlsConn     tls_;
    bool        connected_ = false;

    // Opcodes
    static constexpr uint8_t OP_CONTINUATION = 0x00;
    static constexpr uint8_t OP_TEXT         = 0x01;
    static constexpr uint8_t OP_BINARY       = 0x02;
    static constexpr uint8_t OP_CLOSE        = 0x08;
    static constexpr uint8_t OP_PING         = 0x09;
    static constexpr uint8_t OP_PONG         = 0x0A;

    void send_frame(uint8_t opcode, const char* data, size_t len);

    /**
     * Receive one WebSocket frame (with reassembly of continuation frames).
     * Returns opcode, fills `out` with payload.
     * Returns -1 on timeout.  Throws on error or close.
     */
    int recv_frame(std::string& out, int timeout_ms);
};
