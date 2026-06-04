/**
 * net_utils.cpp — TLS socket, HTTPS client, WebSocket framing.
 *
 * Shared by all 4 exchange connectors.
 * Uses OpenSSL 3.x + POSIX BSD sockets — no Boost, no extra deps.
 */

#include "net_utils.h"
#include "auth_utils.h"      // for base64_encode (WsClient upgrade key)

// OpenSSL
#include <openssl/err.h>
#include <openssl/rand.h>
#include <openssl/x509v3.h>

// POSIX
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
#include <netinet/tcp.h>

// STL
#include <algorithm>
#include <cerrno>
#include <cstring>
#include <sstream>
#include <stdexcept>
#include <string>

// =============================================================================
// TlsConn
// =============================================================================

void TlsConn::connect(const std::string& host, int port) {
    // Create TLS context with system CA verification
    ctx_ = SSL_CTX_new(TLS_client_method());
    if (!ctx_) throw std::runtime_error("SSL_CTX_new failed");
    SSL_CTX_set_default_verify_paths(ctx_);
    SSL_CTX_set_verify(ctx_, SSL_VERIFY_PEER, nullptr);

    // Resolve hostname to IP
    struct addrinfo hints{};
    hints.ai_family   = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    struct addrinfo* res = nullptr;
    std::string port_str = std::to_string(port);
    if (::getaddrinfo(host.c_str(), port_str.c_str(), &hints, &res) != 0 || !res)
        throw std::runtime_error("getaddrinfo failed for " + host);

    // TCP connect (non-blocking with 15-second timeout)
    fd_ = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd_ < 0) {
        ::freeaddrinfo(res);
        throw std::runtime_error("socket() failed: " + std::string(std::strerror(errno)));
    }

    // Disable Nagle algorithm — send each packet immediately without waiting
    // to batch with subsequent data. Critical for small frequent WebSocket
    // frames like order placement where every millisecond counts.
    int nodelay = 1;
    ::setsockopt(fd_, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

    int flags = ::fcntl(fd_, F_GETFL, 0);
    ::fcntl(fd_, F_SETFL, flags | O_NONBLOCK);

    int rc = ::connect(fd_, res->ai_addr, res->ai_addrlen);
    if (rc != 0 && errno != EINPROGRESS) {
        int saved = errno;
        ::freeaddrinfo(res);
        throw std::runtime_error("TCP connect to " + host +
                                 " failed: " + std::strerror(saved));
    }
    if (rc != 0) {
        fd_set wfds;
        FD_ZERO(&wfds);
        FD_SET(fd_, &wfds);
        struct timeval tv{ 15, 0 };  // 15-second connect timeout
        int sel = ::select(fd_ + 1, nullptr, &wfds, nullptr, &tv);
        if (sel <= 0) {
            ::freeaddrinfo(res);
            throw std::runtime_error("TCP connect to " + host + " timed out after 15s");
        }
        int err = 0;
        socklen_t errlen = sizeof(err);
        ::getsockopt(fd_, SOL_SOCKET, SO_ERROR, &err, &errlen);
        if (err != 0) {
            ::freeaddrinfo(res);
            throw std::runtime_error("TCP connect to " + host +
                                     " failed: " + std::strerror(err));
        }
    }
    ::freeaddrinfo(res);
    ::fcntl(fd_, F_SETFL, flags);  // restore blocking mode

    // TLS handshake
    ssl_ = SSL_new(ctx_);
    if (!ssl_) throw std::runtime_error("SSL_new failed");
    SSL_set_fd(ssl_, fd_);
    SSL_set_tlsext_host_name(ssl_, host.c_str());   // SNI

    // Enable hostname verification
    X509_VERIFY_PARAM* vpm = SSL_get0_param(ssl_);
    X509_VERIFY_PARAM_set1_host(vpm, host.c_str(), host.size());

    if (SSL_connect(ssl_) != 1) {
        throw std::runtime_error("SSL_connect to " + host + " failed: " + ssl_err_str());
    }
}

void TlsConn::write_all(const char* data, size_t len) {
    while (len > 0) {
        int n = SSL_write(ssl_, data,
                          static_cast<int>(std::min(len, size_t(16384))));
        if (n <= 0) throw std::runtime_error("SSL_write failed: " + ssl_err_str());
        data += n;
        len  -= static_cast<size_t>(n);
    }
}

void TlsConn::read_exact(void* buf, size_t n) {
    auto* p = static_cast<char*>(buf);
    size_t got = 0;
    while (got < n) {
        int r = SSL_read(ssl_, p + got, static_cast<int>(n - got));
        if (r <= 0) throw std::runtime_error("SSL_read failed (read_exact): " + ssl_err_str());
        got += static_cast<size_t>(r);
    }
}

int TlsConn::read_some(void* buf, int n, int timeout_ms) {
    // Check SSL internal buffer first (data already decrypted)
    if (SSL_pending(ssl_) > 0) {
        int r = SSL_read(ssl_, buf, n);
        return (r <= 0) ? -1 : r;
    }
    // Wait for socket to become readable
    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(fd_, &rfds);
    struct timeval tv{ timeout_ms / 1000, (timeout_ms % 1000) * 1000 };
    int sel = ::select(fd_ + 1, &rfds, nullptr, nullptr, &tv);
    if (sel == 0) return 0;   // timeout
    if (sel <  0) return -1;  // select error
    int r = SSL_read(ssl_, buf, n);
    return (r <= 0) ? -1 : r;
}

void TlsConn::close() {
    if (ssl_) { SSL_shutdown(ssl_); SSL_free(ssl_); ssl_ = nullptr; }
    if (ctx_) { SSL_CTX_free(ctx_); ctx_ = nullptr; }
    if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
}

std::string TlsConn::ssl_err_str() {
    unsigned long e = ERR_get_error();
    if (!e) return "(unknown ssl error)";
    char buf[256];
    ERR_error_string_n(e, buf, sizeof(buf));
    return std::string(buf);
}

// =============================================================================
// https_request
// =============================================================================

HttpResponse https_request(
    const std::string& host,
    const std::string& method,
    const std::string& path_and_query,
    const std::map<std::string, std::string>& headers,
    const std::string& body)
{
    TlsConn conn;
    conn.connect(host, 443);

    // Format HTTP/1.1 request
    std::ostringstream req;
    req << method << " " << path_and_query << " HTTP/1.1\r\n";
    req << "Host: " << host << "\r\n";
    req << "Connection: close\r\n";
    if (!body.empty()) req << "Content-Length: " << body.size() << "\r\n";
    for (auto& [k, v] : headers) req << k << ": " << v << "\r\n";
    req << "\r\n";
    if (!body.empty()) req << body;
    conn.write_all(req.str());

    // Helper: read a CRLF-terminated line from TLS conn
    auto read_line = [&]() -> std::string {
        std::string line;
        char c;
        while (true) {
            conn.read_exact(&c, 1);
            if (c == '\n') break;
            if (c != '\r') line += c;
        }
        return line;
    };

    // Parse status line → extract 3-digit code
    std::string status_line = read_line();
    int status = 0;
    {
        auto sp = status_line.find(' ');
        if (sp != std::string::npos) status = std::stoi(status_line.substr(sp + 1, 3));
    }

    // Parse headers (case-insensitive for transfer-encoding / content-length)
    std::string transfer_encoding;
    long content_length = -1;
    while (true) {
        std::string line = read_line();
        if (line.empty()) break;
        auto colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string name  = line.substr(0, colon);
        std::string value = line.substr(colon + 1);
        if (!value.empty() && value[0] == ' ') value = value.substr(1);
        std::transform(name.begin(), name.end(), name.begin(), ::tolower);
        if (name == "content-length")    content_length    = std::stol(value);
        if (name == "transfer-encoding") transfer_encoding = value;
    }

    // Read body
    std::string resp_body;
    if (!transfer_encoding.empty() &&
        transfer_encoding.find("chunked") != std::string::npos)
    {
        while (true) {
            std::string size_line = read_line();
            size_t chunk_size = std::stoul(size_line, nullptr, 16);
            if (chunk_size == 0) break;
            std::vector<char> chunk(chunk_size);
            conn.read_exact(chunk.data(), chunk_size);
            resp_body.append(chunk.data(), chunk_size);
            read_line();  // trailing \r\n
        }
    } else if (content_length >= 0) {
        resp_body.resize(static_cast<size_t>(content_length));
        if (content_length > 0)
            conn.read_exact(&resp_body[0], static_cast<size_t>(content_length));
    } else {
        // No Content-Length and not chunked: read until server closes
        char buf[4096];
        while (true) {
            int r = conn.read_some(buf, sizeof(buf), 5000);
            if (r <= 0) break;
            resp_body.append(buf, static_cast<size_t>(r));
        }
    }

    return {status, resp_body};
}

// =============================================================================
// WsClient
// =============================================================================

void WsClient::connect(const std::string& host, const std::string& path) {
    tls_.connect(host, 443);

    // Generate random 16-byte WebSocket key and base64-encode it
    unsigned char key_bytes[16];
    RAND_bytes(key_bytes, 16);
    std::string ws_key = base64_encode(
        std::string(reinterpret_cast<char*>(key_bytes), 16));

    // HTTP/1.1 Upgrade request
    std::ostringstream req;
    req << "GET " << path << " HTTP/1.1\r\n";
    req << "Host: " << host << "\r\n";
    req << "Upgrade: websocket\r\n";
    req << "Connection: Upgrade\r\n";
    req << "Sec-WebSocket-Key: " << ws_key << "\r\n";
    req << "Sec-WebSocket-Version: 13\r\n";
    req << "\r\n";
    tls_.write_all(req.str());

    // Read and verify 101 Switching Protocols
    auto read_line = [this]() -> std::string {
        std::string line;
        char c;
        while (true) {
            tls_.read_exact(&c, 1);
            if (c == '\n') break;
            if (c != '\r') line += c;
        }
        return line;
    };

    std::string status_line = read_line();
    if (status_line.find("101") == std::string::npos)
        throw std::runtime_error("WS upgrade failed: " + status_line);

    while (true) {           // drain remaining headers
        if (read_line().empty()) break;
    }

    connected_ = true;
}

void WsClient::send_text(const std::string& msg) {
    send_frame(OP_TEXT, msg.data(), msg.size());
}

void WsClient::send_ping() {
    send_frame(OP_PING, nullptr, 0);
}

std::string WsClient::recv_msg(int timeout_ms) {
    std::string payload;
    while (true) {
        int op = recv_frame(payload, timeout_ms);
        if (op < 0)             return "";   // timeout
        if (op == OP_PING) {
            send_frame(OP_PONG, payload.data(), payload.size());
            continue;
        }
        if (op == OP_PONG)  continue;
        if (op == OP_CLOSE) throw std::runtime_error("WS server closed connection");
        if (op == OP_TEXT || op == OP_BINARY) return payload;
    }
}

void WsClient::send_frame(uint8_t opcode, const char* data, size_t len) {
    // Frame header
    std::vector<uint8_t> hdr;
    hdr.push_back(0x80u | (opcode & 0x0Fu));   // FIN=1

    // Payload length (with MASK bit = 1 for client frames)
    if (len < 126) {
        hdr.push_back(0x80u | static_cast<uint8_t>(len));
    } else if (len < 65536) {
        hdr.push_back(0x80u | 126u);
        hdr.push_back(static_cast<uint8_t>((len >> 8) & 0xFF));
        hdr.push_back(static_cast<uint8_t>(len & 0xFF));
    } else {
        hdr.push_back(0x80u | 127u);
        for (int i = 7; i >= 0; --i)
            hdr.push_back(static_cast<uint8_t>((len >> (i * 8)) & 0xFF));
    }

    // 4-byte random masking key
    uint8_t mask[4];
    RAND_bytes(mask, 4);
    for (uint8_t b : mask) hdr.push_back(b);
    tls_.write_all(reinterpret_cast<char*>(hdr.data()), hdr.size());

    if (len == 0) return;

    // Masked payload
    std::vector<uint8_t> masked(len);
    for (size_t i = 0; i < len; i++)
        masked[i] = static_cast<uint8_t>(data[i]) ^ mask[i % 4];
    tls_.write_all(reinterpret_cast<char*>(masked.data()), len);
}

int WsClient::recv_frame(std::string& out, int timeout_ms) {
    out.clear();
    std::string assembled;
    int final_opcode = -1;

    while (true) {
        // First byte: FIN + opcode
        uint8_t b0 = 0;
        {
            int r = tls_.read_some(&b0, 1, timeout_ms);
            if (r == 0) return -1;   // timeout
            if (r <  0) throw std::runtime_error("WS recv error on first byte");
        }
        bool    fin    = (b0 & 0x80u) != 0;
        uint8_t opcode = b0 & 0x0Fu;

        // Second byte: MASK + payload length
        uint8_t b1 = 0;
        tls_.read_exact(&b1, 1);
        bool     server_masked = (b1 & 0x80u) != 0;
        uint64_t payload_len   = b1 & 0x7Fu;

        if (payload_len == 126) {
            uint8_t ext[2]; tls_.read_exact(ext, 2);
            payload_len = (uint64_t(ext[0]) << 8) | ext[1];
        } else if (payload_len == 127) {
            uint8_t ext[8]; tls_.read_exact(ext, 8);
            payload_len = 0;
            for (int i = 0; i < 8; i++) payload_len = (payload_len << 8) | ext[i];
        }

        // Masking key (server-to-client frames must NOT be masked per RFC,
        // but handle defensively)
        uint8_t mask_key[4] = {};
        if (server_masked) tls_.read_exact(mask_key, 4);

        // Payload
        std::string fragment(payload_len, '\0');
        if (payload_len > 0) tls_.read_exact(&fragment[0], payload_len);
        if (server_masked)
            for (size_t i = 0; i < payload_len; i++)
                fragment[i] ^= static_cast<char>(mask_key[i % 4]);

        // Control frames can't be fragmented → return immediately
        if (opcode == OP_PING || opcode == OP_PONG || opcode == OP_CLOSE) {
            out = fragment;
            return opcode;
        }

        // Data frame (text/binary) or continuation
        if (opcode != OP_CONTINUATION) final_opcode = opcode;
        assembled += fragment;

        if (fin) {
            out = assembled;
            return final_opcode;
        }
        // else: fragmented — keep reading continuation frames
    }
}
