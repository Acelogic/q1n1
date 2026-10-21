/* SPDX-License-Identifier: MIT */
/* Read-only AppleHPM status inventory. No unlock, DBMa, Command, or Write calls.
 * Build with the separately licensed macvdmtool AppleHPMLib.h include path. */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include "AppleHPMLib.h"

int main(int argc, char **argv)
{
    if (argc != 1) {
        std::fprintf(stderr, "Usage: %s\nRead-only HPM register inventory. No options.\n", argv[0]);
        return 2;
    }
    io_iterator_t iter = 0;
    auto ret = IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("AppleHPM"), &iter);
    if (ret) { std::fprintf(stderr, "HPM inventory error: 0x%x\n", ret); return 1; }
    unsigned success = 0, failed = 0;
    io_service_t service;
    while ((service = IOIteratorNext(iter))) {
        int32_t rid = -1;
        auto prop = IORegistryEntryCreateCFProperty(service, CFSTR("RID"), kCFAllocatorDefault, 0);
        if (prop && CFGetTypeID(prop) == CFNumberGetTypeID())
            CFNumberGetValue((CFNumberRef)prop, kCFNumberSInt32Type, &rid);
        if (prop) CFRelease(prop);
        uint64_t id = 0;
        IORegistryEntryGetRegistryEntryID(service, &id);
        io_string_t path = {};
        IORegistryEntryGetPath(service, kIOServicePlane, path);
        std::printf("RID %d registry 0x%llx path %s\n", rid, (unsigned long long)id, path);
        IOCFPlugInInterface **plugin = nullptr;
        SInt32 score = 0;
        ret = IOCreatePlugInInterfaceForService(service, kAppleHPMLibType, kIOCFPlugInInterfaceID, &plugin, &score);
        if (ret || !plugin) {
            std::printf("  Plugin unavailable: 0x%x\n", ret); failed++;
            IOObjectRelease(service); continue;
        }
        AppleHPMLib **device = nullptr;
        auto hr = (*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(kAppleHPMLibInterface), (LPVOID *)&device);
        if (hr || !device) {
            std::printf("  Interface unavailable: 0x%x\n", (unsigned)hr); failed++;
        } else {
            for (uint8_t reg : {uint8_t(0x03),uint8_t(0x1a),uint8_t(0x3f),uint8_t(0x40)}) {
                uint8_t data[64] = {};
                uint64_t length = 0;
                ret = (*device)->Read(device, 0, reg, data, sizeof(data), 0, &length);
                std::printf("  reg 0x%02x status 0x%x length %llu:", reg, ret, (unsigned long long)length);
                if (!ret && length <= sizeof(data)) {
                    for (uint64_t i = 0; i < length; i++) std::printf(" %02x",data[i]);
                    success++;
                } else failed++;
                std::printf("\n");
            }
            (*device)->Release(device);
        }
        IODestroyPlugInInterface(plugin);
        IOObjectRelease(service);
    }
    IOObjectRelease(iter);
    std::printf("Read-only result: %u register reads succeeded, %u failures\n",success,failed);
    return success && !failed ? 0 : 1;
}
