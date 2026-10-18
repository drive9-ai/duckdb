cmake_minimum_required(VERSION 3.15)
project(tidbfs)
find_package(CURL REQUIRED)
find_package(Threads REQUIRED)
find_package(Python3 REQUIRED COMPONENTS Interpreter)
set(CMAKE_CXX_STANDARD 17)
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/deps")
file(DOWNLOAD
  "https://raw.githubusercontent.com/nlohmann/json/v3.11.3/single_include/nlohmann/json.hpp"
  "${CMAKE_CURRENT_BINARY_DIR}/deps/json.hpp"
  EXPECTED_HASH SHA256=9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6
  TLS_VERIFY ON)
include_directories("${CMAKE_CURRENT_BINARY_DIR}/deps")
set(SOURCES src/tidbfs_extension.cpp)
if(APPLE)
  set(EXTENT_LIBRARY "${CMAKE_CURRENT_BINARY_DIR}/libtidbfs_extent.a")
else()
  set(EXTENT_LIBRARY "${CMAKE_CURRENT_BINARY_DIR}/libtidbfs_extent.so")
endif()
file(GLOB_RECURSE EXTENT_SOURCE "${CMAKE_CURRENT_SOURCE_DIR}/bridge/*.go")
add_custom_command(OUTPUT "${EXTENT_LIBRARY}"
  COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/scripts/build_extent.py" "${EXTENT_LIBRARY}"
  DEPENDS ${EXTENT_SOURCE} bridge/go.mod bridge/go.sum scripts/build_extent.py
  WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
add_custom_target(tidbfs_extent DEPENDS "${EXTENT_LIBRARY}")
build_loadable_extension(tidbfs "" ${SOURCES})
add_dependencies(tidbfs_loadable_extension tidbfs_extent)
target_link_libraries(tidbfs_loadable_extension "${EXTENT_LIBRARY}" CURL::libcurl Threads::Threads ${CMAKE_DL_LIBS} resolv)
if(APPLE)
  target_link_libraries(tidbfs_loadable_extension "-framework CoreFoundation" "-framework Security")
else()
  set_target_properties(tidbfs_loadable_extension PROPERTIES
    BUILD_WITH_INSTALL_RPATH TRUE INSTALL_RPATH "$ORIGIN")
endif()
