cmake_minimum_required(VERSION 3.15)
project(IocpEchoServer CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(iocp_echo_server src/main.cpp)

if(WIN32)
    target_link_libraries(iocp_echo_server PRIVATE Ws2_32)
endif()
