cmake_minimum_required(VERSION 3.15)
project(tidbfs)
find_package(CURL REQUIRED)
find_package(Threads REQUIRED)
set(CMAKE_CXX_STANDARD 17)
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/deps")
file(DOWNLOAD
  "https://raw.githubusercontent.com/nlohmann/json/v3.11.3/single_include/nlohmann/json.hpp"
  "${CMAKE_CURRENT_BINARY_DIR}/deps/json.hpp"
  EXPECTED_HASH SHA256=9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6
  TLS_VERIFY ON)
include_directories("${CMAKE_CURRENT_BINARY_DIR}/deps")
set(SOURCES src/tidbfs_extension.cpp)
build_loadable_extension(tidbfs "" ${SOURCES})
target_link_libraries(tidbfs_loadable_extension CURL::libcurl Threads::Threads)
