cmake_minimum_required(VERSION 3.15)
project(alkimi_cpp_connectors VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ---------------------------------------------------------------------------
# Find Python + pybind11 (installed via pip)
# ---------------------------------------------------------------------------
find_package(Python3 REQUIRED COMPONENTS Interpreter Development)

execute_process(
    COMMAND "${Python3_EXECUTABLE}" -m pybind11 --cmakedir
    OUTPUT_VARIABLE pybind11_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
)
find_package(pybind11 REQUIRED HINTS "${pybind11_DIR}")

# ---------------------------------------------------------------------------
# Find OpenSSL (system — already installed)
# ---------------------------------------------------------------------------
find_package(OpenSSL REQUIRED)

# ---------------------------------------------------------------------------
# Fetch nlohmann/json (header-only, ~1 MB, downloaded once then cached)
# ---------------------------------------------------------------------------
include(FetchContent)
FetchContent_Declare(
    nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG        v3.11.3
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(nlohmann_json)

# ---------------------------------------------------------------------------
# Connector source files
# ---------------------------------------------------------------------------
set(CONNECTOR_SOURCES
    src/auth_utils.cpp
    src/net_utils.cpp
    src/kucoin_connector.cpp
    src/gate_connector.cpp
    src/mexc_connector.cpp
    src/kraken_connector.cpp
)

# ---------------------------------------------------------------------------
# pybind11 shared library module
# Output: alkimi_cpp_connectors.cpython-<ver>-<arch>.so
# ---------------------------------------------------------------------------
pybind11_add_module(alkimi_cpp_connectors
    bindings/bindings.cpp
    ${CONNECTOR_SOURCES}
)

target_include_directories(alkimi_cpp_connectors PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(alkimi_cpp_connectors PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    nlohmann_json::nlohmann_json
    Threads::Threads
)

find_package(Threads REQUIRED)

# ---------------------------------------------------------------------------
# Install: copy .so next to exchange/cpp_connector.py so Python can find it
# ---------------------------------------------------------------------------
install(
    TARGETS alkimi_cpp_connectors
    LIBRARY DESTINATION "${CMAKE_CURRENT_SOURCE_DIR}/../.."
)

# ---------------------------------------------------------------------------
# Test binary: standalone auth_utils verification (no pybind11 needed)
# ---------------------------------------------------------------------------
add_executable(test_auth_utils
    tests/test_auth_utils.cpp
    src/auth_utils.cpp
)

target_include_directories(test_auth_utils PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(test_auth_utils PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
)

# ---------------------------------------------------------------------------
# Test binary: kucoin connector unit tests (no live network required)
# ---------------------------------------------------------------------------
add_executable(test_kucoin
    tests/test_kucoin.cpp
    src/kucoin_connector.cpp
    src/net_utils.cpp
    src/auth_utils.cpp
)

target_include_directories(test_kucoin PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(test_kucoin PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    nlohmann_json::nlohmann_json
    Threads::Threads
)

# ---------------------------------------------------------------------------
# Test binary: gate connector unit tests (no live network required)
# ---------------------------------------------------------------------------
add_executable(test_gate
    tests/test_gate.cpp
    src/gate_connector.cpp
    src/net_utils.cpp
    src/auth_utils.cpp
)

target_include_directories(test_gate PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(test_gate PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    nlohmann_json::nlohmann_json
    Threads::Threads
)

# ---------------------------------------------------------------------------
# Test binary: mexc connector unit tests (no live network required)
# ---------------------------------------------------------------------------
add_executable(test_mexc
    tests/test_mexc.cpp
    src/mexc_connector.cpp
    src/net_utils.cpp
    src/auth_utils.cpp
)

target_include_directories(test_mexc PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(test_mexc PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    nlohmann_json::nlohmann_json
    Threads::Threads
)

# ---------------------------------------------------------------------------
# Test binary: kraken connector unit tests (no live network required)
# ---------------------------------------------------------------------------
add_executable(test_kraken
    tests/test_kraken.cpp
    src/kraken_connector.cpp
    src/net_utils.cpp
    src/auth_utils.cpp
)

target_include_directories(test_kraken PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(test_kraken PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    nlohmann_json::nlohmann_json
    Threads::Threads
)
