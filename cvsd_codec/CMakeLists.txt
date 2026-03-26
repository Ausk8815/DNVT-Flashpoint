cmake_minimum_required(VERSION 3.15)
project(cvsd_codec LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)

add_library(cvsd_codec SHARED cvsd_codec.cpp)
target_include_directories(cvsd_codec PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})

# Strip "lib" prefix on all platforms so the DLL is just "cvsd_codec.dll"
set_target_properties(cvsd_codec PROPERTIES PREFIX "")
