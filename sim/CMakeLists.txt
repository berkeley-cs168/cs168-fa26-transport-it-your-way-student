# Builds transportlab/_tlsim, the ns-3 backend, against a pinned ns-3 build.
# tools/setup-ns3.sh runs this; see it for the ns-3 configure flags.
#
#   cmake -S sim -B <build> -DNS3_BUILD=<ns-3 build dir> -DNS3_VERSION=3.48
cmake_minimum_required(VERSION 3.16)
project(tlsim CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# No fused multiply-add, so x86 and ARM compute the same floating point
# results. ns-3 itself is built with the same flag.
add_compile_options(-ffp-contract=off)

find_package(Python 3.10 REQUIRED COMPONENTS Interpreter Development.Module)

set(NS3_BUILD "" CACHE PATH "ns-3 build directory, holding include/ and lib/")
set(NS3_VERSION "" CACHE STRING "ns-3 release, such as 3.48")
if(NOT EXISTS "${NS3_BUILD}/include/ns3/core-module.h")
  message(FATAL_ERROR "no ns-3 headers under NS3_BUILD=${NS3_BUILD}. run make setup")
endif()
# Looked up fresh every time, so pointing NS3_BUILD somewhere else takes effect.
unset(NS3_LIB CACHE)
find_library(NS3_LIB ns${NS3_VERSION}-monolib PATHS "${NS3_BUILD}/lib" NO_DEFAULT_PATH REQUIRED)

Python_add_library(_tlsim MODULE WITH_SOABI tlsim.cc)
target_include_directories(_tlsim PRIVATE
  "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/pybind11/include"
  "${NS3_BUILD}/include")
target_compile_definitions(_tlsim PRIVATE TL_NS3_VERSION="${NS3_VERSION}" NS3_BUILD_PROFILE_RELEASE)
target_link_libraries(_tlsim PRIVATE "${NS3_LIB}")
# Find libns3 next to where it was built, without LD_LIBRARY_PATH.
set_target_properties(_tlsim PROPERTIES
  BUILD_RPATH "${NS3_BUILD}/lib"
  CXX_VISIBILITY_PRESET hidden
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../transportlab")
