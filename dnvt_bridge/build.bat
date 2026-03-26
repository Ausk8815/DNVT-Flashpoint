@echo off
setlocal
cd /d "%~dp0"

set LIBUSB_DLL_DIR=C:\Users\Ausk\AppData\Roaming\Python\Python310\site-packages\libusb\_platform\windows\x86_64
set CVSD_DIR=..\cvsd_codec
set INC=-Iinclude -I..

echo === Generating libusb import library ===
dlltool -d "%LIBUSB_DLL_DIR%\libusb-1.0.def" -l libusb-1.0.a

echo === Compiling CVSD codec ===
g++ -c -O2 -std=c++17 %INC% "%CVSD_DIR%\cvsd_codec.cpp" -o cvsd_codec.o

echo === Compiling DNVT bridge ===
g++ -c -O2 -std=c++17 %INC% dnvt_bridge.cpp -o dnvt_bridge.o

echo === Linking DLL ===
g++ -shared -o dnvt_bridge.dll dnvt_bridge.o cvsd_codec.o -L. -lusb-1.0 -static-libgcc -static-libstdc++ -lpthread

if exist dnvt_bridge.dll (
    echo === SUCCESS: dnvt_bridge.dll built ===
    copy /Y dnvt_bridge.dll ..\ >nul
    copy /Y "%LIBUSB_DLL_DIR%\libusb-1.0.dll" ..\ >nul
    echo Copied dnvt_bridge.dll and libusb-1.0.dll to project root
) else (
    echo === BUILD FAILED ===
)

del /q *.o *.a 2>nul
endlocal
